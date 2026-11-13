"""Database models: Workflow, ExecutionEvent and Incident.

Deletion policy: workflows are SOFT-deleted via is_active=False. There is no hard delete
and no cascade, and the foreign key is ON DELETE RESTRICT, so events are never orphaned.
"""
import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, NaiveUTCDateTime
from app.timeutil import utcnow


class ExecutionStatus(enum.StrEnum):
    """Canonical FlowOps execution status.

    DRAFT mapping from n8n execution statuses, to be finalized in M7:
        success -> success      error    -> failed     crashed -> failed
        canceled -> canceled    waiting  -> waiting
        running -> running      new      -> running    unknown -> unknown
    """

    SUCCESS = "success"
    FAILED = "failed"
    RUNNING = "running"
    WAITING = "waiting"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


TERMINAL = frozenset(
    {ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.CANCELED}
)
NON_TERMINAL = frozenset(
    {ExecutionStatus.RUNNING, ExecutionStatus.WAITING, ExecutionStatus.UNKNOWN}
)


class IncidentStatus(enum.StrEnum):
    """Lifecycle of an incident (V2-M4): open -> acknowledged -> resolved. A new failure after
    a resolve makes it REGRESSED (set by FlowOps only, never by a person)."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    REGRESSED = "regressed"


# "Counts as open": needs attention.
ACTIVE_INCIDENT = frozenset({IncidentStatus.OPEN, IncidentStatus.REGRESSED})

# The ONLY moves a person can make: current status -> statuses they may set. Everything else
# (jumps, no-ops, setting open/regressed) is refused. RESOLVED -> REGRESSED is system-only.
HUMAN_TRANSITIONS = {
    IncidentStatus.OPEN: frozenset({IncidentStatus.ACKNOWLEDGED}),
    IncidentStatus.REGRESSED: frozenset({IncidentStatus.ACKNOWLEDGED}),
    IncidentStatus.ACKNOWLEDGED: frozenset({IncidentStatus.RESOLVED}),
    IncidentStatus.RESOLVED: frozenset(),
}


def _incident_status_enum(name: str) -> Enum:
    return Enum(
        IncidentStatus,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )


class AnalysisStatus(enum.StrEnum):
    """Outcome of one analysis attempt. Only OK rows carry an analysis."""

    OK = "ok"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"  # ran out of output tokens, or was cut off
    INVALID = "invalid"  # no usable structured output came back
    ERROR = "error"  # network / auth / API error


class Workflow(Base):
    __tablename__ = "workflows"
    # One FlowOps workflow per (source system, id in that system). NULL external_ids never
    # collide, so workflows without one stay unrestricted.
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_workflows_source_external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))
    # The workflow id in the source system (e.g. n8n), always a string. Unique together
    # with `source`; that composite unique index also serves lookups by (source, external_id).
    external_id: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)

    events: Mapped[list["ExecutionEvent"]] = relationship(back_populates="workflow")


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), index=True
    )
    # Upsert key for ingestion (M4). NULLs are allowed and never collide.
    external_event_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(
            ExecutionStatus,
            name="execution_status",
            native_enum=False,
            create_constraint=True,  # without this no CHECK constraint is created
            validate_strings=True,
            values_callable=lambda members: [m.value for m in members],
        )
    )
    started_at: Mapped[datetime | None] = mapped_column(NaiveUTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(NaiveUTCDateTime)
    # None unless both timestamps exist; see app.timeutil.compute_duration_ms.
    duration_ms: Mapped[int | None]
    # The node that FAILED, from the error's own node (n8n sync, M8); NULL when unknown.
    # Never the last node that ran. Incident signatures group by it.
    node_name: Mapped[str | None] = mapped_column(String(255))
    mode: Mapped[str | None] = mapped_column(String(50))
    # Sanitized and length-capped; the only error text that may be rendered.
    error_summary: Mapped[str | None] = mapped_column(Text)
    # Original error payload, stored as received. NEVER render or return it.
    error_raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)
    # Set only for FAILED events (V2-M1), automatically when the event becomes failed.
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT"), index=True
    )

    workflow: Mapped[Workflow] = relationship(back_populates="events")
    incident: Mapped["Incident | None"] = relationship(back_populates="events")


class Incident(Base):
    """A group of failed events that are "the same problem" (see app.signature).

    The readable parts are kept so the UI can show WHY events were grouped. Counters
    (occurrences, first/last seen) are NOT stored: they are computed by query, so they can
    never disagree with the events.
    """

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(String(16), unique=True)
    signature_version: Mapped[str] = mapped_column(String(10))
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), index=True
    )
    node_name: Mapped[str | None] = mapped_column(String(255))  # NULL = unattributed
    error_name: Mapped[str | None] = mapped_column(String(255))  # NULL = unknown
    normalized_message: Mapped[str] = mapped_column(Text)  # redacted and normalized, safe to show
    status: Mapped[IncidentStatus] = mapped_column(
        _incident_status_enum("incident_status"),
        default=IncidentStatus.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)

    workflow: Mapped[Workflow] = relationship()
    events: Mapped[list[ExecutionEvent]] = relationship(back_populates="incident")


class IncidentAnalysis(Base):
    """One analysis attempt for an incident (V2-M3). Rows are NEVER updated or deleted: a new
    attempt is a new row, so the history of what was asked and answered stays intact."""

    __tablename__ = "incident_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT"), index=True
    )
    # sha256 of the exact context that was sent (app.services.incident_context.context_hash)
    context_hash: Mapped[str] = mapped_column(String(64))
    context_version: Mapped[int]
    prompt_version: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[AnalysisStatus] = mapped_column(
        Enum(
            AnalysisStatus,
            name="analysis_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda members: [m.value for m in members],
        )
    )
    # The verified analysis (JSON) -- only for status "ok". Never the raw model output.
    analysis_json: Mapped[str | None] = mapped_column(Text)
    validation_notes_json: Mapped[str | None] = mapped_column(Text)
    # Scrubbed and capped; only for status "refused".
    refusal: Mapped[str | None] = mapped_column(Text)
    # Exception class name (+ HTTP status) or the incomplete reason; never a message or body.
    error_type: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    reasoning_tokens: Mapped[int | None]
    forced: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)

    incident: Mapped[Incident] = relationship()


class IncidentTransition(Base):
    """One status change of an incident (V2-M4). Insert-only history. `actor` is
    self-declared ("flowops" for a regression): the token proves access, not identity."""

    __tablename__ = "incident_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT"), index=True
    )
    from_status: Mapped[IncidentStatus] = mapped_column(_incident_status_enum("transition_from_status"))
    to_status: Mapped[IncidentStatus] = mapped_column(_incident_status_enum("transition_to_status"))
    actor: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)  # redacted and capped
    # Only for a regression: the failed event that caused it.
    event_id: Mapped[int | None] = mapped_column(ForeignKey("execution_events.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)


class IncidentCauseConfirmation(Base):
    """A person confirms ONE hypothesis of a stored analysis (V2-M4). The only place "verified"
    exists. The hypothesis is (analysis_id, hypothesis_index): analysis rows are never updated,
    so that reference cannot drift. Insert-only; the newest row is the incident's current cause."""

    __tablename__ = "incident_cause_confirmations"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT"), index=True
    )
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("incident_analyses.id", ondelete="RESTRICT"), index=True
    )
    hypothesis_index: Mapped[int]
    verified_by: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(Text)  # required, redacted and capped
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)
