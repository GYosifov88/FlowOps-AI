from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.analysis.analyzer import Analyzer, OpenAIAnalyzer
from app.auth import require_ingest_token
from app.config import Settings, get_settings
from app.database import get_db
from app.models import IncidentStatus
from app.schemas import (
    AnalysisRead,
    AnalyzeResponse,
    ConfirmationCreate,
    ConfirmationRead,
    IncidentContextResponse,
    IncidentDetail,
    IncidentPage,
    IncidentRead,
    TransitionCreate,
    TransitionRead,
    TransitionResponse,
)
from app.services import analysis as analysis_service
from app.services import approval
from app.services import incident_context, incident_views
from app.services import incidents as service

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


def build_analyzer(settings: Settings) -> Analyzer | None:
    """The configured analyzer, or None when there is no OPENAI_API_KEY."""
    if not settings.openai_api_key:
        return None
    return OpenAIAnalyzer(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
        max_output_tokens=settings.openai_max_output_tokens,
        timeout=settings.openai_timeout_seconds,
    )


def get_analyzer(settings: Settings = Depends(get_settings)) -> Analyzer:
    """The analyzer for this request. Tests override this dependency with a fake."""
    analyzer = build_analyzer(settings)
    if analyzer is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OPENAI_API_KEY is not configured; incident analysis is unavailable",
        )
    return analyzer


def get_optional_analyzer(settings: Settings = Depends(get_settings)) -> Analyzer | None:
    """Same, for the HTML pages: None (not a 503) when no key is configured."""
    return build_analyzer(settings)


_analysis_read = incident_views.analysis_read
_confirmation_read = incident_views.confirmation_read
_read = incident_views.incident_read


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
    cause = approval.confirmed_cause(db, incident_id)
    return {
        **_read(summary),
        "recent_events": recent,
        "confirmed_cause": _confirmation_read(cause) if cause else None,
    }


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


@router.post(
    "/{incident_id}/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ingest_token)],
    responses={
        200: {"description": "Reused: an identical analysis (same context, model, prompt) already exists"},
        502: {"description": "The model call failed (the failed attempt is stored)"},
        503: {"description": "OPENAI_API_KEY is not configured"},
    },
)
def analyze_incident(
    incident_id: int,
    response: Response,
    force: bool = Query(default=False, description="Call the model again even if an identical analysis exists."),
    db: Session = Depends(get_db),
    analyzer: Analyzer = Depends(get_analyzer),
):
    """Analyse one incident with the configured model. COSTS MONEY and never runs by itself.

    Requires the `X-FlowOps-Token` header. Sends exactly the context shown by
    `GET /api/incidents/{id}/context`. The result is verified on the server (every cited
    reference must exist in that context and every quote must appear in it) and stored; it is
    never overwritten. Without `force`, an identical earlier analysis is returned for free (200).
    A refused / incomplete / invalid answer is stored and returned with its status (201);
    only a failed call is a 502.
    """
    try:
        outcome = analysis_service.analyze_incident(db, incident_id, analyzer, force=force)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    if outcome.result == "reused":
        response.status_code = status.HTTP_200_OK
    if outcome.row.status.value == "error":
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Analysis failed ({outcome.row.error_type}); the attempt was stored as analysis {outcome.row.id}",
        )
    return {"result": outcome.result, "analysis": _analysis_read(outcome.row)}


@router.get("/{incident_id}/analyses", response_model=list[AnalysisRead])
def list_incident_analyses(incident_id: int, db: Session = Depends(get_db)):
    """Every stored analysis attempt of this incident, newest first (read-only, never calls a model)."""
    try:
        rows = analysis_service.list_analyses(db, incident_id)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    return [_analysis_read(row) for row in rows]


# ------------------------------------------------------------------ approval (V2-M4)


@router.post(
    "/{incident_id}/transitions",
    response_model=TransitionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ingest_token)],
    responses={409: {"description": "Not an allowed move from the current status (body lists the allowed ones)"}},
)
def transition_incident(incident_id: int, data: TransitionCreate, db: Session = Depends(get_db)):
    """Acknowledge or resolve an incident. Requires `X-FlowOps-Token`.

    Allowed: open -> acknowledged, regressed -> acknowledged, acknowledged -> resolved.
    Everything else is a 409, including repeating the current status and open -> resolved.
    A resolved incident becomes `regressed` by itself when a new failure arrives; people cannot set it.
    """
    try:
        incident, row = approval.transition(db, incident_id, IncidentStatus(data.to), data.actor, data.note)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    except approval.InvalidTransition as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": f"Cannot move from {exc.current.value} to {data.to}",
                "current_status": exc.current.value,
                "allowed": [s.value for s in exc.allowed],
            },
        )
    return {"incident_status": incident.status, "transition": row}


@router.get("/{incident_id}/transitions", response_model=list[TransitionRead])
def list_incident_transitions(incident_id: int, db: Session = Depends(get_db)):
    """Status history, oldest first."""
    try:
        return approval.list_transitions(db, incident_id)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")


@router.post(
    "/{incident_id}/confirmations",
    response_model=ConfirmationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_ingest_token)],
    responses={422: {"description": "Not an ok analysis of this incident, or no such hypothesis"}},
)
def confirm_incident_cause(incident_id: int, data: ConfirmationCreate, db: Session = Depends(get_db)):
    """A person confirms ONE hypothesis of a stored analysis. Requires `X-FlowOps-Token`.

    The only place a cause becomes "verified", and it is a human doing it. The newest
    confirmation is the incident's current cause; older ones stay as history. It does not
    change the incident status and nothing is applied automatically.
    """
    try:
        view = approval.confirm_cause(
            db, incident_id, data.analysis_id, data.hypothesis_index, data.actor, data.note
        )
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    except approval.InvalidConfirmation as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.reason)
    return _confirmation_read(view)


@router.get("/{incident_id}/confirmations", response_model=list[ConfirmationRead])
def list_incident_confirmations(incident_id: int, db: Session = Depends(get_db)):
    """Every confirmation, newest first."""
    try:
        views = approval.list_confirmations(db, incident_id)
    except service.IncidentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")
    return [_confirmation_read(v) for v in views]
