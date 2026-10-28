from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.auth import require_ingest_token
from app.database import get_db
from app.schemas import EventCreate, EventIngestResponse
from app.services import events as service

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post(
    "",
    response_model=EventIngestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ingest_token)],
    responses={200: {"description": "Existing event: updated or unchanged"}},
)
def ingest_event(data: EventCreate, response: Response, db: Session = Depends(get_db)):
    """Record one execution event (requires the `X-FlowOps-Token` header).

    Upsert on `external_event_id`: 201 `created`; 200 `updated` when a non-terminal event
    reaches a terminal status; 200 `unchanged` otherwise. `error_raw` is never returned.
    """
    try:
        result = service.ingest_event(db, data)
    except service.WorkflowNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    except service.EventOwnerMismatch:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "external_event_id already belongs to a different workflow",
        )
    if result.outcome != "created":
        response.status_code = status.HTTP_200_OK
    return {"result": result.outcome, "event": result.event}
