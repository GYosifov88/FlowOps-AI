from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.auth import require_ingest_token
from app.database import get_db
from app.models import ExecutionStatus
from app.schemas import EventCreate, EventIngestResponse, EventPage
from app.services import events as service
from app.timeutil import parse_query_bound

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


DATE_HELP = (
    "ISO 8601 date or datetime, interpreted as UTC (naive input is assumed UTC, aware input "
    "is converted; URL-encode a '+' offset as %2B). Filters on `started_at`: events with a "
    "NULL `started_at` are EXCLUDED whenever `started_from` or `started_to` is given. "
)


def _bound(name: str, value: str | None, *, exclusive_upper: bool = False):
    if value is None:
        return None
    try:
        return parse_query_bound(value, exclusive_upper=exclusive_upper)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [{"loc": ["query", name], "msg": "Not a valid ISO 8601 date or datetime",
              "type": "value_error", "input": value}],
        )


@router.get("", response_model=EventPage)
def list_events(
    workflow_id: int | None = Query(default=None, description="Only this workflow's events."),
    status: ExecutionStatus | None = Query(default=None, description="Canonical status."),
    started_from: str | None = Query(
        default=None,
        description=DATE_HELP + "INCLUSIVE: `started_at >= started_from`. "
        "A bare date means 00:00 UTC of that day.",
        examples=["2026-10-27", "2026-10-27T09:00:00Z"],
    ),
    started_to: str | None = Query(
        default=None,
        description=DATE_HELP + "EXCLUSIVE: `started_at < started_to`. A bare date expands "
        "to 00:00 of the NEXT day, so `2026-10-27` includes all of 27 October.",
        examples=["2026-10-27", "2026-10-27T18:00:00Z"],
    ),
    limit: int = Query(default=50, ge=1, le=200, description="Page size (max 200; more is a 422)."),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List execution events, newest first (`started_at` DESC, then `id` DESC).

    Filters combine with AND. `started_from` is inclusive and `started_to` exclusive, both
    in UTC; a bare date for `started_to` covers that whole day. Events with a NULL
    `started_at` are excluded whenever either bound is given, and otherwise sort last.
    `total` counts every match regardless of paging. An unknown `workflow_id`, or
    `started_from` later than `started_to`, simply yields an empty page. `error_raw` is
    never returned.
    """
    items, total = service.query_events(
        db,
        workflow_id=workflow_id,
        status=status,
        started_from=_bound("started_from", started_from),
        started_to=_bound("started_to", started_to, exclusive_upper=True),
        limit=limit,
        offset=offset,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
