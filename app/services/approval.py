"""Human approval of incidents (V2-M4): status transitions and cause confirmation.

* A person moves an incident open/regressed -> acknowledged -> resolved. Any other move is
  refused (`InvalidTransition`). RESOLVED -> REGRESSED is made by FlowOps alone, when a new
  failure arrives (`incidents.regress_if_reopened`); a person can never set open or regressed.
* A person confirms ONE hypothesis of a stored analysis, with a note. This is the only place
  "verified" exists. The model never verifies anything, and nothing is applied automatically.
* Both histories are insert-only. `actor` is self-declared (V1 has no users; the token proves
  access, not identity). Notes are redacted and capped. Logs carry ids and statuses only --
  never an actor name or a note.
"""
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.analysis.schema import CheckedAnalysis
from app.models import (
    HUMAN_TRANSITIONS,
    AnalysisStatus,
    Incident,
    IncidentAnalysis,
    IncidentCauseConfirmation,
    IncidentStatus,
    IncidentTransition,
)
from app.sanitize import redact
from app.services.incidents import IncidentNotFound

logger = logging.getLogger(__name__)

NOTE_CAP = 1000


class InvalidTransition(Exception):
    def __init__(self, current: IncidentStatus, allowed: list[IncidentStatus]) -> None:
        super().__init__(f"{current.value} -> not allowed")
        self.current = current
        self.allowed = allowed


class InvalidConfirmation(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = redact(note.strip())[:NOTE_CAP].strip()
    return cleaned or None


def _allowed(current: IncidentStatus) -> list[IncidentStatus]:
    return sorted(HUMAN_TRANSITIONS[current], key=lambda s: s.value)


# ------------------------------------------------------------------ transitions


def transition(
    db: Session, incident_id: int, to: IncidentStatus, actor: str, note: str | None
) -> tuple[Incident, IncidentTransition]:
    """Raises IncidentNotFound / InvalidTransition. Commits the change and its history row together."""
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise IncidentNotFound
    current = incident.status
    if to not in HUMAN_TRANSITIONS[current]:
        raise InvalidTransition(current, _allowed(current))

    # Conditional UPDATE: if the status changed since we read it (a second click, or a
    # regression), nothing is written and the caller gets a 409 with the real current status.
    result = db.execute(
        update(Incident).where(Incident.id == incident_id, Incident.status == current).values(status=to)
    )
    if result.rowcount != 1:
        db.rollback()
        db.expire_all()
        now_current = db.get(Incident, incident_id).status
        raise InvalidTransition(now_current, _allowed(now_current))

    row = IncidentTransition(
        incident_id=incident_id, from_status=current, to_status=to, actor=actor, note=_clean_note(note)
    )
    db.add(row)
    db.commit()
    db.refresh(incident)
    db.refresh(row)
    logger.info("Incident %s %s -> %s (transition %s)", incident_id, current.value, to.value, row.id)
    return incident, row


def list_transitions(db: Session, incident_id: int) -> list[IncidentTransition]:
    """Oldest first. Raises IncidentNotFound."""
    if db.get(Incident, incident_id) is None:
        raise IncidentNotFound
    return list(
        db.scalars(
            select(IncidentTransition)
            .where(IncidentTransition.incident_id == incident_id)
            .order_by(IncidentTransition.id)
        )
    )


# ------------------------------------------------------------------ cause confirmation


@dataclass
class ConfirmationView:
    """A confirmation plus the hypothesis it points at (read from the never-updated analysis)."""

    row: IncidentCauseConfirmation
    cause: str | None
    confidence: str | None
    supported: bool | None


def _view(db: Session, row: IncidentCauseConfirmation) -> ConfirmationView:
    analysis = db.get(IncidentAnalysis, row.analysis_id)
    if analysis is None or not analysis.analysis_json:
        return ConfirmationView(row, None, None, None)
    hypotheses = CheckedAnalysis.model_validate_json(analysis.analysis_json).hypotheses
    if row.hypothesis_index >= len(hypotheses):
        return ConfirmationView(row, None, None, None)
    hypothesis = hypotheses[row.hypothesis_index]
    return ConfirmationView(row, hypothesis.cause, hypothesis.confidence, hypothesis.supported)


def confirm_cause(
    db: Session, incident_id: int, analysis_id: int, hypothesis_index: int, actor: str, note: str
) -> ConfirmationView:
    """A person confirms hypothesis `hypothesis_index` of analysis `analysis_id`.

    Raises IncidentNotFound / InvalidConfirmation. The analysis must belong to THIS incident and
    be an `ok` one. A hypothesis flagged `supported=false` may be confirmed: the person may have
    checked it where FlowOps cannot see (for example the credential inside n8n). Confirming
    never changes the incident status. The newest confirmation is the current cause.
    """
    if db.get(Incident, incident_id) is None:
        raise IncidentNotFound
    analysis = db.get(IncidentAnalysis, analysis_id)
    if analysis is None or analysis.incident_id != incident_id:
        raise InvalidConfirmation("analysis not found for this incident")
    if analysis.status != AnalysisStatus.OK or not analysis.analysis_json:
        raise InvalidConfirmation("only an analysis with status ok has hypotheses to confirm")
    count = len(CheckedAnalysis.model_validate_json(analysis.analysis_json).hypotheses)
    if not 0 <= hypothesis_index < count:
        raise InvalidConfirmation(f"hypothesis_index must be 0..{count - 1}" if count else "the analysis has no hypotheses")
    cleaned = _clean_note(note)
    if cleaned is None:
        raise InvalidConfirmation("a note is required")

    row = IncidentCauseConfirmation(
        incident_id=incident_id,
        analysis_id=analysis_id,
        hypothesis_index=hypothesis_index,
        verified_by=actor,
        note=cleaned,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "Cause confirmed incident=%s analysis=%s hypothesis=%s confirmation=%s",
        incident_id, analysis_id, hypothesis_index, row.id,
    )
    return _view(db, row)


def list_confirmations(db: Session, incident_id: int) -> list[ConfirmationView]:
    """Newest first. Raises IncidentNotFound."""
    if db.get(Incident, incident_id) is None:
        raise IncidentNotFound
    rows = db.scalars(
        select(IncidentCauseConfirmation)
        .where(IncidentCauseConfirmation.incident_id == incident_id)
        .order_by(IncidentCauseConfirmation.id.desc())
    )
    return [_view(db, row) for row in rows]


def confirmed_cause(db: Session, incident_id: int) -> ConfirmationView | None:
    """The incident's current confirmed cause (the newest confirmation), or None."""
    row = db.scalar(
        select(IncidentCauseConfirmation)
        .where(IncidentCauseConfirmation.incident_id == incident_id)
        .order_by(IncidentCauseConfirmation.id.desc())
        .limit(1)
    )
    return None if row is None else _view(db, row)


__all__ = [
    "ConfirmationView", "InvalidConfirmation", "InvalidTransition", "NOTE_CAP",
    "confirm_cause", "confirmed_cause", "list_confirmations", "list_transitions", "transition",
]
