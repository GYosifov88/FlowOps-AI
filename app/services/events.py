"""Execution event ingestion: an upsert keyed on external_event_id.

    stored row      incoming     action
    --------------  -----------  ------------------------------------------------
    (none)          any          insert                                   created
    NON_TERMINAL    TERMINAL     update in place (finish the execution)   updated
    NON_TERMINAL    NON_TERMINAL leave alone                              unchanged
    TERMINAL        any          leave alone (warn if status differs)     unchanged

An event without an external_event_id has nothing to dedup on and is always inserted.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import NON_TERMINAL, TERMINAL, ExecutionEvent, ExecutionStatus, Workflow
from app.sanitize import sanitize_error
from app.schemas import EventCreate
from app.timeutil import compute_duration_ms, to_naive_utc

logger = logging.getLogger(__name__)

IngestOutcome = Literal["created", "updated", "unchanged"]


class WorkflowNotFound(Exception):
    pass


class EventOwnerMismatch(Exception):
    """external_event_id already belongs to a different workflow."""


@dataclass
class IngestResult:
    outcome: IngestOutcome
    event: ExecutionEvent


def ingest_event(
    db: Session, data: EventCreate, *, summary_source: str | None = None
) -> IngestResult:
    """Upsert one event. `summary_source` is the text to sanitize into error_summary;
    by default that is error_raw. The n8n sync passes readable text there, because its
    error_raw is a JSON string."""
    if db.get(Workflow, data.workflow_id) is None:  # inactive workflows are accepted
        raise WorkflowNotFound

    existing = _find(db, data.external_event_id)
    if existing is None:
        try:
            return IngestResult("created", _insert(db, data, summary_source))
        except IntegrityError:
            # Lost a race against a concurrent insert of the same external_event_id.
            db.rollback()
            existing = _find(db, data.external_event_id)
            if existing is None:
                raise
    return _apply_to_existing(db, existing, data, summary_source)


def _find(db: Session, external_event_id: str | None) -> ExecutionEvent | None:
    if external_event_id is None:
        return None
    return db.scalar(
        select(ExecutionEvent).where(
            ExecutionEvent.external_event_id == external_event_id
        )
    )


def _insert(
    db: Session, data: EventCreate, summary_source: str | None = None
) -> ExecutionEvent:
    started_at = to_naive_utc(data.started_at)
    finished_at = to_naive_utc(data.finished_at)
    event = ExecutionEvent(
        workflow_id=data.workflow_id,
        external_event_id=data.external_event_id,
        status=data.status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=compute_duration_ms(started_at, finished_at),
        node_name=data.node_name,
        mode=data.mode,
        error_summary=sanitize_error(
            summary_source if summary_source is not None else data.error_raw
        ),
        error_raw=data.error_raw,  # stored exactly as received
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _apply_to_existing(
    db: Session,
    event: ExecutionEvent,
    data: EventCreate,
    summary_source: str | None = None,
) -> IngestResult:
    if event.workflow_id != data.workflow_id:
        raise EventOwnerMismatch

    if event.status in TERMINAL:
        if data.status != event.status:
            logger.warning(
                "Ignoring %s for event %s: already terminal as %s",
                data.status.value, event.id, event.status.value,
            )
        return IngestResult("unchanged", event)

    if data.status in NON_TERMINAL:
        return IngestResult("unchanged", event)

    # NON_TERMINAL -> TERMINAL: finish the existing row. id, created_at and an existing
    # started_at are kept.
    if event.started_at is None:
        event.started_at = to_naive_utc(data.started_at)
    event.status = data.status
    event.finished_at = to_naive_utc(data.finished_at)
    event.duration_ms = compute_duration_ms(event.started_at, event.finished_at)
    if data.error_raw is not None or summary_source is not None:
        if data.error_raw is not None:
            event.error_raw = data.error_raw
        event.error_summary = sanitize_error(
            summary_source if summary_source is not None else data.error_raw
        )
    if event.node_name is None:
        event.node_name = data.node_name
    if event.mode is None:
        event.mode = data.mode
    db.commit()
    db.refresh(event)
    return IngestResult("updated", event)


def query_events(
    db: Session,
    *,
    workflow_id: int | None = None,
    status: ExecutionStatus | None = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ExecutionEvent], int]:
    """Filtered, paged events plus the total number of matches.

    started_from is inclusive, started_to exclusive (both naive UTC). If either bound is
    given, events with a NULL started_at never match. Newest first; id breaks ties so
    paging is stable. NULL started_at sorts last explicitly, because SQLite and PostgreSQL
    disagree on where NULLs go by default.
    """
    conditions = []
    if workflow_id is not None:
        conditions.append(ExecutionEvent.workflow_id == workflow_id)
    if status is not None:
        conditions.append(ExecutionEvent.status == status)
    if started_from is not None:
        conditions.append(ExecutionEvent.started_at >= started_from)
    if started_to is not None:
        conditions.append(ExecutionEvent.started_at < started_to)

    total = db.scalar(select(func.count()).select_from(ExecutionEvent).where(*conditions))
    items = db.scalars(
        select(ExecutionEvent)
        .where(*conditions)
        .order_by(ExecutionEvent.started_at.desc().nulls_last(), ExecutionEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(items), total
