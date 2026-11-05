import json
import logging
from datetime import datetime, timedelta

import httpx2
import pytest
from sqlalchemy import func, select, text

import app.services.n8n_sync as sync_module
from app.models import ExecutionEvent, ExecutionStatus, Workflow
from app.n8n_client import N8nAuthError, N8nClient, N8nUnavailable
from app.services.n8n_sync import (
    ErrorDetail,
    extract_error,
    map_status,
    registered_workflows,
    sync_all,
    sync_workflow,
    to_event,
)
from tests.n8n_helpers import (
    WF,
    FakeN8n,
    error_detail,
    fixture_executions,
    load,
    make_execution,
)

NOW = datetime(2026, 10, 30, 12, 0, 0)  # "now" for the age rules; the data is all older


@pytest.fixture
def wf(db_session):
    workflow = Workflow(name="Example Workflow", source="n8n", external_id=WF)
    db_session.add(workflow)
    db_session.commit()
    return workflow


def events(db_session) -> dict[str, ExecutionEvent]:
    db_session.expire_all()
    return {e.external_event_id: e for e in db_session.scalars(select(ExecutionEvent))}


def count(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(ExecutionEvent))


def run(db_session, fake, wf, now=NOW):
    return sync_workflow(db_session, fake, wf, now=now)


# ====================================================================== mapping


@pytest.mark.parametrize("raw, stopped, expected", [
    ("success", "2026-10-30T10:00:00Z", ExecutionStatus.SUCCESS),
    ("error", "2026-10-30T10:00:00Z", ExecutionStatus.FAILED),
    ("crashed", "2026-10-30T10:00:00Z", ExecutionStatus.FAILED),
    ("canceled", "2026-10-30T10:00:00Z", ExecutionStatus.CANCELED),
    ("waiting", "2026-10-30T10:00:00Z", ExecutionStatus.WAITING),
    ("running", None, ExecutionStatus.RUNNING),
    ("new", None, ExecutionStatus.RUNNING),
    ("unknown", None, ExecutionStatus.UNKNOWN),
    ("SUCCESS", None, ExecutionStatus.SUCCESS),
    (None, None, ExecutionStatus.RUNNING),  # the public API returns null while not finished
    (None, "2026-10-30T10:00:00Z", ExecutionStatus.UNKNOWN),
])
def test_status_mapping(raw, stopped, expected):
    assert map_status(raw, stopped) == expected


def test_an_unrecognised_status_becomes_unknown_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_status("exploded-in-a-new-way") == ExecutionStatus.UNKNOWN

    assert "Unrecognised n8n execution status" in caplog.text


def test_the_canonical_enum_covers_every_n8n_status():
    for n8n_status in ("canceled", "crashed", "error", "new", "running", "success", "unknown", "waiting"):
        assert map_status(n8n_status, "2026-10-30T10:00:00Z") in set(ExecutionStatus)


def test_to_event_for_a_real_successful_execution():
    execution = fixture_executions()[0]  # 20637

    event, summary = to_event(7, execution, ExecutionStatus.SUCCESS, None)

    assert event.external_event_id == "n8n:20637" and event.workflow_id == 7
    assert event.status == ExecutionStatus.SUCCESS and event.mode == "webhook"
    assert event.started_at.isoformat() == "2026-10-30T19:04:25.784000+00:00"
    assert event.finished_at.isoformat() == "2026-10-30T19:05:12.399000+00:00"
    assert (event.node_name, event.error_raw, summary) == (None, None, None)


def test_a_waiting_execution_has_a_stopped_at_but_no_finish_time():
    execution = make_execution(1, "waiting", stopped="2026-10-30T10:00:05.000Z")

    event, _ = to_event(1, execution, ExecutionStatus.WAITING, None)

    assert event.finished_at is None


def test_started_at_may_be_missing_for_a_queued_execution():
    execution = make_execution(1, "new", started=None, stopped=None)

    event, _ = to_event(1, execution, ExecutionStatus.RUNNING, None)

    assert event.started_at is None and event.finished_at is None


def test_a_crashed_execution_is_marked_in_the_summary():
    crashed = make_execution(1, "crashed")
    with_detail = ErrorDetail(True, "Error: out of memory", "{}", None)

    assert to_event(1, crashed, ExecutionStatus.FAILED, with_detail)[1] == "n8n: crashed - Error: out of memory"
    assert to_event(1, crashed, ExecutionStatus.FAILED, None)[1] == "n8n: crashed (error details unavailable)"


# ============================================================ error extraction


def test_the_real_error_fixture_yields_the_failing_node_and_a_readable_summary():
    error = extract_error(load("F3_error_with_data.json"))

    assert error.available
    assert error.node_name == "SHEETS — Load Settings"
    assert error.summary == 'NodeApiError: The credential "Example-marketing" needs to be reconnected.'
    assert set(json.loads(error.raw)) <= {"name", "message", "description", "httpCode", "stack", "node"}
    assert json.loads(error.raw)["node"] == {"name": "SHEETS — Load Settings"}


def test_only_the_node_name_survives_never_its_credentials_or_parameters():
    """The real error.node carries credentials and parameters (confirmed on a real instance)."""
    detail = error_detail(
        make_execution(1, "error"),
        name="NodeApiError", message="boom", httpCode="401",
        node={
            "name": "Real Node", "id": "NODE-ID-MARKER", "type": "TYPE-MARKER",
            "credentials": {"googleApi": {"id": "CRED-MARKER", "name": "CRED-NAME-MARKER"}},
            "parameters": {"url": "PARAM-MARKER", "headers": "HEADER-MARKER"},
            "position": [1, 2], "alwaysOutputData": True,
        },
    )
    detail["data"]["resultData"].update(
        runData={"Node": [{"data": "RUNDATA-MARKER"}]}, pinData={"x": "PINDATA-MARKER"},
        lastNodeExecuted="Some Other Node",
    )
    detail["workflowData"] = {"nodes": "WORKFLOWDATA-MARKER"}
    detail["customData"] = {"k": "CUSTOMDATA-MARKER"}

    error = extract_error(detail)

    assert error.node_name == "Real Node"
    kept = json.loads(error.raw)
    assert kept["node"] == {"name": "Real Node"}
    assert set(kept) == {"name", "message", "httpCode", "node"}
    everything = error.raw + error.summary + str(error.node_name)
    for marker in ("NODE-ID", "TYPE-MARKER", "CRED", "PARAM-MARKER", "HEADER-MARKER", "RUNDATA",
                   "PINDATA", "WORKFLOWDATA", "CUSTOMDATA", "Some Other Node"):
        assert marker not in everything


def test_node_name_comes_only_from_the_error_node_never_from_the_last_node_executed():
    only_last = error_detail(make_execution(1, "error"), name="E", message="boom")
    only_last["data"]["resultData"]["lastNodeExecuted"] = "Last Node That Ran"
    both = error_detail(make_execution(1, "error"), name="E", message="boom", node={"name": "The Failing Node"})
    both["data"]["resultData"]["lastNodeExecuted"] = "Last Node That Ran"

    assert extract_error(only_last).node_name is None
    assert "Last Node That Ran" not in extract_error(only_last).raw
    assert extract_error(both).node_name == "The Failing Node"
    assert "Last Node That Ran" not in extract_error(both).raw


@pytest.mark.parametrize("node", [None, "a string", [], {}, {"name": None}, {"name": ""}, {"name": "   "}, {"name": 7}])
def test_a_missing_or_malformed_node_leaves_node_name_empty(node):
    error = {"name": "E", "message": "boom"}
    if node is not None:
        error["node"] = node

    result = extract_error(error_detail(make_execution(1, "error"), **error))

    assert result.available and result.node_name is None
    assert "node" not in json.loads(result.raw)


def test_long_fields_are_capped():
    result = extract_error(error_detail(
        make_execution(1, "error"), name="N" * 1000, message="m" * 5000, description="d" * 5000,
        stack="s" * 9000, node={"name": "n" * 1000},
    ))

    kept = json.loads(result.raw)
    assert len(kept["name"]) == 255 and len(kept["message"]) == 2000
    assert len(kept["description"]) == 2000 and len(kept["stack"]) == 4000
    assert len(result.node_name) == 255
    assert len(result.raw) < 20_000  # far below the 100 KB schema cap


def test_odd_field_types_are_ignored_not_crashed_on():
    result = extract_error(error_detail(
        make_execution(1, "error"), name="E", message={"nested": "dict"}, description=["list"],
        httpCode=True, stack=123,
    ))

    assert json.loads(result.raw) == {"name": "E"}
    assert extract_error(error_detail(make_execution(1, "error"), name="E", httpCode=404)).raw.count("404") == 1


@pytest.mark.parametrize("detail, reason", [
    ({"dataTooLargeToDisplay": True, "data": {}}, "data too large"),
    ({"data": {"dataTooLargeToDisplay": True}}, "data too large"),
    ({"data": {"redactionInfo": {"isRedacted": True, "reason": "policy", "canReveal": False}}}, "redacted"),
    ({"id": "1"}, None),  # fetched without data
    ({"data": {}}, None),
    ({"data": {"resultData": {}}}, None),
    ({"data": {"resultData": {"error": "a string"}}}, None),
    ({"data": {"resultData": {"error": {"stack": "only a stack"}}}}, None),
])
def test_missing_or_withheld_error_detail_is_recorded_as_unavailable(detail, reason):
    result = extract_error(detail)

    assert not result.available and result.raw is None and result.node_name is None
    expected = "n8n: error details unavailable" + (f" ({reason})" if reason else "")
    assert result.summary == expected


# ================================================================ a sync run


def test_first_run_ingests_everything_with_the_real_fixtures(db_session, wf):
    fake = FakeN8n()

    result = run(db_session, fake, wf)

    assert (result.created, result.updated, result.unchanged, result.skipped) == (9, 0, 0, 0)
    assert result.warnings == []
    rows = events(db_session)
    assert len(rows) == 9
    assert sorted(r.status for r in rows.values()).count(ExecutionStatus.SUCCESS) == 8
    ok = rows["n8n:20637"]
    assert ok.status == ExecutionStatus.SUCCESS and ok.workflow_id == wf.id
    assert ok.duration_ms == 46_615 and ok.mode == "webhook"
    assert ok.node_name is None and ok.error_raw is None and ok.error_summary is None
    failed = rows["n8n:20628"]
    assert failed.status == ExecutionStatus.FAILED
    assert failed.node_name == "SHEETS — Load Settings"
    assert failed.error_summary == 'NodeApiError: The credential "Example-marketing" needs to be reconnected.'
    assert set(json.loads(failed.error_raw)) <= {"name", "message", "description", "httpCode", "stack", "node"}


def test_error_detail_is_fetched_for_failures_only(db_session, wf):
    fake = FakeN8n()

    run(db_session, fake, wf)

    assert fake.fetched(include_data=True) == ["20628"]  # the one failure, nothing else
    assert fake.fetched(include_data=False) == []  # successes are never fetched at all
    assert set(c[0] for c in fake.calls) <= {"workflow", "list", "get"}


def test_a_second_run_changes_nothing_and_fetches_nothing(db_session, wf):
    run(db_session, FakeN8n(), wf)
    before = {k: (v.id, v.status, v.finished_at) for k, v in events(db_session).items()}
    fake = FakeN8n()

    result = run(db_session, fake, wf)

    assert (result.created, result.updated, result.unchanged, result.skipped) == (0, 0, 0, 0)
    assert {k: (v.id, v.status, v.finished_at) for k, v in events(db_session).items()} == before
    assert fake.fetched() == []  # known finished executions are never re-fetched
    assert fake.list_calls() == 2  # walked the overlap window, then stopped


def test_oldest_executions_are_ingested_first(db_session, wf, monkeypatch):
    order = []
    real = sync_module.ingest_event

    def spy(db, data, **kwargs):
        order.append(data.external_event_id)
        return real(db, data, **kwargs)

    monkeypatch.setattr(sync_module, "ingest_event", spy)

    run(db_session, FakeN8n(), wf)

    assert order == [f"n8n:{i}" for i in (20628, 20629, 20630, 20631, 20633, 20634, 20635, 20636, 20637)]


def test_an_interrupted_run_leaves_a_clean_prefix_and_the_next_run_finishes(db_session, wf, monkeypatch):
    real = sync_module.ingest_event
    calls = {"n": 0}

    def dies_on_the_fifth(db, data, **kwargs):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("laptop went to sleep")
        return real(db, data, **kwargs)

    monkeypatch.setattr(sync_module, "ingest_event", dies_on_the_fifth)
    with pytest.raises(RuntimeError):
        run(db_session, FakeN8n(), wf)
    # The four OLDEST are stored, nothing newer, nothing half-written.
    assert set(events(db_session)) == {f"n8n:{i}" for i in (20628, 20629, 20630, 20631)}

    monkeypatch.setattr(sync_module, "ingest_event", real)
    result = run(db_session, FakeN8n(), wf)

    assert result.created == 5 and result.warnings == []
    assert count(db_session) == 9


def test_catch_up_brings_back_exactly_the_missing_newest_executions(db_session, wf):
    run(db_session, FakeN8n(), wf)
    db_session.execute(text("delete from execution_events where external_event_id in "
                            "('n8n:20637','n8n:20636','n8n:20635')"))
    db_session.commit()
    fake = FakeN8n()

    result = run(db_session, fake, wf)

    assert result.created == 3 and result.warnings == []
    assert count(db_session) == 9
    assert fake.fetched() == []  # none of the three was a failure


def test_a_running_execution_is_completed_in_place_when_n8n_lists_it_as_finished(db_session, wf):
    running = make_execution(11, None, started="2026-10-30T10:00:00.000Z", stopped=None)
    run(db_session, FakeN8n([running]), wf)
    first = events(db_session)["n8n:11"]
    assert first.status == ExecutionStatus.RUNNING and first.finished_at is None
    row_id, created_at = first.id, first.created_at

    finished = make_execution(11, "success", started="2026-10-30T10:00:00.000Z", stopped="2026-10-30T10:00:30.000Z")
    result = run(db_session, FakeN8n([finished]), wf)

    assert (result.created, result.updated) == (0, 1)
    row = events(db_session)["n8n:11"]
    assert (row.id, row.created_at) == (row_id, created_at) and count(db_session) == 1
    assert row.status == ExecutionStatus.SUCCESS and row.duration_ms == 30_000


def test_a_failure_that_was_running_gets_its_error_detail_when_it_ends(db_session, wf):
    run(db_session, FakeN8n([make_execution(12, "running", stopped=None)]), wf)
    failed = make_execution(12, "error")
    detail = error_detail(failed, name="NodeApiError", message="boom", node={"name": "Fetch"})

    fake = FakeN8n([failed], details={"12": detail})
    result = run(db_session, fake, wf)

    row = events(db_session)["n8n:12"]
    assert result.updated == 1 and row.status == ExecutionStatus.FAILED
    assert row.node_name == "Fetch" and row.error_summary == "NodeApiError: boom"
    assert fake.fetched(include_data=True) == ["12"]


def test_walking_past_the_first_known_execution_catches_one_that_finished_late(db_session, wf):
    """Execution 10 started first but finished after 11; a stop at 11 would miss it."""
    run(db_session, FakeN8n([
        make_execution(11, "success", started="2026-10-30T10:05:00.000Z", stopped="2026-10-30T10:06:00.000Z"),
        make_execution(10, None, started="2026-10-30T10:00:00.000Z", stopped=None),
    ]), wf)
    assert events(db_session)["n8n:10"].status == ExecutionStatus.RUNNING

    result = run(db_session, FakeN8n([
        make_execution(11, "success", started="2026-10-30T10:05:00.000Z", stopped="2026-10-30T10:06:00.000Z"),
        make_execution(10, "success", started="2026-10-30T10:00:00.000Z", stopped="2026-10-30T10:30:00.000Z"),
    ]), wf)

    assert result.updated == 1
    assert events(db_session)["n8n:10"].status == ExecutionStatus.SUCCESS


def test_recheck_finds_an_execution_the_list_no_longer_shows_as_running(db_session, wf):
    run(db_session, FakeN8n([make_execution(10, None, stopped=None)]), wf)
    finished = make_execution(10, "success", stopped="2026-10-30T10:30:00.000Z")
    fake = FakeN8n([], details={"10": finished})  # the list shows nothing for it

    result = run(db_session, fake, wf)

    assert result.updated == 1
    assert events(db_session)["n8n:10"].status == ExecutionStatus.SUCCESS
    assert fake.fetched() == ["10"] and fake.fetched(include_data=True) == []


def seed_open_row(db_session, wf, exec_id, status, started):
    db_session.add(ExecutionEvent(workflow_id=wf.id, external_event_id=f"n8n:{exec_id}", status=status,
                                  started_at=started))
    db_session.commit()


def test_open_rows_past_the_give_up_age_are_left_alone_with_a_warning(db_session, wf):
    seed_open_row(db_session, wf, 501, ExecutionStatus.RUNNING, NOW - timedelta(days=3))
    seed_open_row(db_session, wf, 502, ExecutionStatus.UNKNOWN, NOW - timedelta(hours=25))
    fake = FakeN8n([])

    result = run(db_session, fake, wf)

    assert fake.fetched() == []
    assert any("2 unfinished execution(s) are past the give-up age" in w for w in result.warnings)
    assert {r.status for r in events(db_session).values()} == {ExecutionStatus.RUNNING, ExecutionStatus.UNKNOWN}


def test_a_waiting_row_is_rechecked_for_much_longer(db_session, wf):
    seed_open_row(db_session, wf, 503, ExecutionStatus.WAITING, NOW - timedelta(days=3))
    seed_open_row(db_session, wf, 504, ExecutionStatus.WAITING, NOW - timedelta(days=31))
    fake = FakeN8n([], details={"503": make_execution(503, "success", stopped="2026-10-30T09:00:00.000Z")})

    result = run(db_session, fake, wf)

    assert fake.fetched() == ["503"]
    assert result.updated == 1 and any("1 unfinished" in w for w in result.warnings)


def test_an_open_row_that_n8n_no_longer_has_is_left_alone_with_a_warning(db_session, wf):
    seed_open_row(db_session, wf, 505, ExecutionStatus.RUNNING, NOW - timedelta(hours=1))

    result = run(db_session, FakeN8n([], missing=["505"]), wf)

    assert events(db_session)["n8n:505"].status == ExecutionStatus.RUNNING
    assert any("505" in w and "gone from n8n" in w for w in result.warnings)


# ------------------------------------------------------------------- gaps


def test_a_gap_is_reported_when_n8n_pruned_what_flowops_last_saw(db_session, wf):
    seed = ExecutionEvent(workflow_id=wf.id, external_event_id="n8n:19000", status=ExecutionStatus.SUCCESS,
                          started_at=datetime(2026, 10, 1, 8, 0, 0))
    db_session.add(seed)
    db_session.commit()

    result = run(db_session, FakeN8n(), wf)

    assert result.created == 9  # nothing is invented, everything n8n still has is imported
    (gap,) = [w for w in result.warnings if "possible gap" in w]
    assert "2026-10-01 08:00 UTC" in gap and "2026-10-24 06:16 UTC" in gap


def test_no_gap_warning_on_a_first_run_or_a_normal_second_run(db_session, wf):
    first = run(db_session, FakeN8n(), wf)
    second = run(db_session, FakeN8n(), wf)

    assert first.warnings == [] and second.warnings == []


# ------------------------------------------------------- detail edge cases


def test_a_failure_whose_detail_is_gone_is_still_recorded(db_session, wf):
    result = run(db_session, FakeN8n(missing=["20628"]), wf)

    row = events(db_session)["n8n:20628"]
    assert result.created == 9 and row.status == ExecutionStatus.FAILED
    assert row.node_name is None and row.error_raw is None
    assert row.error_summary == "n8n: error details unavailable"


def test_a_failure_with_oversized_data_is_recorded_without_detail(db_session, wf):
    too_large = {**make_execution(20628, "error"), "dataTooLargeToDisplay": True, "data": {}}

    run(db_session, FakeN8n(details={"20628": too_large}), wf)

    row = events(db_session)["n8n:20628"]
    assert row.status == ExecutionStatus.FAILED and row.node_name is None and row.error_raw is None
    assert row.error_summary == "n8n: error details unavailable (data too large)"


def test_a_crashed_execution_is_failed_with_a_crash_marker(db_session, wf):
    crashed = make_execution(30, "crashed")

    run(db_session, FakeN8n([crashed], details={"30": error_detail(crashed, name="Error", message="out of memory")}), wf)

    row = events(db_session)["n8n:30"]
    assert row.status == ExecutionStatus.FAILED
    assert row.error_summary == "n8n: crashed - Error: out of memory"


def test_waiting_new_and_unknown_executions_map_and_stay_open(db_session, wf):
    run(db_session, FakeN8n([
        make_execution(40, "waiting", stopped="2026-10-30T10:00:05.000Z"),
        make_execution(41, "new", started=None, stopped=None),
        make_execution(42, None, stopped="2026-10-30T10:00:05.000Z"),
    ]), wf)

    rows = events(db_session)
    assert rows["n8n:40"].status == ExecutionStatus.WAITING and rows["n8n:40"].finished_at is None
    assert rows["n8n:41"].status == ExecutionStatus.RUNNING and rows["n8n:41"].started_at is None
    assert rows["n8n:42"].status == ExecutionStatus.UNKNOWN


def test_an_event_id_owned_by_another_workflow_is_skipped_not_fatal(db_session, wf):
    other = Workflow(name="Other", source="n8n", external_id="wf_other")
    db_session.add(other)
    db_session.commit()
    db_session.add(ExecutionEvent(workflow_id=other.id, external_event_id="n8n:20637", status=ExecutionStatus.SUCCESS))
    db_session.commit()

    result = run(db_session, FakeN8n(), wf)

    assert result.skipped == 1 and result.created == 8
    assert any("20637" in w and "another FlowOps workflow" in w for w in result.warnings)


def test_an_execution_with_a_bad_shape_is_skipped_not_fatal(db_session, wf):
    odd = make_execution(50, "success")
    odd["mode"] = None
    no_id = {k: v for k, v in make_execution(51, "success").items() if k != "id"}

    result = run(db_session, FakeN8n([no_id, odd]), wf)

    assert result.created == 1 and result.skipped == 1


# ----------------------------------------------- n8n-side workflow settings


def test_saving_disabled_in_n8n_is_warned_about(db_session, wf):
    settings = {"saveDataErrorExecution": "none", "saveDataSuccessExecution": "none"}
    fake = FakeN8n(workflow={**load("F5_workflow.json"), "settings": settings})

    result = run(db_session, fake, wf)

    assert any("FAILED executions" in w for w in result.warnings)
    assert any("SUCCESSFUL executions" in w for w in result.warnings)


def test_default_or_all_save_settings_raise_no_warning(db_session, wf):
    for settings in ({}, {"saveDataErrorExecution": "DEFAULT", "saveDataSuccessExecution": "all"}):
        fake = FakeN8n([], workflow={**load("F5_workflow.json"), "settings": settings})
        assert run(db_session, fake, wf).warnings == []


def test_a_workflow_n8n_does_not_have_is_skipped_with_a_warning(db_session, wf):
    fake = FakeN8n(workflow=None)

    result = run(db_session, fake, wf)

    assert any("no workflow with this id" in w for w in result.warnings)
    assert fake.list_calls() == 0 and count(db_session) == 0


# ============================================================ sync_all


def test_only_active_n8n_workflows_with_an_external_id_are_synced(db_session, wf):
    db_session.add_all([
        Workflow(name="inactive", source="n8n", external_id="wf_fixture_002", is_active=False),
        Workflow(name="other source", source="zapier", external_id="wf_fixture_003"),
        Workflow(name="no external id", source="n8n"),
    ])
    db_session.commit()

    assert [w.external_id for w in registered_workflows(db_session)] == [WF]


def test_unregistered_active_n8n_workflows_are_reported_not_imported(db_session, wf):
    db_session.add_all([
        Workflow(name="inactive", source="n8n", external_id="wf_fixture_002", is_active=False),
        Workflow(name="other source", source="zapier", external_id="wf_fixture_003"),
    ])
    db_session.commit()
    fake = FakeN8n()

    report = sync_all(db_session, fake, now=NOW)

    unregistered = {u["id"]: u["name"] for u in report.unregistered}
    assert unregistered == {"wf_fixture_003": "Example Workflow 3", "wf_fixture_004": "Example Workflow 4"}
    assert [w.external_id for w in report.workflows] == [WF]
    assert {c[1] for c in fake.calls if c[0] in ("workflow", "list")} == {WF}  # the inactive one is not polled
    assert db_session.scalar(select(func.count()).select_from(Workflow)) == 3  # nothing auto-created
    assert report.totals == {"created": 9, "updated": 0, "unchanged": 0, "skipped": 0}


def test_an_auth_failure_aborts_the_whole_run(db_session, wf):
    class Rejected(FakeN8n):
        def list_executions(self, *args, **kwargs):
            raise N8nAuthError("n8n rejected the API key (HTTP 401) for GET /executions")

    with pytest.raises(N8nAuthError):
        sync_all(db_session, Rejected(), now=NOW)


def test_a_network_failure_aborts_the_run_but_keeps_what_was_stored(db_session, wf):
    class Flaky(FakeN8n):
        def get_execution(self, *args, **kwargs):
            raise N8nUnavailable("GET /executions/20628 failed: ReadTimeout")

    with pytest.raises(N8nUnavailable):
        sync_all(db_session, Flaky(), now=NOW)
    assert set(events(db_session)) == set()  # 20628, the oldest, was the one that failed

    result = sync_all(db_session, FakeN8n(), now=NOW)
    assert result.totals["created"] == 9


# ================================================ data minimisation, end to end

MARKERS = ("CRED-MARKER", "PARAM-MARKER", "RUNDATA-MARKER", "PINDATA-MARKER",
           "WORKFLOWDATA-MARKER", "CUSTOMDATA-MARKER", "LASTNODE-MARKER")


def dirty_failure():
    failed = make_execution(60, "error")
    detail = error_detail(
        failed, name="NodeApiError", message="boom", httpCode="500",
        node={"name": "Real Node", "credentials": {"a": "CRED-MARKER"}, "parameters": {"b": "PARAM-MARKER"}},
    )
    detail["data"]["resultData"].update(
        runData={"x": "RUNDATA-MARKER"}, pinData={"y": "PINDATA-MARKER"}, lastNodeExecuted="LASTNODE-MARKER")
    detail["workflowData"] = "WORKFLOWDATA-MARKER"
    detail["customData"] = "CUSTOMDATA-MARKER"
    return failed, detail


def test_no_run_data_reaches_the_database_or_the_log(db_session, wf, caplog):
    failed, detail = dirty_failure()

    with caplog.at_level(logging.DEBUG):
        run(db_session, FakeN8n([failed], details={"60": detail}), wf)

    stored = repr(db_session.execute(text("select * from execution_events")).all())
    stored += repr(db_session.execute(text("select * from workflows")).all())
    for marker in MARKERS:
        assert marker not in stored and marker not in caplog.text
    assert events(db_session)["n8n:60"].node_name == "Real Node"


def test_through_the_real_client_only_the_failure_is_fetched_with_data_and_nothing_leaks(db_session, wf, caplog):
    """The real N8nClient on a mock transport: every request, in order, GET only."""
    key = "KEY-SECRET-DO-NOT-LOG-123"
    seen: list[httpx2.Request] = []
    cursor_f1 = load("F1_list_page1.json")["nextCursor"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path, params = request.url.path, request.url.params
        if path == "/api/v1/workflows/wf_fixture_001":
            return httpx2.Response(200, json=load("F5_workflow.json"))
        if path == "/api/v1/workflows":
            return httpx2.Response(200, json=load("F5_active_list.json"))
        if path == "/api/v1/executions":
            page = "F2_list_page2.json" if params.get("cursor") == cursor_f1 else "F1_list_page1.json"
            return httpx2.Response(200, json=load(page))
        if path == "/api/v1/executions/20628":
            return httpx2.Response(200, json=load("F3_error_with_data.json"))
        return httpx2.Response(404)

    client = N8nClient("https://n8n.example.com", key, transport=httpx2.MockTransport(handler))

    with caplog.at_level(logging.DEBUG):
        report = sync_all(db_session, client, now=NOW)

    assert report.totals["created"] == 9
    assert {r.method for r in seen} == {"GET"}
    with_data = [r for r in seen if "includeData" in str(r.url)]
    assert [r.url.path for r in with_data] == ["/api/v1/executions/20628"]
    assert all("ignoreDataSizeLimit" not in str(r.url) and "redactExecutionData" not in str(r.url) for r in seen)
    list_requests = [r for r in seen if r.url.path == "/api/v1/executions"]
    assert len(list_requests) == 2 and all("includeData" not in str(r.url) for r in list_requests)
    assert key not in caplog.text and key not in repr(report)
