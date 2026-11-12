"""The corpus decides whether grouping merges what it should and splits what it should not.

Every case in tests/corpus/incidents.json carries a `group` label. The contract:
cases with the SAME group end up in ONE incident; cases with DIFFERENT groups never do.
"""
import json
from collections import defaultdict

import pytest
from sqlalchemy import select

import app.signature as signature
from app.models import ExecutionEvent, Incident
from app.services.incident_context import FRAME_TAG, build_context, canonical_json, frame_context
from app.services.incidents import list_incidents
from tests.corpus_helpers import ingest_corpus, load_corpus
from tests.n8n_helpers import load

CORPUS = load_corpus()
CASES = CORPUS["cases"]
BY_ID = {c["id"]: c for c in CASES}

# Pairs that look alike and MUST stay apart, with the reason (see each case's `note`).
MUST_NOT_MERGE = [
    ("c01", "c04", "same error and node, another workflow"),
    ("c01", "c05", "same node, different error"),
    ("c09", "c10", "same node, 500 vs 429"),
    ("c11", "c13", "same message, another node"),
    ("c14", "c15", "same node, a different field is undefined"),
    ("c17", "c19", "crashed with no detail, another workflow"),
    ("c20", "c21", "redacted vs data too large"),
]


@pytest.fixture
def loaded(db_session):
    workflow_ids, event_ids = ingest_corpus(db_session, CORPUS)
    db_session.expire_all()
    incident_of = {
        cid: db_session.get(ExecutionEvent, eid).incident_id for cid, eid in event_ids.items()
    }
    return workflow_ids, event_ids, incident_of


# ------------------------------------------------------------------ the corpus itself


def test_corpus_is_big_enough_and_covers_the_designed_kinds():
    assert len(CASES) >= 15
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert {c["tier"] for c in CASES} == {1, 3}
    assert {c["source"] for c in CASES} == {"n8n", "api"}
    assert any(c["trap"] for c in CASES)
    assert {"auth_credential", "rate_limit", "timeout", "upstream_error", "data_schema",
            "configuration", "infrastructure_crash", "unknown"} <= {c["category"] for c in CASES}


def test_corpus_was_written_for_the_current_signature_rules():
    assert CORPUS["signature_version"] == signature.SIGNATURE_VERSION


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_case_is_labelled(case):
    for key in ("id", "tier", "source", "workflow", "group", "category", "expected_message",
                "facts_knowable", "cause_knowable", "trap"):
        assert key in case, key
    assert case["workflow"] in CORPUS["workflows"]
    assert case["facts_knowable"] and all(isinstance(f, str) for f in case["facts_knowable"])


F3 = load("F3_error_with_data.json")
F3_ERROR = F3["data"]["resultData"]["error"]
ALLOWED_TOP = set(F3) | {"dataTooLargeToDisplay"}
ALLOWED_DATA = {"resultData", "redactionInfo"}
ALLOWED_RESULT = set(F3["data"]["resultData"])
ALLOWED_ERROR = set(F3_ERROR)


@pytest.mark.parametrize("case", [c for c in CASES if c["source"] == "n8n"], ids=lambda c: c["id"])
def test_n8n_cases_have_the_shape_of_the_real_execution(case):
    """Compared against tests/fixtures/n8n/F3_error_with_data.json: no invented fields."""
    execution = case["execution"]
    assert set(execution) <= ALLOWED_TOP
    data = execution.get("data", {})
    assert set(data) <= ALLOWED_DATA
    result = data.get("resultData", {})
    assert set(result) <= ALLOWED_RESULT
    error = result.get("error")
    if error:
        assert set(error) <= ALLOWED_ERROR
        assert set(error.get("node", {})) <= {"name"}
        assert error["stack"].startswith(f"{error['name']}: {error['message']}")
    for key in ("status", "mode", "startedAt", "stoppedAt"):
        assert type(execution[key]) is type(F3[key])


def test_the_tier_1_case_is_the_real_failure_as_scrubbed_in_F3():
    error = BY_ID["c01"]["execution"]["data"]["resultData"]["error"]
    for key in ("name", "message", "description", "httpCode"):
        assert error[key] == F3_ERROR[key]
    assert error["node"] == F3_ERROR["node"]
    assert BY_ID["c01"]["execution"]["data"]["resultData"]["lastNodeExecuted"] == \
        F3["data"]["resultData"]["lastNodeExecuted"]


# ------------------------------------------------------------------ grouping


def test_same_group_merges_and_different_groups_do_not(loaded):
    _, _, incident_of = loaded
    assert None not in incident_of.values()  # every corpus case is a failure

    by_group = defaultdict(set)
    for case in CASES:
        by_group[case["group"]].add(incident_of[case["id"]])
    assert {g: len(ids) for g, ids in by_group.items() if len(ids) != 1} == {}, "a group was split"

    all_incidents = [next(iter(ids)) for ids in by_group.values()]
    assert len(set(all_incidents)) == len(by_group), "two different groups were merged"


@pytest.mark.parametrize("a, b, why", MUST_NOT_MERGE, ids=[f"{a}-{b}" for a, b, _ in MUST_NOT_MERGE])
def test_lookalikes_stay_apart(loaded, a, b, why):
    _, _, incident_of = loaded
    assert incident_of[a] != incident_of[b], why


def test_variants_that_must_merge_do_merge(loaded):
    _, _, incident_of = loaded
    for group in {c["group"] for c in CASES}:
        members = [c["id"] for c in CASES if c["group"] == group]
        assert len({incident_of[m] for m in members}) == 1, (group, members)
    assert sum(1 for g in {c["group"] for c in CASES} if len([c for c in CASES if c["group"] == g]) > 1) >= 6


def test_occurrence_counts_equal_group_sizes(loaded, db_session):
    summaries, total = list_incidents(db_session, limit=200)
    assert total == len({c["group"] for c in CASES})
    sizes = defaultdict(int)
    for case in CASES:
        sizes[case["group"]] += 1
    assert sorted(s.occurrence_count for s in summaries) == sorted(sizes.values())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_normalized_message_matches_the_label(loaded, db_session, case):
    _, _, incident_of = loaded
    assert db_session.get(Incident, incident_of[case["id"]]).normalized_message == case["expected_message"]


def test_unattributed_cases_have_no_node_even_when_last_node_executed_is_present(loaded, db_session):
    """Only error.node.name counts (M8 rule): lastNodeExecuted on c18, c22, c23 must be ignored."""
    _, _, incident_of = loaded
    for cid in ("c17", "c18", "c19", "c20", "c21", "c22", "c23", "c30"):
        assert db_session.get(Incident, incident_of[cid]).node_name is None, cid
    assert db_session.get(Incident, incident_of["c01"]).node_name == "SHEETS — Load Settings"


def test_every_incident_belongs_to_the_workflow_of_its_cases(loaded, db_session):
    workflow_ids, _, incident_of = loaded
    for case in CASES:
        assert db_session.get(Incident, incident_of[case["id"]]).workflow_id == workflow_ids[case["workflow"]]


def test_ingest_order_does_not_change_the_signatures(db_session, tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.database import create_db_engine, init_db

    ingest_corpus(db_session, CORPUS)
    forward = {i.signature for i in db_session.scalars(select(Incident))}

    other = create_db_engine(f"sqlite:///{(tmp_path / 'reversed.db').as_posix()}")
    try:
        init_db(other)
        with sessionmaker(bind=other)() as session:
            ingest_corpus(session, CORPUS, reverse=True)
            backward = {i.signature for i in session.scalars(select(Incident))}
    finally:
        other.dispose()
    assert forward == backward and len(forward) == len({c["group"] for c in CASES})


# ------------------------------------------------------------------ secrets


FAKE_SECRETS = ["FAKE-TOKEN-ABCDEF123456", "FAKE-TOKEN-ZZZZZZ999999", "FAKE-KEY-0123456789", "FAKE-KEY-9876543210"]


def test_fake_secrets_do_not_reach_any_readable_part(loaded, db_session):
    texts = []
    for incident in db_session.scalars(select(Incident)):
        texts += [incident.normalized_message, incident.node_name or "", incident.error_name or ""]
    for event in db_session.scalars(select(ExecutionEvent)):
        texts.append(event.error_summary or "")
    blob = "\n".join(texts)
    for secret in FAKE_SECRETS:
        assert secret not in blob
    assert "[redacted]" in blob


def test_fake_secrets_do_not_reach_the_api(client, db_session, loaded):
    bodies = [client.get("/api/incidents?limit=200").text]
    ids = [i.id for i in db_session.scalars(select(Incident))]
    bodies += [client.get(f"/api/incidents/{i}").text for i in ids]
    bodies += [client.get(f"/api/incidents/{i}/context").text for i in ids]
    bodies.append(client.get("/api/events?limit=200").text)
    for body in bodies:
        for secret in FAKE_SECRETS:
            assert secret not in body
        assert "error_raw" not in body


# ------------------------------------------------------------------ the context (V2-M2)


def has_real_error_detail(case) -> bool:
    """n8n cases whose execution carried an error object; API free text never has a projection."""
    if case["source"] != "n8n":
        return False
    error = case["execution"].get("data", {}).get("resultData", {}).get("error")
    return bool(error)


def test_every_corpus_context_is_clean_and_complete(loaded, db_session):
    _, _, incident_of = loaded
    seen = set()
    for case in CASES:
        incident_id = incident_of[case["id"]]
        if incident_id in seen:
            continue
        seen.add(incident_id)
        context = build_context(db_session, incident_id)
        blob = canonical_json(context)

        for secret in FAKE_SECRETS:
            assert secret not in blob, case["id"]
        assert "@" not in blob, case["id"]               # no e-mail (or user@host) survived
        assert "error_raw" not in blob and "extra_field" not in blob
        assert len(blob) < 8000, (case["id"], len(blob))
        assert context["trusted"]["error_detail_available"] is has_real_error_detail(case), case["id"]
        assert (context["untrusted"]["error"] is not None) is has_real_error_detail(case), case["id"]
        assert context["trusted"]["occurrences"]["count"] >= 1

        framed = frame_context(context)
        assert framed.count("<") == 2 and framed.count(">") == 2
        inner = framed.removeprefix(f"<{FRAME_TAG}>\n").removesuffix(f"\n</{FRAME_TAG}>")
        assert json.loads(inner) == context
    assert len(seen) == len({c["group"] for c in CASES})


def test_the_real_credential_incident_context_reads_as_expected(loaded, db_session):
    _, _, incident_of = loaded
    context = build_context(db_session, incident_of["c01"])
    untrusted = context["untrusted"]
    assert untrusted["node_name"] == "SHEETS — Load Settings"
    assert untrusted["error_name"] == "NodeApiError"
    assert untrusted["error"]["message"].startswith('The credential "Example-marketing"')
    # The LATEST occurrence is c03 (26 Oct): no description, httpCode 401 -- not c01's.
    assert untrusted["error"]["description"] is None and untrusted["error"]["http_code"] == "401"
    assert untrusted["error"]["stack"][0].startswith("NodeApiError:")
    assert context["trusted"]["occurrences"]["count"] == 3
    assert context["trusted"]["facts"]["last_success_before_first_failure"] is None  # the corpus holds failures only
