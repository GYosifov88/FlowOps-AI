"""The analysis service: create, dedup, force, history, failure statuses, hygiene."""
import copy
import json
import logging

import pytest
from sqlalchemy import func, select

import app.services.analysis as analysis_service
from app.analysis.analyzer import AnalyzerResult
from app.analysis.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from app.analysis.schema import Fact, CheckedAnalysis
from app.models import AnalysisStatus, ExecutionEvent, IncidentAnalysis
from app.services.analysis import analyze_incident, list_analyses
from app.services.incident_context import build_context, context_hash
from app.services.incidents import IncidentNotFound
from tests.analysis_helpers import FakeAnalyzer, ev, good_output, ok_result
from tests.corpus_helpers import ingest_case, load_corpus, register_workflows

CORPUS = load_corpus()
CASES = {case["id"]: case for case in CORPUS["cases"]}


@pytest.fixture
def seeded(db_session):
    """The corpus Sheets-credential incident (3 failures). Returns (incident_id, workflow_ids)."""
    workflow_ids = register_workflows(db_session, CORPUS)
    event = None
    for case_id in ("c01", "c02", "c03"):
        event = ingest_case(db_session, CASES[case_id], workflow_ids)
    db_session.expire_all()
    return db_session.get(ExecutionEvent, event.id).incident_id, workflow_ids


@pytest.fixture
def incident_id(seeded):
    return seeded[0]


def rows(db):
    db.expire_all()
    return list(db.scalars(select(IncidentAnalysis).order_by(IncidentAnalysis.id)))


def snapshot(row):
    return {column.name: getattr(row, column.name) for column in IncidentAnalysis.__table__.columns}


def add_same_problem_event(db, workflow_ids):
    """A new failure with the same signature as c01-c03, so the context (not the incident) changes."""
    case = copy.deepcopy(CASES["c03"])
    case["execution"]["id"] = "910099"
    case["execution"]["startedAt"] = "2026-10-27T06:00:00.000Z"
    case["execution"]["stoppedAt"] = "2026-10-27T06:00:01.000Z"
    return ingest_case(db, case, workflow_ids)


# ====================================================================== creating


def test_a_new_analysis_is_verified_and_stored(db_session, incident_id):
    analyzer = FakeAnalyzer()
    outcome = analyze_incident(db_session, incident_id, analyzer)

    assert outcome.result == "created" and len(analyzer.calls) == 1
    (row,) = rows(db_session)
    assert row.id == outcome.row.id and row.incident_id == incident_id
    assert row.status == AnalysisStatus.OK
    assert (row.provider, row.model, row.prompt_version, row.forced) == ("fake", "fake-model-1", PROMPT_VERSION, False)
    assert (row.input_tokens, row.output_tokens, row.reasoning_tokens) == (1500, 2500, 2000)
    assert row.context_version == 2 and row.refusal is None and row.error_type is None
    stored = CheckedAnalysis.model_validate_json(row.analysis_json)
    assert len(stored.facts) == 2 and stored.hypotheses[0].supported is True
    assert json.loads(row.validation_notes_json) == []


def test_the_analyzer_gets_exactly_the_preview_context_and_the_hash_matches(db_session, incident_id):
    analyzer = FakeAnalyzer()
    analyze_incident(db_session, incident_id, analyzer)

    expected = build_context(db_session, incident_id)
    assert analyzer.calls == [expected]
    assert rows(db_session)[0].context_hash == context_hash(expected)


def test_unverifiable_claims_are_removed_before_storing_and_the_notes_are_kept(db_session, incident_id):
    output = good_output(
        facts=[
            Fact(statement="Invented.", evidence=[ev("untrusted.error.cause", "something completely made up")]),
            Fact(statement="Real.", evidence=[ev("untrusted.node_name", "SHEETS — Load Settings")]),
        ]
    )
    analyze_incident(db_session, incident_id, FakeAnalyzer([ok_result(output)]))

    (row,) = rows(db_session)
    stored = CheckedAnalysis.model_validate_json(row.analysis_json)
    assert [f.statement for f in stored.facts] == ["Real."]
    notes = json.loads(row.validation_notes_json)
    assert any("fact[0] removed" in n for n in notes)


def test_the_slow_call_runs_with_no_transaction_open(db_session, incident_id):
    seen = []

    def results(context):
        seen.append(db_session.in_transaction())
        return ok_result()

    analyze_incident(db_session, incident_id, FakeAnalyzer(results))
    assert seen == [False]


def test_unknown_incident_never_calls_the_analyzer(db_session):
    analyzer = FakeAnalyzer()
    with pytest.raises(IncidentNotFound):
        analyze_incident(db_session, 999, analyzer)
    assert analyzer.calls == [] and rows(db_session) == []


# ====================================================================== dedup, force, history


def test_an_identical_second_request_is_free(db_session, incident_id):
    analyzer = FakeAnalyzer()
    first = analyze_incident(db_session, incident_id, analyzer)
    second = analyze_incident(db_session, incident_id, analyzer)

    assert (first.result, second.result) == ("created", "reused")
    assert second.row.id == first.row.id and len(analyzer.calls) == 1
    assert len(rows(db_session)) == 1


def test_force_makes_a_new_row_and_leaves_the_old_one_untouched(db_session, incident_id):
    analyzer = FakeAnalyzer()
    first = analyze_incident(db_session, incident_id, analyzer)
    before = snapshot(rows(db_session)[0])

    forced = analyze_incident(db_session, incident_id, analyzer, force=True)

    assert forced.result == "created" and forced.row.id != first.row.id and forced.row.forced is True
    assert len(analyzer.calls) == 2
    stored = rows(db_session)
    assert len(stored) == 2 and snapshot(stored[0]) == before


def test_after_a_forced_run_the_plain_request_reuses_the_newest_ok_row(db_session, incident_id):
    analyzer = FakeAnalyzer()
    analyze_incident(db_session, incident_id, analyzer)
    forced = analyze_incident(db_session, incident_id, analyzer, force=True)
    again = analyze_incident(db_session, incident_id, analyzer)
    assert again.result == "reused" and again.row.id == forced.row.id and len(analyzer.calls) == 2


def test_new_data_changes_the_context_and_so_calls_the_model_again(db_session, seeded):
    incident_id, workflow_ids = seeded
    analyzer = FakeAnalyzer()
    analyze_incident(db_session, incident_id, analyzer)

    add_same_problem_event(db_session, workflow_ids)
    second = analyze_incident(db_session, incident_id, analyzer)

    assert second.result == "created" and len(analyzer.calls) == 2
    first, newest = rows(db_session)
    assert first.context_hash != newest.context_hash


def test_another_model_or_prompt_version_is_not_a_duplicate(db_session, incident_id, monkeypatch):
    base = FakeAnalyzer(model="model-a")
    other = FakeAnalyzer(model="model-b")
    analyze_incident(db_session, incident_id, base)

    assert analyze_incident(db_session, incident_id, other).result == "created"
    assert analyze_incident(db_session, incident_id, other).result == "reused"

    monkeypatch.setattr(analysis_service, "PROMPT_VERSION", "next")
    assert analyze_incident(db_session, incident_id, base).result == "created"
    assert len(rows(db_session)) == 3


def test_history_is_listed_newest_first(db_session, incident_id):
    analyzer = FakeAnalyzer()
    ids = [analyze_incident(db_session, incident_id, analyzer, force=True).row.id for _ in range(3)]
    assert [r.id for r in list_analyses(db_session, incident_id)] == ids[::-1]
    with pytest.raises(IncidentNotFound):
        list_analyses(db_session, 999)


# ====================================================================== failure statuses


FAILURES = [
    AnalyzerResult("refused", refusal="I can't help with that.", input_tokens=900, output_tokens=20),
    AnalyzerResult("incomplete", error_type="max_output_tokens", input_tokens=900, output_tokens=16000),
    AnalyzerResult("invalid", error_type="no_parsed_output", input_tokens=900, output_tokens=300),
    AnalyzerResult("error", error_type="APITimeoutError"),
]


@pytest.mark.parametrize("failure", FAILURES, ids=lambda r: r.status)
def test_a_failed_attempt_is_stored_without_an_analysis_and_never_blocks_a_retry(db_session, incident_id, failure):
    analyzer = FakeAnalyzer([failure, ok_result()])

    first = analyze_incident(db_session, incident_id, analyzer)
    assert first.row.status.value == failure.status
    assert first.row.analysis_json is None and first.row.validation_notes_json is None
    assert (first.row.refusal, first.row.error_type) == (failure.refusal, failure.error_type)
    assert (first.row.input_tokens, first.row.output_tokens) == (failure.input_tokens, failure.output_tokens)

    retry = analyze_incident(db_session, incident_id, analyzer)
    assert retry.result == "created" and retry.row.status == AnalysisStatus.OK and len(analyzer.calls) == 2
    assert analyze_incident(db_session, incident_id, analyzer).result == "reused"


def test_an_analyzer_that_breaks_its_contract_is_recorded_not_propagated(db_session, incident_id):
    secret = "SECRET-IN-EXCEPTION-123"
    outcome = analyze_incident(db_session, incident_id, FakeAnalyzer([RuntimeError(secret)]))

    assert outcome.row.status == AnalysisStatus.ERROR and outcome.row.error_type == "RuntimeError"
    assert secret not in json.dumps(snapshot(outcome.row), default=str)


# ====================================================================== hygiene


def test_logs_carry_metadata_only_never_the_context_or_the_prompt(db_session, incident_id, caplog):
    with caplog.at_level(logging.DEBUG):
        analyze_incident(db_session, incident_id, FakeAnalyzer())
        analyze_incident(db_session, incident_id, FakeAnalyzer())  # the reuse path logs too

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "analysis stored" in text and "analysis reused" in text
    for forbidden in ("SHEETS", "credential", "reconnected", "Example-marketing", SYSTEM_PROMPT[:40],
                      "untrusted_incident_data", "OPENAI"):
        assert forbidden not in text


def test_rows_are_inserted_never_deleted_or_rewritten(db_session, incident_id):
    analyzer = FakeAnalyzer()
    analyze_incident(db_session, incident_id, analyzer)
    analyze_incident(db_session, incident_id, analyzer, force=True)
    first = snapshot(rows(db_session)[0])

    analyze_incident(db_session, incident_id, FakeAnalyzer([AnalyzerResult("error", error_type="X")]), force=True)

    assert db_session.scalar(select(func.count()).select_from(IncidentAnalysis)) == 3
    assert snapshot(rows(db_session)[0]) == first
