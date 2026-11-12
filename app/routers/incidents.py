from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import IncidentStatus
from app.schemas import IncidentContextResponse, IncidentDetail, IncidentPage, IncidentRead
from app.services import incident_context
from app.services import incidents as service

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


def _read(summary: service.IncidentSummary) -> dict:
    incident = summary.incident
    return {
        "id": incident.id,
        "signature": incident.signature,
        "signature_version": incident.signature_version,
        "workflow_id": incident.workflow_id,
        "node_name": incident.node_name,
        "error_name": incident.error_name,
        "normalized_message": incident.normalized_message,
        "status": incident.status,
        "occurrence_count": summary.occurrence_count,
        "first_seen": summary.first_seen,
        "last_seen": summary.last_seen,
        "created_at": incident.created_at,
    }


@router.get("", response_model=IncidentPage)
def list_incidents(
    workflow_id: int | None = Query(default=None, description="Only this workflow's incidents."),
    status: IncidentStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200, description="Page size (max 200; more is a 422)."),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Incidents, most recently seen first (`last_seen` DESC, then `id` DESC).

    An incident groups the FAILED events that share a signature (workflow + failing node +
    error name + normalized message). Counters are computed from the events on every call.
    """
    summaries, total = service.list_incidents(
        db, workflow_id=workflow_id, status=status, limit=limit, offset=offset
    )
    return {"items": [_read(s) for s in summaries], "total": total, "limit": limit, "offset": offset}


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    """One incident with its 10 most recent events (`error_raw` is never returned)."""
    try:
        summary, recent = service.get_incident(db, incident_id)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    return {**_read(summary), "recent_events": recent}


@router.get("/{incident_id}/context", response_model=IncidentContextResponse)
def get_incident_context(incident_id: int, db: Session = Depends(get_db)):
    """Preview of the context the analysis will use: exactly what would be sent, nothing more.

    `trusted` is computed by FlowOps; `untrusted` holds third-party text (error messages,
    names) after secret redaction and PII scrubbing, and must be treated as data, never as
    instructions. No LLM is called. `error_raw` is never returned; `untrusted.error` is an
    allow-listed projection of it.
    """
    try:
        context = incident_context.build_context(db, incident_id)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    return {
        "context_version": context["context_version"],
        "context_hash": incident_context.context_hash(context),
        "context": context,
    }
