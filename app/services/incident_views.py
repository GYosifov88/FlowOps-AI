"""Read-side data for the incident pages and the API (V2-M5).

The same builders feed the JSON routes and the HTML templates, so what a page shows is exactly
what the API returns. Nothing here calls a model or writes anything. Templates get pydantic
views (never ORM rows): `EventRead` has no `error_raw`, so it cannot be rendered.
"""
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import ACTIVE_INCIDENT, IncidentAnalysis, IncidentStatus
from app.schemas import AnalysisRead, ConfirmationRead, EventRead, IncidentRead, TransitionRead
from app.services import analysis as analysis_service
from app.services import approval
from app.services import dashboard as dashboard_service
from app.services import incident_context
from app.services import incidents as incidents_service

# View name -> the statuses it shows (None = every status). Anything else is an unknown view.
VIEW_ACTIVE = "active"
VIEW_ALL = "all"
VIEWS: dict[str, frozenset[IncidentStatus] | None] = {
    VIEW_ACTIVE: ACTIVE_INCIDENT,
    VIEW_ALL: None,
    **{s.value: frozenset({s}) for s in IncidentStatus},
}

MOVE_LABELS = {
    IncidentStatus.ACKNOWLEDGED: "Acknowledge",
    IncidentStatus.RESOLVED: "Mark resolved",
}


# ------------------------------------------------------------------ builders shared with the API


incident_read = incidents_service.incident_read


def analysis_read(row: IncidentAnalysis) -> dict:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "context_hash": row.context_hash,
        "context_version": row.context_version,
        "prompt_version": row.prompt_version,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "analysis": json.loads(row.analysis_json) if row.analysis_json else None,
        "validation_notes": json.loads(row.validation_notes_json) if row.validation_notes_json else [],
        "refusal": row.refusal,
        "error_type": row.error_type,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "reasoning_tokens": row.reasoning_tokens,
        "forced": row.forced,
        "created_at": row.created_at,
    }


def confirmation_read(view: approval.ConfirmationView) -> dict:
    row = view.row
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "analysis_id": row.analysis_id,
        "hypothesis_index": row.hypothesis_index,
        "cause": view.cause,
        "confidence": view.confidence,
        "supported": view.supported,
        "verified_by": row.verified_by,
        "note": row.note,
        "created_at": row.created_at,
    }


# ------------------------------------------------------------------ list page


def parse_view(view: str) -> frozenset[IncidentStatus] | None:
    """The statuses for a view name. Raises ValueError for an unknown one."""
    view = view.strip() or VIEW_ACTIVE
    if view not in VIEWS:
        raise ValueError(view)
    return VIEWS[view]


@dataclass
class IncidentListData:
    items: list[IncidentRead]
    total: int
    workflow_names: dict[int, str]


def incident_list(db: Session, view: str, *, limit: int, offset: int) -> IncidentListData:
    statuses = parse_view(view)
    summaries, total = incidents_service.list_incidents(db, statuses=statuses, limit=limit, offset=offset)
    items = [IncidentRead.model_validate(incident_read(s)) for s in summaries]
    names = dashboard_service.workflow_names(db, {i.workflow_id for i in items})
    return IncidentListData(items, total, names)


# ------------------------------------------------------------------ detail page


@dataclass
class IncidentPageData:
    incident: IncidentRead
    workflow_name: str
    events: list[EventRead]
    allowed_moves: list[tuple[str, str]]  # (status value, button label), only what a person may do now
    confirmed: ConfirmationRead | None  # the newest human confirmation
    confirmations: list[ConfirmationRead]  # newest first, includes `confirmed`
    transitions: list[TransitionRead]  # oldest first
    analyses: list[AnalysisRead]  # every attempt, newest first
    analysis: AnalysisRead | None  # the newest attempt with status ok
    context_hash: str
    context_json: str


def incident_page(db: Session, incident_id: int) -> IncidentPageData:
    """Everything the detail page shows. Raises IncidentNotFound. Calls no model."""
    summary, recent = incidents_service.get_incident(db, incident_id)
    incident = IncidentRead.model_validate(incident_read(summary))
    context = incident_context.build_context(db, incident_id)
    analyses = [AnalysisRead.model_validate(analysis_read(r)) for r in analysis_service.list_analyses(db, incident_id)]
    confirmations = [
        ConfirmationRead.model_validate(confirmation_read(v)) for v in approval.list_confirmations(db, incident_id)
    ]
    return IncidentPageData(
        incident=incident,
        workflow_name=dashboard_service.workflow_names(db, {incident.workflow_id}).get(
            incident.workflow_id, f"Workflow #{incident.workflow_id}"
        ),
        events=dashboard_service.to_views(recent),
        allowed_moves=[(s.value, MOVE_LABELS[s]) for s in approval.allowed_moves(incident.status)],
        confirmed=confirmations[0] if confirmations else None,
        confirmations=confirmations,
        transitions=[TransitionRead.model_validate(t) for t in approval.list_transitions(db, incident_id)],
        analyses=analyses,
        analysis=next((a for a in analyses if a.status.value == "ok"), None),
        context_hash=incident_context.context_hash(context),
        context_json=json.dumps(context, indent=2, ensure_ascii=False, sort_keys=True),
    )
