"""Incident grouping (V2-M1): failed events with the same signature share one Incident.

`assign_incident` is called by `ingest_event` whenever an event is stored as, or becomes,
`failed` -- so the API, the seed script and the n8n sync all group the same way, in the
same transaction as the event. Nothing else ever gets an incident.

Occurrence counts and first/last seen are computed by query (never stored). "Seen" is
`started_at`, or `created_at` when the event has no start time.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.signature as signature
from app.database import NaiveUTCDateTime
from app.models import ExecutionEvent, ExecutionStatus, Incident, IncidentStatus

logger = logging.getLogger(__name__)

RECENT_EVENTS = 10

_seen_at = func.coalesce(ExecutionEvent.started_at, ExecutionEvent.created_at)


class IncidentNotFound(Exception):
    pass


@dataclass
class IncidentSummary:
    incident: Incident
    occurrence_count: int
    first_seen: datetime
    last_seen: datetime


@dataclass
class RebuildResult:
    failed_events: int
    incidents_created: int
    incidents_removed: int
    incidents_total: int


def _find_by_signature(db: Session, sig: str) -> Incident | None:
    return db.scalar(select(Incident).where(Incident.signature == sig))


def _incident_for(db: Session, event: ExecutionEvent) -> tuple[Incident, bool]:
    """Get or create the incident for this event's CURRENT fields. (incident, created)."""
    parts = signature.build_parts(
        node_name=event.node_name,
        error_raw=event.error_raw,
        error_summary=event.error_summary,
    )
    sig = signature.compute_signature(event.workflow_id, parts)
    incident = _find_by_signature(db, sig)
    if incident is not None:
        return incident, False
    incident = Incident(
        signature=sig,
        signature_version=signature.SIGNATURE_VERSION,
        workflow_id=event.workflow_id,
        node_name=parts.node_name,
        error_name=parts.error_name or None,
        normalized_message=parts.normalized_message,
        status=IncidentStatus.OPEN,
    )
    # No commit here: the caller commits the event and the incident together. If another
    # request created the same signature first, the UNIQUE constraint raises IntegrityError
    # at flush/commit and ingest_event rolls back and tries again (it then finds it).
    db.add(incident)
    return incident, True


def assign_incident(db: Session, event: ExecutionEvent) -> Incident | None:
    """Attach `event` to its incident if it is failed; any other status gets none."""
    if event.status != ExecutionStatus.FAILED:
        return None
    incident, created = _incident_for(db, event)
    event.incident = incident
    if created:
        logger.info("New incident for workflow %s (signature %s)", event.workflow_id, incident.signature)
    return incident


def rebuild_all(db: Session) -> RebuildResult:
    """Recompute every failed event's incident with the CURRENT signature rules.

    Run after a rule change (scripts/rebuild_incidents.py). Idempotent: a second run
    changes nothing. Non-failed events are set to no incident. Incidents left with no
    events (their old signature no longer exists) are deleted -- they are derived data, not
    history, and would otherwise show up as empty ghosts. One transaction: all or nothing.
    """
    created = 0
    failed = db.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.status == ExecutionStatus.FAILED)
        .order_by(ExecutionEvent.id)
    ).all()
    for event in failed:
        incident, was_created = _incident_for(db, event)
        created += was_created
        if was_created:
            db.flush()  # later events in this loop must find it
        event.incident = incident

    for event in db.scalars(
        select(ExecutionEvent).where(
            ExecutionEvent.status != ExecutionStatus.FAILED,
            ExecutionEvent.incident_id.is_not(None),
        )
    ):
        event.incident = None

    db.flush()
    in_use = select(ExecutionEvent.incident_id).where(ExecutionEvent.incident_id.is_not(None))
    orphans = db.scalars(select(Incident).where(Incident.id.not_in(in_use))).all()
    for orphan in orphans:
        db.delete(orphan)
    db.commit()
    total = db.scalar(select(func.count()).select_from(Incident))
    return RebuildResult(len(failed), created, len(orphans), total)


# ------------------------------------------------------------------ reads


def _stats_subquery():
    return (
        select(
            ExecutionEvent.incident_id.label("incident_id"),
            func.count().label("occurrence_count"),
            func.min(_seen_at, type_=NaiveUTCDateTime).label("first_seen"),
            func.max(_seen_at, type_=NaiveUTCDateTime).label("last_seen"),
        )
        .where(ExecutionEvent.incident_id.is_not(None))
        .group_by(ExecutionEvent.incident_id)
        .subquery()
    )


def list_incidents(
    db: Session,
    *,
    workflow_id: int | None = None,
    status: IncidentStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[IncidentSummary], int]:
    """Incidents, most recently seen first (id breaks ties), plus the total match count."""
    stats = _stats_subquery()
    conditions = []
    if workflow_id is not None:
        conditions.append(Incident.workflow_id == workflow_id)
    if status is not None:
        conditions.append(Incident.status == status)

    total = db.scalar(
        select(func.count())
        .select_from(Incident)
        .join(stats, stats.c.incident_id == Incident.id)
        .where(*conditions)
    )
    rows = db.execute(
        select(Incident, stats.c.occurrence_count, stats.c.first_seen, stats.c.last_seen)
        .join(stats, stats.c.incident_id == Incident.id)
        .where(*conditions)
        .order_by(stats.c.last_seen.desc(), Incident.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [IncidentSummary(*row) for row in rows], total


def get_incident(db: Session, incident_id: int) -> tuple[IncidentSummary, list[ExecutionEvent]]:
    """One incident with its counters and its most recent events. Raises IncidentNotFound."""
    stats = _stats_subquery()
    row = db.execute(
        select(Incident, stats.c.occurrence_count, stats.c.first_seen, stats.c.last_seen)
        .join(stats, stats.c.incident_id == Incident.id)
        .where(Incident.id == incident_id)
    ).first()
    if row is None:
        raise IncidentNotFound
    recent = db.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.incident_id == incident_id)
        .order_by(_seen_at.desc(), ExecutionEvent.id.desc())
        .limit(RECENT_EVENTS)
    ).all()
    return IncidentSummary(*row), list(recent)
