from sqlalchemy import func, select

from app.models import ExecutionEvent, ExecutionStatus, Workflow
from scripts import seed

DISTINCT_EVENTS = len({e["external_event_id"] for e in seed.EVENTS})


def counts(db_session):
    db_session.expire_all()
    return (
        db_session.scalar(select(func.count()).select_from(Workflow)),
        db_session.scalar(select(func.count()).select_from(ExecutionEvent)),
    )


def test_first_run_creates_everything_and_exercises_the_upsert(client, db_session, auth_headers):
    results = seed.run_seed(client, auth_headers["X-FlowOps-Token"])

    assert counts(db_session) == (len(seed.WORKFLOWS), DISTINCT_EVENTS)
    assert results["created"] == DISTINCT_EVENTS
    assert results["updated"] == 2  # exec-004 and exec-005: running -> terminal
    assert results["unchanged"] == 2  # the two exact duplicates
    pair = db_session.scalar(
        select(ExecutionEvent).where(ExecutionEvent.external_event_id == "seed-exec-004")
    )
    assert pair.status == ExecutionStatus.SUCCESS
    assert pair.duration_ms == 12_000


def test_running_the_seed_twice_adds_nothing(client, db_session, auth_headers):
    seed.run_seed(client, auth_headers["X-FlowOps-Token"])
    before = counts(db_session)

    second = seed.run_seed(client, auth_headers["X-FlowOps-Token"])

    assert counts(db_session) == before
    assert set(second) == {"unchanged"}


def test_seed_error_payloads_are_synthetic_and_get_sanitized(client, db_session, auth_headers):
    seed.run_seed(client, auth_headers["X-FlowOps-Token"])

    for row in db_session.scalars(select(ExecutionEvent)):
        if row.error_summary:
            assert "seed-not-a-real" not in row.error_summary


def test_seed_with_the_wrong_token_fails_cleanly(client):
    try:
        seed.run_seed(client, "wrong")
    except seed.SeedError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected SeedError")
