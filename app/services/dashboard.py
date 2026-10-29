"""Read-side data for the HTML pages. Pages call this layer; templates never query."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ExecutionEvent, ExecutionStatus, Workflow
from app.schemas import EventRead
from app.services import events as events_service

RECENT_EVENTS = 10
RECENT_FAILURES = 5


@dataclass
class DashboardData:
    workflows_total: int
    workflows_active: int
    events_total: int
    status_counts: dict[ExecutionStatus, int]  # every status present, zeros included
    recent_events: list[EventRead]
    recent_failures: list[EventRead]
    workflow_names: dict[int, str]


def workflow_names(db: Session, workflow_ids: set[int]) -> dict[int, str]:
    if not workflow_ids:
        return {}
    rows = db.execute(
        select(Workflow.id, Workflow.name).where(Workflow.id.in_(workflow_ids))
    )
    return {wid: name for wid, name in rows}


def to_views(events: list[ExecutionEvent]) -> list[EventRead]:
    """ORM rows -> EventRead, which has no error_raw field: templates cannot reach it."""
    return [EventRead.model_validate(e) for e in events]


def get_dashboard(db: Session) -> DashboardData:
    workflows_total = db.scalar(select(func.count()).select_from(Workflow))
    workflows_active = db.scalar(
        select(func.count()).select_from(Workflow).where(Workflow.is_active.is_(True))
    )
    counts = dict(
        db.execute(
            select(ExecutionEvent.status, func.count()).group_by(ExecutionEvent.status)
        ).all()
    )
    status_counts = {s: counts.get(s, 0) for s in ExecutionStatus}

    recent, _ = events_service.query_events(db, limit=RECENT_EVENTS)
    failures, _ = events_service.query_events(
        db, status=ExecutionStatus.FAILED, limit=RECENT_FAILURES
    )
    recent_views, failure_views = to_views(recent), to_views(failures)
    return DashboardData(
        workflows_total=workflows_total,
        workflows_active=workflows_active,
        events_total=sum(status_counts.values()),
        status_counts=status_counts,
        recent_events=recent_views,
        recent_failures=failure_views,
        workflow_names=workflow_names(
            db, {e.workflow_id for e in recent_views + failure_views}
        ),
    )
