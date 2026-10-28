import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.services.events as events_service
from app.models import ExecutionEvent, ExecutionStatus, Workflow
from app.schemas import EventCreate

URL = "/api/events"


@pytest.fixture
def workflow_id(client):
    response = client.post("/api/workflows", json={"name": "Daily report", "source": "n8n"})
    return response.json()["id"]


@pytest.fixture
def post(client, auth_headers):
    def _post(**body):
        return client.post(URL, json=body, headers=auth_headers)

    return _post


def count_events(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(ExecutionEvent))


def test_valid_event_creates_row(post, workflow_id, db_session):
    response = post(
        workflow_id=workflow_id,
        external_event_id="exec-1",
        status="failed",
        started_at="2026-10-27T09:00:00Z",
        finished_at="2026-10-27T09:00:02.500Z",
        node_name="HTTP Request",
        mode="trigger",
        error_raw="Error: boom",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["result"] == "created"
    event = body["event"]
    assert event["workflow_id"] == workflow_id
    assert event["external_event_id"] == "exec-1"
    assert event["status"] == "failed"
    assert event["started_at"] == "2026-10-27T09:00:00Z"
    assert event["duration_ms"] == 2500
    assert event["node_name"] == "HTTP Request"
    assert event["mode"] == "trigger"
    assert event["error_summary"] == "Error: boom"
    assert count_events(db_session) == 1


def test_missing_optional_fields_ok(post, workflow_id):
    response = post(workflow_id=workflow_id, status="running")

    assert response.status_code == 201
    event = response.json()["event"]
    assert event["external_event_id"] is None
    assert event["started_at"] is None
    assert event["finished_at"] is None
    assert event["duration_ms"] is None  # None, never 0
    assert event["node_name"] is None
    assert event["mode"] is None
    assert event["error_summary"] is None


def test_aware_timestamps_are_converted_to_utc(post, workflow_id, db_session):
    post(
        workflow_id=workflow_id,
        external_event_id="tz",
        status="success",
        started_at="2026-10-27T12:00:00+03:00",
        finished_at="2026-10-27T09:00:01Z",
    )

    row = db_session.scalar(select(ExecutionEvent))
    assert row.started_at.isoformat() == "2026-10-27T09:00:00"  # naive UTC
    assert row.duration_ms == 1000


def test_duplicate_non_terminal_is_noop(post, workflow_id, db_session):
    first = post(workflow_id=workflow_id, external_event_id="e", status="running",
                 started_at="2026-10-27T09:00:00Z")
    again = post(workflow_id=workflow_id, external_event_id="e", status="running",
                 started_at="2026-10-27T09:00:00Z")

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["result"] == "unchanged"
    assert again.json()["event"]["id"] == first.json()["event"]["id"]
    assert count_events(db_session) == 1


def test_non_terminal_to_other_non_terminal_is_noop(post, workflow_id, db_session):
    post(workflow_id=workflow_id, external_event_id="e", status="running")

    response = post(workflow_id=workflow_id, external_event_id="e", status="waiting")

    assert response.json()["result"] == "unchanged"
    assert response.json()["event"]["status"] == "running"  # as the spec says


def test_upsert_running_then_success_updates_row_in_place(post, workflow_id, db_session):
    first = post(
        workflow_id=workflow_id,
        external_event_id="exec-9",
        status="running",
        started_at="2026-10-27T09:00:00Z",
        node_name="Start",
    ).json()["event"]

    response = post(
        workflow_id=workflow_id,
        external_event_id="exec-9",
        status="success",
        started_at="2030-01-01T00:00:00Z",  # must NOT replace the original started_at
        finished_at="2026-10-27T09:00:05Z",
        node_name="Final node",  # must NOT replace an existing node_name
        mode="webhook",  # fills a missing mode
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "updated"
    assert count_events(db_session) == 1
    event = body["event"]
    assert event["id"] == first["id"]
    assert event["created_at"] == first["created_at"]
    assert event["status"] == "success"
    assert event["started_at"] == "2026-10-27T09:00:00Z"
    assert event["finished_at"] == "2026-10-27T09:00:05Z"
    assert event["duration_ms"] == 5000
    assert event["node_name"] == "Start"
    assert event["mode"] == "webhook"


def test_upsert_running_then_failed_carries_the_sanitized_error(post, workflow_id, db_session):
    post(workflow_id=workflow_id, external_event_id="f", status="running",
         started_at="2026-10-27T09:00:00Z")

    response = post(
        workflow_id=workflow_id,
        external_event_id="f",
        status="failed",
        finished_at="2026-10-27T09:00:01Z",
        error_raw="Auth failed: password=hunter2",
    )

    assert response.json()["result"] == "updated"
    row = db_session.scalar(select(ExecutionEvent))
    db_session.refresh(row)
    assert row.status == ExecutionStatus.FAILED
    assert row.error_raw == "Auth failed: password=hunter2"
    assert "hunter2" not in row.error_summary


def test_update_fills_a_missing_started_at_but_never_overwrites(post, workflow_id):
    post(workflow_id=workflow_id, external_event_id="s", status="running")

    event = post(
        workflow_id=workflow_id, external_event_id="s", status="success",
        started_at="2026-10-27T09:00:00Z", finished_at="2026-10-27T09:00:03Z",
    ).json()["event"]

    assert event["started_at"] == "2026-10-27T09:00:00Z"
    assert event["duration_ms"] == 3000


@pytest.mark.parametrize("terminal", ["success", "failed", "canceled"])
@pytest.mark.parametrize("incoming", ["running", "success", "failed", "canceled", "waiting"])
def test_terminal_event_is_not_overwritten(post, workflow_id, db_session, terminal, incoming):
    first = post(
        workflow_id=workflow_id, external_event_id="t", status=terminal,
        started_at="2026-10-27T09:00:00Z", finished_at="2026-10-27T09:00:04Z",
        error_raw="original",
    ).json()["event"]

    response = post(
        workflow_id=workflow_id, external_event_id="t", status=incoming,
        finished_at="2030-01-01T00:00:00Z", error_raw="replacement",
    )

    assert response.status_code == 200
    assert response.json()["result"] == "unchanged"
    assert response.json()["event"] == first
    assert db_session.scalar(select(ExecutionEvent)).error_raw == "original"
    assert count_events(db_session) == 1


def test_a_conflicting_terminal_status_is_logged(post, workflow_id, caplog):
    post(workflow_id=workflow_id, external_event_id="w", status="success")

    with caplog.at_level(logging.WARNING, logger="app.services.events"):
        post(workflow_id=workflow_id, external_event_id="w", status="failed")

    assert "already terminal as success" in caplog.text


def test_event_without_external_id_always_inserts(post, workflow_id, db_session):
    for _ in range(3):
        assert post(workflow_id=workflow_id, status="success").status_code == 201

    assert count_events(db_session) == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"workflow_id": 1},  # no status
        {"status": "success"},  # no workflow
        {"workflow_id": 1, "status": "exploded"},
        {"workflow_id": 1, "status": "error"},  # n8n's word; the mapping is M7's job
        {"workflow_id": "abc", "status": "success"},
        {"workflow_id": 1, "status": "success", "started_at": "yesterday-ish"},
        {"workflow_id": 1, "status": "success", "finished_at": "31/02/2026"},
        {"workflow_id": 1, "status": "success", "external_event_id": ""},
        {"workflow_id": 1, "status": "success", "node_name": "n" * 256},
        {"workflow_id": 1, "status": "success", "mode": "m" * 51},
        {"workflow_id": 1, "status": "success", "error_raw": "x" * 100_001},
        {"workflow_id": 1, "status": "success", "surprise": 1},
    ],
)
def test_bad_payload_returns_422(client, auth_headers, db_session, payload):
    response = client.post(URL, json=payload, headers=auth_headers)

    assert response.status_code == 422
    assert count_events(db_session) == 0


def test_invalid_json_returns_422(client, auth_headers):
    response = client.post(
        URL, content=b"{not json", headers={**auth_headers, "Content-Type": "application/json"}
    )

    assert response.status_code == 422


def test_missing_token_returns_401(client, workflow_id, db_session):
    response = client.post(URL, json={"workflow_id": workflow_id, "status": "success"})

    assert response.status_code == 401
    assert count_events(db_session) == 0


def test_wrong_token_returns_401(client, workflow_id, db_session):
    response = client.post(
        URL,
        json={"workflow_id": workflow_id, "status": "success"},
        headers={"X-FlowOps-Token": "not-the-token"},
    )

    assert response.status_code == 401
    assert count_events(db_session) == 0


def test_unknown_workflow_is_404(post, db_session):
    response = post(workflow_id=999, status="success")

    assert response.status_code == 404
    assert count_events(db_session) == 0


def test_events_for_a_deactivated_workflow_are_still_accepted(client, post, workflow_id):
    client.delete(f"/api/workflows/{workflow_id}")

    assert post(workflow_id=workflow_id, status="success").status_code == 201


def test_external_event_id_of_another_workflow_is_409(client, post, workflow_id, db_session):
    other = client.post("/api/workflows", json={"name": "B", "source": "n8n"}).json()["id"]
    post(workflow_id=workflow_id, external_event_id="shared", status="running")

    response = post(workflow_id=other, external_event_id="shared", status="success")

    assert response.status_code == 409
    row = db_session.scalar(select(ExecutionEvent))
    assert row.workflow_id == workflow_id and row.status == ExecutionStatus.RUNNING


def test_error_raw_is_stored_unmodified_but_never_returned(post, workflow_id, db_session):
    raw = "  Boom\n  Authorization: Bearer SECRETTOKEN123  \n"

    response = post(workflow_id=workflow_id, status="failed", error_raw=raw)

    assert "error_raw" not in response.json()["event"]
    assert "SECRETTOKEN123" not in response.text
    row = db_session.scalar(select(ExecutionEvent))
    assert row.error_raw == raw  # byte for byte, whitespace included
    assert "SECRETTOKEN123" not in row.error_summary
    assert row.error_summary.startswith("Boom Authorization:")


def test_response_exposes_exactly_the_documented_fields(post, workflow_id):
    event = post(workflow_id=workflow_id, status="success").json()["event"]

    assert set(event) == {
        "id", "workflow_id", "external_event_id", "status", "started_at", "finished_at",
        "duration_ms", "node_name", "mode", "error_summary", "created_at",
    }


def test_losing_an_insert_race_falls_back_to_the_existing_row(
    post, workflow_id, db_session, engine, monkeypatch
):
    """Two ingests of one new external_event_id: the loser hits the UNIQUE index."""
    post(workflow_id=workflow_id, external_event_id="race", status="running",
         started_at="2026-10-27T09:00:00Z")
    real_find = events_service._find
    calls = []

    def blind_first_time(db, external_event_id):
        calls.append(external_event_id)
        return None if len(calls) == 1 else real_find(db, external_event_id)

    monkeypatch.setattr(events_service, "_find", blind_first_time)

    response = post(workflow_id=workflow_id, external_event_id="race", status="success",
                    finished_at="2026-10-27T09:00:02Z")

    assert response.status_code == 200
    assert response.json()["result"] == "updated"
    assert len(calls) == 2
    assert count_events(db_session) == 1


def test_integrity_error_without_an_existing_row_is_not_swallowed(db_session, monkeypatch):
    db_session.add(Workflow(name="w", source="n8n"))
    db_session.commit()

    def boom(*args, **kwargs):
        raise IntegrityError("stmt", {}, Exception("some other constraint"))

    monkeypatch.setattr(events_service, "_insert", boom)

    with pytest.raises(IntegrityError):
        events_service.ingest_event(
            db_session,
            EventCreate(workflow_id=1, external_event_id="x", status=ExecutionStatus.SUCCESS),
        )
