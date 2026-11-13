"""V2-M4: incident status lifecycle (table-driven) and regression on a new failure."""
import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    ACTIVE_INCIDENT,
    HUMAN_TRANSITIONS,
    Incident,
    IncidentStatus,
    IncidentTransition,
)
from app.services.incidents import rebuild_all
from tests.approval_helpers import fail, fake_analyzer, incident_id, move, now, status_of, workflow_id  # noqa: F401

STATES = ["open", "acknowledged", "resolved", "regressed"]


def force_status(db, incident_id, status):
    db.execute(text("UPDATE incidents SET status = :s WHERE id = :i"), {"s": status, "i": incident_id})
    db.commit()


def transitions(client, incident_id):
    return client.get(f"/api/incidents/{incident_id}/transitions").json()


# ====================================================================== the table


def test_the_rules_in_code_match_the_spec():
    assert {k.value: sorted(s.value for s in v) for k, v in HUMAN_TRANSITIONS.items()} == {
        "open": ["acknowledged"], "regressed": ["acknowledged"],
        "acknowledged": ["resolved"], "resolved": [],
    }
    assert ACTIVE_INCIDENT == {IncidentStatus.OPEN, IncidentStatus.REGRESSED}


ALLOWED = {("open", "acknowledged"), ("regressed", "acknowledged"), ("acknowledged", "resolved")}


@pytest.mark.parametrize("current", STATES)
@pytest.mark.parametrize("to", ["acknowledged", "resolved"])
def test_every_pair_is_allowed_or_a_409_that_writes_nothing(
    client, auth_headers, incident_id, db_session, current, to
):
    force_status(db_session, incident_id, current)
    response = move(client, auth_headers, incident_id, to, note="because")

    if (current, to) in ALLOWED:
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["incident_status"] == to
        assert (body["transition"]["from_status"], body["transition"]["to_status"]) == (current, to)
        assert body["transition"]["actor"] == "alice" and body["transition"]["note"] == "because"
        assert status_of(client, incident_id) == to
        assert len(transitions(client, incident_id)) == 1
    else:
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["current_status"] == current
        assert detail["allowed"] == sorted(s.value for s in HUMAN_TRANSITIONS[IncidentStatus(current)])
        assert status_of(client, incident_id) == current
        assert transitions(client, incident_id) == []


@pytest.mark.parametrize("to", ["open", "regressed", "closed", "", None, 7])
def test_people_cannot_set_open_regressed_or_anything_else(client, auth_headers, incident_id, to):
    assert move(client, auth_headers, incident_id, to).status_code == 422
    assert status_of(client, incident_id) == "open"
    assert transitions(client, incident_id) == []


def test_the_happy_path_and_its_history(client, auth_headers, incident_id):
    assert move(client, auth_headers, incident_id, "acknowledged", actor="bob", note="looking").status_code == 201
    assert move(client, auth_headers, incident_id, "resolved", actor="bob", note="reconnected").status_code == 201
    history = transitions(client, incident_id)
    assert [(h["from_status"], h["to_status"]) for h in history] == [("open", "acknowledged"), ("acknowledged", "resolved")]
    assert history[0]["created_at"].endswith("Z") and history[0]["event_id"] is None
    # a second resolve, or going back, is refused
    assert move(client, auth_headers, incident_id, "resolved").status_code == 409
    assert move(client, auth_headers, incident_id, "acknowledged").status_code == 409


# ====================================================================== auth and input


def test_mutations_need_the_token_before_anything_else(client, incident_id, db_session):
    for headers in ({}, {"X-FlowOps-Token": "wrong"}):
        assert client.post(f"/api/incidents/{incident_id}/transitions", headers=headers, json={"to": "acknowledged", "actor": "a"}).status_code == 401
        assert client.post("/api/incidents/999/transitions", headers=headers, json={}).status_code == 401  # beats 404 and 422
    assert status_of(client, incident_id) == "open"
    assert db_session.scalar(select(func.count()).select_from(IncidentTransition)) == 0


def test_reads_need_no_token(client, incident_id):
    assert client.get(f"/api/incidents/{incident_id}/transitions").status_code == 200
    assert client.get(f"/api/incidents/{incident_id}/confirmations").status_code == 200


def test_unknown_incident_is_404(client, auth_headers):
    assert move(client, auth_headers, 999, "acknowledged").status_code == 404
    assert client.get("/api/incidents/999/transitions").status_code == 404


@pytest.mark.parametrize("body", [
    {"to": "acknowledged"},                                   # no actor
    {"to": "acknowledged", "actor": ""},
    {"to": "acknowledged", "actor": "   "},
    {"to": "acknowledged", "actor": "x" * 101},
    {"to": "acknowledged", "actor": "<script>"},
    {"to": "acknowledged", "actor": "a", "note": "n" * 1001},
    {"to": "acknowledged", "actor": "a", "extra": 1},
])
def test_bad_input_is_422_and_changes_nothing(client, auth_headers, incident_id, body):
    assert client.post(f"/api/incidents/{incident_id}/transitions", headers=auth_headers, json=body).status_code == 422
    assert status_of(client, incident_id) == "open"


def test_notes_are_redacted_and_capped_and_actors_stripped(client, auth_headers, incident_id):
    response = move(client, auth_headers, incident_id, "acknowledged", actor="  carol  ",
                    note="token is Bearer abcdefghijklmnopqrstuvwxyz0123456789 ok")
    transition = response.json()["transition"]
    assert transition["actor"] == "carol"
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in transition["note"]


def test_logs_never_carry_the_actor_or_the_note(client, auth_headers, incident_id, caplog):
    caplog.set_level(logging.DEBUG)
    move(client, auth_headers, incident_id, "acknowledged", actor="zed-the-actor", note="secret-looking-note")
    assert "zed-the-actor" not in caplog.text and "secret-looking-note" not in caplog.text
    assert "acknowledged" in caplog.text  # ids and statuses are logged


def test_a_stale_status_loses_the_race_with_a_409(client, auth_headers, incident_id, db_session, monkeypatch):
    """Someone else changes the status between our read and our UPDATE: nothing is written."""
    import app.services.approval as approval

    real = approval.update

    def update_after_a_rival_moved_it(model):
        force_status(db_session, incident_id, "resolved")  # the rival wins first
        return real(model)

    monkeypatch.setattr(approval, "update", update_after_a_rival_moved_it)
    response = move(client, auth_headers, incident_id, "acknowledged")
    assert response.status_code == 409 and response.json()["detail"]["current_status"] == "resolved"
    assert db_session.scalar(select(func.count()).select_from(IncidentTransition)) == 0


# ====================================================================== regression


def resolve(client, headers, incident_id):
    assert move(client, headers, incident_id, "acknowledged").status_code == 201
    assert move(client, headers, incident_id, "resolved").status_code == 201


def test_a_new_failure_after_the_resolve_regresses_the_incident(client, auth_headers, incident_id, fail):
    resolve(client, auth_headers, incident_id)
    body = fail("e2", started=now() + timedelta(minutes=1))
    assert body["event"]["incident_id"] == incident_id
    assert status_of(client, incident_id) == "regressed"
    last = transitions(client, incident_id)[-1]
    assert (last["from_status"], last["to_status"], last["actor"]) == ("resolved", "regressed", "flowops")
    assert last["event_id"] == body["event"]["id"]


def test_a_failure_that_started_before_the_resolve_is_attached_without_reopening(
    client, auth_headers, incident_id, fail
):
    """The n8n sync re-reads a 24 h window, so late finishers arrive after a human resolved."""
    resolve(client, auth_headers, incident_id)
    body = fail("late", started=now() - timedelta(hours=1))
    assert body["event"]["incident_id"] == incident_id
    assert status_of(client, incident_id) == "resolved"
    assert [t["to_status"] for t in transitions(client, incident_id)] == ["acknowledged", "resolved"]
    assert client.get(f"/api/incidents/{incident_id}").json()["occurrence_count"] == 2


def test_a_failure_exactly_at_the_resolve_time_does_not_regress(client, auth_headers, incident_id, fail, db_session):
    resolve(client, auth_headers, incident_id)
    resolved_at = db_session.scalar(
        select(IncidentTransition.created_at).where(IncidentTransition.to_status == IncidentStatus.RESOLVED)
    )
    fail("tie", started=resolved_at)  # naive UTC is read as UTC
    assert status_of(client, incident_id) == "resolved"
    fail("after", started=resolved_at + timedelta(seconds=1))
    assert status_of(client, incident_id) == "regressed"


@pytest.mark.parametrize("current", ["open", "acknowledged", "regressed"])
def test_a_failure_on_an_unresolved_incident_changes_nothing(client, auth_headers, incident_id, fail, db_session, current):
    force_status(db_session, incident_id, current)
    fail("e2", started=now() + timedelta(minutes=1))
    assert status_of(client, incident_id) == current
    assert transitions(client, incident_id) == []


def test_running_then_failed_after_the_resolve_regresses(client, auth_headers, incident_id, fail):
    resolve(client, auth_headers, incident_id)
    started = now() + timedelta(minutes=1)
    fail("slow", started=started, status="running", message="")
    assert status_of(client, incident_id) == "resolved"  # not failed yet, no incident
    fail("slow", started=started, status="failed")
    assert status_of(client, incident_id) == "regressed"


def test_a_redelivered_event_does_not_regress_twice(client, auth_headers, incident_id, fail):
    resolve(client, auth_headers, incident_id)
    fail("e2", started=now() + timedelta(minutes=1))
    assert fail("e2", started=now() + timedelta(minutes=1))["result"] == "unchanged"
    assert [t["to_status"] for t in transitions(client, incident_id)].count("regressed") == 1


def test_a_success_never_regresses(client, auth_headers, incident_id, fail):
    resolve(client, auth_headers, incident_id)
    fail("ok1", started=now() + timedelta(minutes=1), status="success")
    assert status_of(client, incident_id) == "resolved"


def test_the_cycle_can_repeat(client, auth_headers, incident_id, fail):
    resolve(client, auth_headers, incident_id)
    fail("e2", started=now() + timedelta(minutes=1))
    assert status_of(client, incident_id) == "regressed"
    resolve(client, auth_headers, incident_id)  # regressed -> acknowledged -> resolved
    fail("e3", started=now() + timedelta(minutes=2))
    assert status_of(client, incident_id) == "regressed"
    assert [t["to_status"] for t in transitions(client, incident_id)] == [
        "acknowledged", "resolved", "regressed", "acknowledged", "resolved", "regressed"]


def test_regressed_counts_as_open_but_acknowledged_does_not(client, auth_headers, incident_id, fail, db_session):
    resolve(client, auth_headers, incident_id)
    fail("e2", started=now() + timedelta(minutes=1))
    db_session.expire_all()
    assert db_session.get(Incident, incident_id).status in ACTIVE_INCIDENT
    assert client.get("/api/incidents?status=regressed").json()["total"] == 1


def test_the_regression_rolls_back_with_its_event(client, auth_headers, incident_id, workflow_id, monkeypatch):
    """If the request fails after the regression, neither the status change nor its history survives."""
    resolve(client, auth_headers, incident_id)
    import app.services.events as events_module

    real_assign = events_module.assign_incident

    def assign_then_fail(db, event):
        real_assign(db, event)
        raise RuntimeError("after the regression, before the commit")

    monkeypatch.setattr(events_module, "assign_incident", assign_then_fail)
    response = client.post("/api/events", headers=auth_headers, json={
        "workflow_id": workflow_id, "external_event_id": "doomed", "status": "failed",
        "started_at": (now() + timedelta(minutes=1)).isoformat(), "node_name": "HTTP", "error_raw": "boom 1"})
    assert response.status_code == 500
    monkeypatch.undo()
    assert status_of(client, incident_id) == "resolved"
    assert [t["to_status"] for t in transitions(client, incident_id)] == ["acknowledged", "resolved"]


def test_rebuild_never_regresses_anything(client, auth_headers, incident_id, fail, db_session):
    resolve(client, auth_headers, incident_id)
    fail("late", started=now() - timedelta(hours=1))
    rebuild_all(db_session)
    assert status_of(client, incident_id) == "resolved"


# ====================================================================== schema, analysis dedup


def test_the_database_still_rejects_an_unknown_status_and_knows_the_new_ones(incident_id, db_session):
    for status in STATES:
        force_status(db_session, incident_id, status)
    with pytest.raises(IntegrityError):
        force_status(db_session, incident_id, "closed")
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO incident_transitions (incident_id, from_status, to_status, actor, created_at) "
            "VALUES (:i, 'open', 'closed', 'x', '2026-01-01')"), {"i": incident_id})
        db_session.commit()


def test_acknowledging_does_not_change_the_context_hash_so_analysis_is_still_reused(
    client, auth_headers, incident_id, fake_analyzer
):
    before = client.get(f"/api/incidents/{incident_id}/context").json()
    assert "status" not in before["context"]["trusted"]["incident"] and before["context_version"] == 2
    assert client.post(f"/api/incidents/{incident_id}/analyze", headers=auth_headers).status_code == 201
    move(client, auth_headers, incident_id, "acknowledged")
    after = client.get(f"/api/incidents/{incident_id}/context").json()
    assert after["context_hash"] == before["context_hash"]
    again = client.post(f"/api/incidents/{incident_id}/analyze", headers=auth_headers)
    assert again.status_code == 200 and again.json()["result"] == "reused"
    assert len(fake_analyzer.calls) == 1
