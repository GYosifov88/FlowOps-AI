from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.models import (
    NON_TERMINAL,
    TERMINAL,
    ExecutionEvent,
    ExecutionStatus,
    Workflow,
)
from app.timeutil import compute_duration_ms, utcnow

PLUS_3 = timezone(timedelta(hours=3))


def make_workflow(session, **overrides) -> Workflow:
    workflow = Workflow(name="Nightly sync", source="n8n", **overrides)
    session.add(workflow)
    session.commit()
    return workflow


def test_workflow_with_two_events_reads_back_with_relationship(engine, db_session):
    workflow = make_workflow(db_session, description="demo", external_id="n8n-42")
    started = datetime(2026, 10, 27, 9, 0, 0)
    db_session.add_all(
        [
            ExecutionEvent(
                workflow_id=workflow.id,
                status=ExecutionStatus.SUCCESS,
                started_at=started,
                finished_at=started + timedelta(seconds=2),
                duration_ms=2000,
            ),
            ExecutionEvent(
                workflow_id=workflow.id,
                status=ExecutionStatus.FAILED,
                node_name="HTTP Request",
                mode="trigger",
                error_summary="boom",
                error_raw="Error: boom\n  at stack...",
            ),
        ]
    )
    db_session.commit()

    with Session(engine) as fresh:  # a new session, so nothing comes from the identity map
        loaded = fresh.get(Workflow, workflow.id)
        assert loaded.is_active is True
        assert loaded.external_id == "n8n-42"
        assert len(loaded.events) == 2
        assert {e.status for e in loaded.events} == {
            ExecutionStatus.SUCCESS,
            ExecutionStatus.FAILED,
        }
        failed = next(e for e in loaded.events if e.status is ExecutionStatus.FAILED)
        assert failed.workflow is loaded
        assert (failed.node_name, failed.mode) == ("HTTP Request", "trigger")
        assert failed.error_raw == "Error: boom\n  at stack..."


def test_timestamps_round_trip_as_naive_utc(engine, db_session):
    workflow = make_workflow(db_session)
    aware = datetime(2026, 10, 27, 12, 0, 0, tzinfo=PLUS_3)  # 09:00 UTC
    event = ExecutionEvent(
        workflow_id=workflow.id,
        status=ExecutionStatus.SUCCESS,
        started_at=aware,
        finished_at=datetime(2026, 10, 27, 9, 0, 3),  # naive: taken as UTC
    )
    db_session.add(event)
    db_session.commit()
    event_id = event.id

    # What SQLite actually holds, bypassing the ORM entirely.
    raw_started = db_session.execute(
        text("SELECT started_at FROM execution_events WHERE id = :id"), {"id": event_id}
    ).scalar()
    assert raw_started == "2026-10-27 09:00:00.000000"

    with Session(engine) as fresh:
        loaded = fresh.get(ExecutionEvent, event_id)
        assert loaded.started_at == datetime(2026, 10, 27, 9, 0, 0)
        assert loaded.finished_at == datetime(2026, 10, 27, 9, 0, 3)
        assert loaded.started_at.tzinfo is None


def test_created_at_defaults_to_current_naive_utc(db_session):
    workflow = make_workflow(db_session)

    assert workflow.created_at.tzinfo is None
    assert abs(utcnow() - workflow.created_at) < timedelta(seconds=5)


def test_status_is_stored_as_the_lowercase_value(db_session):
    workflow = make_workflow(db_session)
    event = ExecutionEvent(workflow_id=workflow.id, status=ExecutionStatus.FAILED)
    db_session.add(event)
    db_session.commit()

    raw = db_session.execute(
        text("SELECT status FROM execution_events WHERE id = :id"), {"id": event.id}
    ).scalar()
    assert raw == "failed"


def test_orm_rejects_an_unknown_status_string(db_session):
    workflow = make_workflow(db_session)
    db_session.add(ExecutionEvent(workflow_id=workflow.id, status="bogus"))

    with pytest.raises((LookupError, StatementError)):
        db_session.commit()


def test_database_check_constraint_rejects_an_unknown_status(db_session):
    """Raw SQL, so SQLAlchemy's own validation cannot intercept it: only the CHECK can."""
    workflow = make_workflow(db_session)
    insert = text(
        "INSERT INTO execution_events (workflow_id, status, created_at) "
        "VALUES (:wf, :status, '2026-10-27 09:00:00.000000')"
    )

    # Control: the same statement with a valid status succeeds, so a failure below is
    # attributable to the status value and nothing else.
    db_session.execute(insert, {"wf": workflow.id, "status": "success"})
    db_session.commit()

    with pytest.raises(IntegrityError, match="execution_status"):
        db_session.execute(insert, {"wf": workflow.id, "status": "bogus"})


def test_duration_ms_stays_none_when_there_is_no_finish_time(db_session):
    workflow = make_workflow(db_session)
    started = datetime(2026, 10, 27, 9, 0, 0)
    event = ExecutionEvent(
        workflow_id=workflow.id,
        status=ExecutionStatus.RUNNING,
        started_at=started,
        duration_ms=compute_duration_ms(started, None),
    )
    db_session.add(event)
    db_session.commit()

    assert event.duration_ms is None


def test_external_event_id_must_be_unique_but_nulls_never_collide(db_session):
    workflow = make_workflow(db_session)
    db_session.add_all(
        [
            ExecutionEvent(workflow_id=workflow.id, status=ExecutionStatus.SUCCESS),
            ExecutionEvent(workflow_id=workflow.id, status=ExecutionStatus.SUCCESS),
            ExecutionEvent(
                workflow_id=workflow.id,
                status=ExecutionStatus.SUCCESS,
                external_event_id="evt-1",
            ),
        ]
    )
    db_session.commit()  # two NULL ids plus one real id: fine

    db_session.add(
        ExecutionEvent(
            workflow_id=workflow.id,
            status=ExecutionStatus.FAILED,
            external_event_id="evt-1",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_hard_deleting_a_workflow_with_events_is_refused(db_session):
    workflow = make_workflow(db_session)
    db_session.add(
        ExecutionEvent(workflow_id=workflow.id, status=ExecutionStatus.SUCCESS)
    )
    db_session.commit()
    workflow_id = workflow.id

    # Through the ORM.
    db_session.delete(workflow)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # And straight at the database: ON DELETE RESTRICT.
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db_session.execute(
            text("DELETE FROM workflows WHERE id = :id"), {"id": workflow_id}
        )


def test_events_survive_soft_deleting_their_workflow(db_session):
    workflow = make_workflow(db_session)
    db_session.add(
        ExecutionEvent(workflow_id=workflow.id, status=ExecutionStatus.SUCCESS)
    )
    db_session.commit()

    workflow.is_active = False
    db_session.commit()

    assert len(workflow.events) == 1


def test_no_delete_cascade_is_configured():
    assert Workflow.events.property.cascade.delete is False


def test_schema_has_the_expected_indexes(engine):
    inspector = inspect(engine)

    pair = next(
        c for c in inspector.get_unique_constraints("workflows")
        if c["column_names"] == ["source", "external_id"]
    )
    assert pair["name"] == "uq_workflows_source_external_id"

    event_indexes = inspector.get_indexes("execution_events")
    assert any(i["column_names"] == ["workflow_id"] for i in event_indexes)


def test_terminal_and_non_terminal_partition_all_statuses():
    assert TERMINAL.isdisjoint(NON_TERMINAL)
    assert TERMINAL | NON_TERMINAL == set(ExecutionStatus)
