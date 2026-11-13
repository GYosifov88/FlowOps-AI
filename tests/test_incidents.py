"""Incident grouping: the ingest hook, the race, rebuild, constraints and the read API."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

import app.services.incidents as incidents_service
import app.signature as signature
from app.models import ExecutionEvent, ExecutionStatus, Incident, Workflow
from app.schemas import EventCreate
from app.services.events import ingest_event
from app.services.incidents import RebuildResult, rebuild_all
from app.services.n8n_sync import sync_workflow
from tests.n8n_helpers import WF, FakeN8n, error_detail, make_execution

T0 = datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)
CRED = 'The credential "Example-marketing" needs to be reconnected.'


@pytest.fixture
def wf(db_session):
    workflow = Workflow(name="Example Workflow", source="n8n", external_id=WF)
    db_session.add(workflow)
    db_session.commit()
    return workflow


@pytest.fixture
def wf_b(db_session):
    workflow = Workflow(name="Other Workflow", source="n8n", external_id="wf_other")
    db_session.add(workflow)
    db_session.commit()
    return workflow


def fail(db, workflow, ext, message="boom 42", node="HTTP", minutes=0, status=ExecutionStatus.FAILED, **extra):
    data = EventCreate(
        workflow_id=workflow.id, external_event_id=ext, status=status,
        started_at=T0 + timedelta(minutes=minutes), node_name=node, error_raw=message, **extra,
    )
    return ingest_event(db, data)


def incidents(db) -> list[Incident]:
    db.expire_all()
    return list(db.scalars(select(Incident).order_by(Incident.id)))


def event(db, ext) -> ExecutionEvent:
    db.expire_all()
    return db.scalar(select(ExecutionEvent).where(ExecutionEvent.external_event_id == ext))


# ====================================================================== the ingest hook


def test_failed_events_with_the_same_problem_share_one_incident(db_session, wf):
    fail(db_session, wf, "e1", "Request failed 429 at 10:00 id 12345")
    fail(db_session, wf, "e2", "Request failed 429 at 10:00 id 99999")

    (incident,) = incidents(db_session)
    assert event(db_session, "e1").incident_id == event(db_session, "e2").incident_id == incident.id
    assert incident.signature_version == "v1" and incident.status.value == "open"


def test_a_different_message_node_or_workflow_is_a_different_incident(db_session, wf, wf_b):
    fail(db_session, wf, "e1", "boom")
    fail(db_session, wf, "e2", "bang")
    fail(db_session, wf, "e3", "boom", node="Other node")
    fail(db_session, wf_b, "e4", "boom")
    assert len(incidents(db_session)) == 4


def test_incident_keeps_the_readable_parts(db_session, wf):
    fail(db_session, wf, "e1", 'Cannot read "x" 99', node=None)
    (incident,) = incidents(db_session)
    assert (incident.node_name, incident.error_name, incident.workflow_id) == (None, None, wf.id)
    assert incident.normalized_message == 'cannot read "<q>" <n>'


def test_the_incident_is_created_in_the_same_commit_as_the_event(db_session, wf):
    result = fail(db_session, wf, "e1")
    assert result.outcome == "created" and result.event.incident_id is not None
    assert db_session.scalar(select(func.count()).select_from(Incident)) == 1


@pytest.mark.parametrize("status", [s for s in ExecutionStatus if s != ExecutionStatus.FAILED])
def test_only_failed_events_get_an_incident(db_session, wf, status):
    result = fail(db_session, wf, "e1", status=status)
    assert result.event.incident_id is None
    assert incidents(db_session) == []


def test_running_then_failed_gets_its_incident_when_it_updates(db_session, wf):
    first = fail(db_session, wf, "e1", None, status=ExecutionStatus.RUNNING)
    assert first.event.incident_id is None

    done = ingest_event(db_session, EventCreate(
        workflow_id=wf.id, external_event_id="e1", status="failed", started_at=T0,
        finished_at=T0 + timedelta(seconds=3), node_name="HTTP", error_raw="boom 42"))

    assert done.outcome == "updated" and done.event.id == first.event.id
    assert done.event.incident_id is not None
    direct = fail(db_session, wf, "e2", "boom 77")  # a same-problem event that arrived already failed
    assert direct.event.incident_id == done.event.incident_id


def test_a_terminal_event_keeps_its_incident_when_sent_again(db_session, wf):
    first = fail(db_session, wf, "e1", "boom")
    again = fail(db_session, wf, "e1", "completely different text")
    assert again.outcome == "unchanged"
    assert again.event.incident_id == first.event.incident_id
    assert len(incidents(db_session)) == 1


def test_events_without_an_external_id_group_too(db_session, wf):
    fail(db_session, wf, None, "boom")
    fail(db_session, wf, None, "boom")
    assert len(incidents(db_session)) == 1
    assert db_session.scalar(select(func.count()).select_from(ExecutionEvent)) == 2


def test_secrets_are_redacted_in_the_stored_readable_parts(db_session, wf):
    fail(db_session, wf, "e1", "Upstream said no. Authorization: Bearer SECRET-VALUE-123456789 rejected")
    (incident,) = incidents(db_session)
    assert "SECRET-VALUE" not in incident.normalized_message
    assert "[redacted]" in incident.normalized_message


def test_a_missing_node_is_a_different_incident_than_a_named_one(db_session, wf):
    fail(db_session, wf, "e1", "boom", node=None)
    fail(db_session, wf, "e2", "boom", node="HTTP")
    assert len(incidents(db_session)) == 2


# ---------------------------------------------------------------- through the real sync


def test_the_n8n_sync_groups_the_real_failure_by_its_own_node(db_session, wf):
    sync_workflow(db_session, FakeN8n(), wf, now=datetime(2026, 10, 30, 12, 0, 0))

    (incident,) = incidents(db_session)
    assert incident.node_name == "SHEETS — Load Settings"
    assert incident.error_name == "NodeApiError"
    assert incident.normalized_message == 'the credential "<q>" needs to be reconnected'
    failed = db_session.scalars(select(ExecutionEvent).where(ExecutionEvent.status == ExecutionStatus.FAILED)).all()
    assert [e.incident_id for e in failed] == [incident.id]
    others = db_session.scalars(select(ExecutionEvent).where(ExecutionEvent.status != ExecutionStatus.FAILED)).all()
    assert others and all(e.incident_id is None for e in others)


def test_a_sync_run_that_sees_a_running_execution_fail_attaches_it(db_session, wf):
    running = make_execution(30001, status=None, started="2026-11-01T10:00:00.000Z", stopped=None)
    sync_workflow(db_session, FakeN8n(executions=[running]), wf, now=datetime(2026, 11, 1, 10, 5))
    assert event(db_session, "n8n:30001").incident_id is None

    failed = make_execution(30001, status="error", started="2026-11-01T10:00:00.000Z",
                            stopped="2026-11-01T10:00:09.000Z")
    detail = error_detail(failed, name="NodeApiError", message=CRED, node={"name": "SHEETS — Load Settings"})
    fake = FakeN8n(executions=[failed], details={"30001": detail})
    result = sync_workflow(db_session, fake, wf, now=datetime(2026, 11, 1, 10, 10))

    assert result.updated == 1
    (incident,) = incidents(db_session)
    assert event(db_session, "n8n:30001").incident_id == incident.id


# ====================================================================== the race


def test_losing_the_race_for_a_new_incident_signature_retries_and_joins_the_winner(db_session, wf, monkeypatch):
    fail(db_session, wf, "e1", "boom")  # the winner: its incident already exists
    (winner,) = incidents(db_session)

    real = incidents_service._find_by_signature
    calls = []

    def blind_once(db, sig):
        calls.append(sig)
        return None if len(calls) == 1 else real(db, sig)  # the first lookup missed the winner

    monkeypatch.setattr(incidents_service, "_find_by_signature", blind_once)
    result = fail(db_session, wf, "e2", "boom")

    assert len(calls) == 2  # first attempt hit the UNIQUE constraint, the retry found the winner
    assert result.outcome == "created"
    assert result.event.incident_id == winner.id
    assert len(incidents(db_session)) == 1
    assert db_session.scalar(select(func.count()).select_from(ExecutionEvent)) == 2


def test_a_failed_attempt_leaves_no_half_written_event(db_session, wf, monkeypatch):
    """If the incident cannot be stored, the event is not stored either (one transaction)."""
    fail(db_session, wf, "e1", "boom")
    monkeypatch.setattr(incidents_service, "_find_by_signature", lambda db, sig: None)  # always blind

    with pytest.raises(IntegrityError):
        fail(db_session, wf, "e2", "boom")

    monkeypatch.undo()
    assert event(db_session, "e2") is None
    assert db_session.scalar(select(func.count()).select_from(ExecutionEvent)) == 1


# ====================================================================== rebuild


def snapshot(db):
    db.expire_all()
    return (
        sorted((i.signature, i.signature_version, i.node_name, i.normalized_message) for i in db.scalars(select(Incident))),
        sorted((e.external_event_id, e.incident.signature if e.incident else None)
               for e in db.scalars(select(ExecutionEvent))),
    )


@pytest.fixture
def populated(db_session, wf, wf_b):
    fail(db_session, wf, "e1", "boom 11")
    fail(db_session, wf, "e2", "boom 22")
    fail(db_session, wf, "e3", "bang", node=None)
    fail(db_session, wf_b, "e4", "boom 33")
    fail(db_session, wf, "e5", status=ExecutionStatus.SUCCESS)
    return db_session


def test_rebuild_is_idempotent(populated):
    before = snapshot(populated)
    first = rebuild_all(populated)
    second = rebuild_all(populated)

    assert snapshot(populated) == before
    assert first == second == RebuildResult(failed_events=4, incidents_created=0, incidents_removed=0, incidents_total=3)


def test_rebuild_repairs_missing_and_wrong_assignments(populated):
    before = snapshot(populated)
    populated.execute(text("UPDATE execution_events SET incident_id = NULL WHERE external_event_id = 'e1'"))
    populated.execute(text(
        "UPDATE execution_events SET incident_id = (SELECT id FROM incidents LIMIT 1) WHERE external_event_id = 'e5'"))
    populated.commit()

    rebuild_all(populated)

    assert snapshot(populated) == before  # e1 re-attached, the success event detached again


def test_a_new_signature_version_reassigns_cleanly_and_removes_old_incidents(populated, monkeypatch):
    before = snapshot(populated)
    old_signatures = {s[0] for s in before[0]}

    monkeypatch.setattr(signature, "SIGNATURE_VERSION", "v2")
    result = rebuild_all(populated)

    after = snapshot(populated)
    assert result == RebuildResult(failed_events=4, incidents_created=3, incidents_removed=3, incidents_total=3)
    assert {s[1] for s in after[0]} == {"v2"}
    assert not old_signatures & {s[0] for s in after[0]}
    assert all(sig is not None for ext, sig in after[1] if ext != "e5")
    assert dict(after[1])["e5"] is None
    assert populated.scalar(select(func.count()).select_from(ExecutionEvent)) == 5  # history untouched

    monkeypatch.undo()  # and back to v1: the original signatures return
    rebuild_all(populated)
    assert snapshot(populated) == before


def test_rebuild_on_an_empty_database(db_session):
    assert rebuild_all(db_session) == RebuildResult(0, 0, 0, 0)


def test_the_rebuild_script_runs_against_the_current_engine(populated, engine, monkeypatch, capsys):
    import app.database as database
    import scripts.rebuild_incidents as script

    monkeypatch.setattr(database, "engine", engine)
    assert script.main() == 0
    assert "4 failed events -> 3 incidents (0 created, 0 removed)" in capsys.readouterr().out


# ====================================================================== constraints


def test_signature_is_unique(populated):
    with pytest.raises(IntegrityError):
        populated.execute(text(
            "INSERT INTO incidents (signature, signature_version, workflow_id, normalized_message, status, created_at) "
            "SELECT signature, 'v1', workflow_id, 'x', 'open', created_at FROM incidents LIMIT 1"))
        populated.commit()


def test_the_database_rejects_an_unknown_incident_status(populated):
    populated.execute(text("UPDATE incidents SET status = 'open' WHERE id = 1"))  # control: valid value is fine
    populated.commit()
    with pytest.raises(IntegrityError):
        populated.execute(text("UPDATE incidents SET status = 'closed' WHERE id = 1"))
        populated.commit()


def test_an_incident_with_events_cannot_be_deleted(populated):
    with pytest.raises(IntegrityError):
        populated.execute(text("DELETE FROM incidents WHERE id = 1"))
        populated.commit()


# ====================================================================== read API


@pytest.fixture
def api_workflow(client):
    return client.post("/api/workflows", json={"name": "W", "source": "n8n"}).json()["id"]


@pytest.fixture
def post_failed(client, auth_headers, api_workflow):
    def _post(ext, message="boom 1", node="HTTP", minutes=0, workflow_id=None, status="failed"):
        started = (T0 + timedelta(minutes=minutes)).isoformat()
        response = client.post("/api/events", headers=auth_headers, json={
            "workflow_id": workflow_id or api_workflow, "external_event_id": ext, "status": status,
            "started_at": started, "node_name": node, "error_raw": message})
        assert response.status_code in (200, 201), response.text
        return response.json()

    return _post


def test_posting_a_failed_event_returns_its_incident_id(post_failed):
    body = post_failed("e1", message="boom 11")
    assert isinstance(body["event"]["incident_id"], int)
    assert post_failed("e2", message="boom 22")["event"]["incident_id"] == body["event"]["incident_id"]
    assert post_failed("e3", status="success")["event"]["incident_id"] is None


def test_running_then_failed_through_the_api_gets_an_incident(post_failed):
    assert post_failed("e1", status="running")["event"]["incident_id"] is None
    done = post_failed("e1", status="failed")
    assert done["result"] == "updated" and done["event"]["incident_id"] is not None


def test_list_incidents_with_counters_computed_from_the_events(client, post_failed):
    for i in range(3):
        post_failed(f"a{i}", "Alpha 1", minutes=i)
    post_failed("b0", "Beta", minutes=10)

    body = client.get("/api/incidents").json()

    assert body["total"] == 2 and (body["limit"], body["offset"]) == (50, 0)
    beta, alpha = body["items"]  # most recently seen first
    assert (beta["occurrence_count"], beta["normalized_message"]) == (1, "beta")
    assert alpha["occurrence_count"] == 3
    assert alpha["first_seen"] == "2026-11-01T09:00:00Z" and alpha["last_seen"] == "2026-11-01T09:02:00Z"
    assert set(alpha) == {
        "id", "signature", "signature_version", "workflow_id", "node_name", "error_name",
        "normalized_message", "status", "occurrence_count", "first_seen", "last_seen", "created_at",
    }
    assert "error_raw" not in client.get("/api/incidents").text


def test_counters_follow_the_events_and_never_drift(client, post_failed, db_session):
    post_failed("a0", "Alpha")
    post_failed("a1", "Alpha", minutes=5)
    incident_id = client.get("/api/incidents").json()["items"][0]["id"]
    assert client.get(f"/api/incidents/{incident_id}").json()["occurrence_count"] == 2

    db_session.execute(text("UPDATE execution_events SET incident_id = NULL WHERE external_event_id = 'a1'"))
    db_session.commit()
    assert client.get(f"/api/incidents/{incident_id}").json()["occurrence_count"] == 1


def test_an_event_without_a_start_time_counts_by_its_creation_time(client, auth_headers, api_workflow):
    client.post("/api/events", headers=auth_headers, json={
        "workflow_id": api_workflow, "status": "failed", "error_raw": "no start"})
    (item,) = client.get("/api/incidents").json()["items"]
    assert item["first_seen"] == item["last_seen"] and item["occurrence_count"] == 1


def test_list_filters_and_paging(client, post_failed):
    other = client.post("/api/workflows", json={"name": "W2", "source": "n8n"}).json()["id"]
    post_failed("a", "Alpha", minutes=1)
    post_failed("b", "Beta", minutes=2)
    post_failed("c", "Gamma", minutes=3, workflow_id=other)

    assert client.get("/api/incidents").json()["total"] == 3
    only = client.get(f"/api/incidents?workflow_id={other}").json()
    assert only["total"] == 1 and only["items"][0]["normalized_message"] == "gamma"
    assert client.get("/api/incidents?workflow_id=999").json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert client.get("/api/incidents?status=open").json()["total"] == 3
    assert client.get("/api/incidents?status=closed").status_code == 422

    page = client.get("/api/incidents?limit=1&offset=1").json()
    assert page["total"] == 3 and [i["normalized_message"] for i in page["items"]] == ["beta"]
    assert client.get("/api/incidents?limit=201").status_code == 422
    assert client.get("/api/incidents?limit=0").status_code == 422
    assert client.get("/api/incidents?offset=-1").status_code == 422


def test_detail_has_the_ten_newest_events_and_no_error_raw(client, post_failed):
    for i in range(12):
        post_failed(f"e{i}", "Alpha", minutes=i)
    incident_id = client.get("/api/incidents").json()["items"][0]["id"]

    response = client.get(f"/api/incidents/{incident_id}")
    body = response.json()

    assert response.status_code == 200 and body["occurrence_count"] == 12
    assert [e["external_event_id"] for e in body["recent_events"]] == [f"e{i}" for i in range(11, 1, -1)]
    assert all(e["incident_id"] == incident_id for e in body["recent_events"])
    assert "error_raw" not in response.text


def test_unknown_incident_is_a_404(client):
    response = client.get("/api/incidents/999")
    assert response.status_code == 404 and response.json()["detail"] == "Incident not found"
    assert client.get("/api/incidents/abc").status_code == 422


def test_unattributed_incident_has_a_null_node(client, post_failed):
    post_failed("e1", "Alpha", node=None)
    assert client.get("/api/incidents").json()["items"][0]["node_name"] is None


def test_empty_list(client):
    assert client.get("/api/incidents").json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
