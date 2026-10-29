"""Server-rendered HTML pages. Thin: parse the request, call the service layer, render."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExecutionStatus
from app.services import dashboard as dashboard_service
from app.services import events as events_service
from app.services import workflows as workflows_service
from app.timeutil import parse_query_bound
from app.web import templates

router = APIRouter(include_in_schema=False)

PAGE_SIZE = 25


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "dashboard.html", {"page": "dashboard", "d": dashboard_service.get_dashboard(db)}
    )


@router.get("/workflows")
def workflows_page(
    request: Request, include_inactive: bool = False, db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request,
        "workflows.html",
        {
            "page": "workflows",
            "workflows": workflows_service.list_workflows(db, include_inactive),
            "include_inactive": include_inactive,
        },
    )


def _parse_filters(workflow_id: str, status: str, started_from: str, started_to: str):
    """Forms submit blank fields as ''. Returns (filters, errors); never raises."""
    filters, errors = {}, []
    if workflow_id.strip():
        try:
            filters["workflow_id"] = int(workflow_id)
        except ValueError:
            errors.append("Workflow must be a number.")
    if status.strip():
        try:
            filters["status"] = ExecutionStatus(status.strip())
        except ValueError:
            errors.append("Unknown status.")
    for key, text, upper in (
        ("started_from", started_from, False),
        ("started_to", started_to, True),
    ):
        if text.strip():
            try:
                filters[key] = parse_query_bound(text, exclusive_upper=upper)
            except ValueError:
                errors.append(f"{key.replace('_', ' ').capitalize()} is not a valid date.")
    return filters, errors


@router.get("/events")
def events_page(
    request: Request,
    workflow_id: str = Query(default=""),
    status: str = Query(default=""),
    started_from: str = Query(default=""),
    started_to: str = Query(default=""),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    filters, errors = _parse_filters(workflow_id, status, started_from, started_to)
    if errors:
        items, total = [], 0
    else:
        rows, total = events_service.query_events(
            db, **filters, limit=PAGE_SIZE, offset=offset
        )
        items = dashboard_service.to_views(rows)

    form = {
        "workflow_id": workflow_id, "status": status,
        "started_from": started_from, "started_to": started_to,
    }
    kept = {k: v for k, v in form.items() if v.strip()}

    def page_url(new_offset: int) -> str:
        return "/events?" + urlencode({**kept, "offset": new_offset})

    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "page": "events",
            "items": items,
            "total": total,
            "errors": errors,
            "form": form,
            "filtered": bool(kept),
            "statuses": [s.value for s in ExecutionStatus],
            "workflows": workflows_service.list_workflows(db, include_inactive=True),
            "workflow_names": dashboard_service.workflow_names(
                db, {e.workflow_id for e in items}
            ),
            "first": offset + 1 if items else 0,
            "last": offset + len(items),
            "prev_url": page_url(max(offset - PAGE_SIZE, 0)) if offset > 0 else None,
            "next_url": page_url(offset + PAGE_SIZE) if offset + len(items) < total else None,
        },
    )
