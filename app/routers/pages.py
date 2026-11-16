"""Server-rendered HTML pages. Thin: parse the request, call the service layer, render.

V2-M5 adds the incident pages and the form actions. Actions are POST -> do the work -> 303
redirect back to the page (Post/Redirect/Get) with a short key (`notice` / `error`) that maps
to a FIXED message: nothing from the URL is ever echoed into a page.
"""
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import ui_auth
from app.analysis.analyzer import Analyzer
from app.config import Settings, get_settings
from app.database import get_db
from app.models import ExecutionStatus, IncidentStatus
from app.routers.incidents import get_optional_analyzer
from app.services import analysis as analysis_service
from app.services import approval
from app.services import dashboard as dashboard_service
from app.services import events as events_service
from app.services import incident_views
from app.services import workflows as workflows_service
from app.services.incidents import IncidentNotFound
from app.timeutil import parse_query_bound
from app.ui_auth import UiAction, UiSession
from app.web import templates

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

PAGE_SIZE = 25


def ui_context(session: UiSession | None, **extra) -> dict:
    """Template variables every page needs for the header (unlock state + its CSRF token)."""
    return {"session": session, "csrf": session.csrf if session else None, **extra}


@router.get("/")
def dashboard(
    request: Request, db: Session = Depends(get_db), session: UiSession | None = Depends(ui_auth.get_ui_session)
):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        ui_context(session, page="dashboard", d=dashboard_service.get_dashboard(db)),
    )


@router.get("/workflows")
def workflows_page(
    request: Request,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    session: UiSession | None = Depends(ui_auth.get_ui_session),
):
    return templates.TemplateResponse(
        request,
        "workflows.html",
        ui_context(
            session,
            page="workflows",
            workflows=workflows_service.list_workflows(db, include_inactive),
            include_inactive=include_inactive,
        ),
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
    session: UiSession | None = Depends(ui_auth.get_ui_session),
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
        ui_context(
            session,
            page="events",
            items=items,
            total=total,
            errors=errors,
            form=form,
            filtered=bool(kept),
            statuses=[s.value for s in ExecutionStatus],
            workflows=workflows_service.list_workflows(db, include_inactive=True),
            workflow_names=dashboard_service.workflow_names(db, {e.workflow_id for e in items}),
            first=offset + 1 if items else 0,
            last=offset + len(items),
            prev_url=page_url(max(offset - PAGE_SIZE, 0)) if offset > 0 else None,
            next_url=page_url(offset + PAGE_SIZE) if offset + len(items) < total else None,
        ),
    )


# ====================================================================== incidents (V2-M5)

# Fixed messages. The URL carries only a key; an unknown key shows nothing.
NOTICES = {
    "acknowledged": "Incident acknowledged.",
    "resolved": "Incident marked resolved.",
    "analyzed": "Analysis stored.",
    "reused": "An identical analysis already existed, so it was reused: no model call, no cost.",
    "confirmed": "Cause confirmed.",
}
ERRORS = {
    "not_allowed": "That move is not allowed from the incident's current status.",
    "bad_input": "The form was not understood.",
    "note_required": "A note is required: say what you checked.",
    "bad_confirmation": "That hypothesis cannot be confirmed (it must come from an ok analysis of this incident).",
    "no_key": "OPENAI_API_KEY is not configured, so no analysis can be run.",
    "analysis_failed": "The model call failed. The attempt was stored; see the attempts table.",
    "analysis_unusable": "The model answered, but with no usable analysis. See the attempts table.",
}


def _message(request: Request, table: dict[str, str], key: str) -> str | None:
    return table.get(request.query_params.get(key, ""))


def _not_found(request: Request, session: UiSession | None) -> Response:
    return templates.TemplateResponse(
        request,
        "message.html",
        ui_context(session, page="incidents", title="Incident not found", message="There is no incident with that id."),
        status_code=404,
    )


@router.get("/incidents")
def incidents_page(
    request: Request,
    view: str = Query(default=incident_views.VIEW_ACTIVE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    session: UiSession | None = Depends(ui_auth.get_ui_session),
):
    errors, data = [], None
    try:
        data = incident_views.incident_list(db, view, limit=PAGE_SIZE, offset=offset)
    except ValueError:
        errors.append("Unknown view.")
    items = data.items if data else []
    total = data.total if data else 0
    current = view.strip() or incident_views.VIEW_ACTIVE

    def page_url(new_offset: int) -> str:
        return "/incidents?" + urlencode({"view": current, "offset": new_offset})

    return templates.TemplateResponse(
        request,
        "incidents.html",
        ui_context(
            session,
            page="incidents",
            items=items,
            total=total,
            errors=errors,
            view=current,
            views=list(incident_views.VIEWS),
            workflow_names=data.workflow_names if data else {},
            first=offset + 1 if items else 0,
            last=offset + len(items),
            prev_url=page_url(max(offset - PAGE_SIZE, 0)) if offset > 0 else None,
            next_url=page_url(offset + PAGE_SIZE) if offset + len(items) < total else None,
        ),
    )


@router.get("/incidents/{incident_id}")
def incident_page(
    request: Request,
    incident_id: int,
    db: Session = Depends(get_db),
    session: UiSession | None = Depends(ui_auth.get_ui_session),
):
    try:
        data = incident_views.incident_page(db, incident_id)
    except IncidentNotFound:
        return _not_found(request, session)
    return templates.TemplateResponse(
        request,
        "incident.html",
        ui_context(
            session,
            page="incidents",
            d=data,
            notice=_message(request, NOTICES, "notice"),
            error=_message(request, ERRORS, "error"),
        ),
    )


def _back(incident_id: int, **query: str) -> RedirectResponse:
    return RedirectResponse(f"/incidents/{incident_id}?{urlencode(query)}", status_code=303)


@router.post("/incidents/{incident_id}/analyze")
def analyze_action(
    incident_id: int,
    act: UiAction = Depends(ui_auth.ui_action),
    db: Session = Depends(get_db),
    analyzer: Analyzer | None = Depends(get_optional_analyzer),
):
    """The ONLY thing in the pages that can reach a model, and only on a signed-in POST."""
    if analyzer is None:
        return _back(incident_id, error="no_key")
    force = act.form.get("force") == "1"
    try:
        outcome = analysis_service.analyze_incident(db, incident_id, analyzer, force=force)
    except IncidentNotFound:
        return RedirectResponse("/incidents", status_code=303)
    status = outcome.row.status.value
    if outcome.result == "reused":
        return _back(incident_id, notice="reused")
    if status == "error":
        return _back(incident_id, error="analysis_failed")
    if status != "ok":
        return _back(incident_id, error="analysis_unusable")
    return _back(incident_id, notice="analyzed")


@router.post("/incidents/{incident_id}/transition")
def transition_action(
    incident_id: int, act: UiAction = Depends(ui_auth.ui_action), db: Session = Depends(get_db)
):
    to = act.form.get("to", "")
    if to not in ("acknowledged", "resolved"):
        return _back(incident_id, error="bad_input")
    try:
        approval.transition(db, incident_id, IncidentStatus(to), act.session.actor, act.form.get("note"))
    except IncidentNotFound:
        return RedirectResponse("/incidents", status_code=303)
    except approval.InvalidTransition:
        return _back(incident_id, error="not_allowed")
    return _back(incident_id, notice=to)


@router.post("/incidents/{incident_id}/confirm")
def confirm_action(
    incident_id: int, act: UiAction = Depends(ui_auth.ui_action), db: Session = Depends(get_db)
):
    try:
        analysis_id = int(act.form.get("analysis_id", ""))
        index = int(act.form.get("hypothesis_index", ""))
    except ValueError:
        return _back(incident_id, error="bad_input")
    if not act.form.get("note", "").strip():
        return _back(incident_id, error="note_required")
    try:
        approval.confirm_cause(db, incident_id, analysis_id, index, act.session.actor, act.form["note"])
    except IncidentNotFound:
        return RedirectResponse("/incidents", status_code=303)
    except approval.InvalidConfirmation:
        return _back(incident_id, error="bad_confirmation")
    return _back(incident_id, notice="confirmed")


# ====================================================================== unlock / lock (V2-M5)


def _unlock_page(request: Request, next_url: str, error: str | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(
        request,
        "unlock.html",
        ui_context(None, page="unlock", next_url=next_url, error=error),
        status_code=status_code,
    )


@router.get("/unlock")
def unlock_page(request: Request, next: str = Query(default="/")):
    return _unlock_page(request, ui_auth.safe_next(next))


@router.post("/unlock", dependencies=[Depends(ui_auth.require_local_origin)])
def unlock_action(
    request: Request,
    form: dict[str, str] = Depends(ui_auth.read_form),
    settings: Settings = Depends(get_settings),
):
    next_url = ui_auth.safe_next(form.get("next"))
    actor = ui_auth.clean_actor(form.get("actor", ""))
    if not ui_auth.token_ok(settings, form.get("token", "")):
        logger.warning("UI unlock refused: wrong token")  # never the token itself
        return _unlock_page(request, next_url, "That token is not correct.", 401)
    if actor is None:
        return _unlock_page(
            request, next_url, "Your name must be 1-100 letters, digits, spaces or . _ @ -", 400
        )
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        ui_auth.SESSION_COOKIE,
        ui_auth.issue_cookie(settings, actor),
        max_age=ui_auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/",
    )
    logger.info("UI unlocked")  # no actor, no token
    return response


@router.post("/lock")
def lock_action(act: UiAction = Depends(ui_auth.ui_action)):
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(ui_auth.SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response
