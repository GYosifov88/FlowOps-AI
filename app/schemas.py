"""Pydantic schemas for the API. Input and output are separate on purpose: the API never
returns a raw ORM object."""
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.analysis.schema import CheckedAnalysis, Level
from app.models import AnalysisStatus, ExecutionStatus, IncidentStatus


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    source: str = Field(min_length=1, max_length=100)
    description: str | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=255)


class WorkflowUpdate(BaseModel):
    """PATCH body: only the fields that are sent are changed."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    source: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None

    @model_validator(mode="after")
    def required_columns_cannot_be_nulled(self):
        for field in ("name", "source", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    source: str
    external_id: str | None
    is_active: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def mark_as_utc(cls, value: datetime) -> datetime:
        # The DB holds naive UTC; the API says so explicitly (serialized with a "Z").
        return value.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    # The DB holds naive UTC; the API says so explicitly (serialized with a "Z").
    return None if value is None else value.replace(tzinfo=timezone.utc)


class EventCreate(BaseModel):
    """Body of POST /api/events. Naive datetimes are taken as UTC, aware ones converted."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    workflow_id: int
    external_event_id: str | None = Field(default=None, min_length=1, max_length=255)
    status: ExecutionStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    node_name: str | None = Field(default=None, max_length=255)
    mode: str | None = Field(default=None, max_length=50)
    # Stored as received (no stripping), but capped so one request cannot store megabytes.
    error_raw: Annotated[str, StringConstraints(strip_whitespace=False, max_length=100_000)] | None = None


class EventRead(BaseModel):
    """Deliberately has no error_raw field: it must never leave the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_id: int
    external_event_id: str | None
    status: ExecutionStatus
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    node_name: str | None
    mode: str | None
    error_summary: str | None
    incident_id: int | None  # set only for failed events (V2-M1)
    created_at: datetime

    @field_validator("started_at", "finished_at", "created_at")
    @classmethod
    def mark_as_utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class EventIngestResponse(BaseModel):
    result: Literal["created", "updated", "unchanged"]
    event: EventRead


class EventPage(BaseModel):
    items: list[EventRead]
    total: int = Field(description="Number of events matching the filters, ignoring paging")
    limit: int
    offset: int


class IncidentRead(BaseModel):
    """An incident plus counters computed by query (they are never stored)."""

    id: int
    signature: str
    signature_version: str
    workflow_id: int
    node_name: str | None = Field(description="Null = unattributed (n8n named no failing node)")
    error_name: str | None
    normalized_message: str = Field(description="Redacted and normalized; why events were grouped")
    status: IncidentStatus
    occurrence_count: int
    first_seen: datetime = Field(description="Earliest started_at (created_at if no start time)")
    last_seen: datetime = Field(description="Latest started_at (created_at if no start time)")
    created_at: datetime

    @field_validator("first_seen", "last_seen", "created_at")
    @classmethod
    def mark_as_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc)


class IncidentDetail(IncidentRead):
    recent_events: list[EventRead] = Field(description="Up to 10 newest events, newest first")
    confirmed_cause: "ConfirmationRead | None" = Field(
        default=None, description="The newest human confirmation, or null (V2-M4)"
    )


class IncidentPage(BaseModel):
    items: list[IncidentRead]
    total: int = Field(description="Number of incidents matching the filters, ignoring paging")
    limit: int
    offset: int


class IncidentContextResponse(BaseModel):
    """Exactly what V2-M3 will send to the model, so it can be read first."""

    context_version: int
    context_hash: str = Field(description="sha256 of the canonical JSON of `context`")
    context: dict[str, Any] = Field(
        description="`trusted` = computed by FlowOps (no free text); `untrusted` = scrubbed third-party text"
    )


class AnalysisRead(BaseModel):
    """One stored analysis attempt. Only status "ok" has `analysis`; it is the VERIFIED version
    (unverifiable facts removed, confidence capped). The context that was sent is not included:
    reproduce it with GET /api/incidents/{id}/context (compare `context_hash`)."""

    id: int
    incident_id: int
    context_hash: str
    context_version: int
    prompt_version: str
    provider: str
    model: str
    status: AnalysisStatus
    analysis: CheckedAnalysis | None
    validation_notes: list[str]
    refusal: str | None
    error_type: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    forced: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def mark_as_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc)


class AnalyzeResponse(BaseModel):
    result: Literal["created", "reused"]
    analysis: AnalysisRead


# ------------------------------------------------------------------ approval (V2-M4)

Actor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9 ._@-]+$"),
]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class TransitionCreate(BaseModel):
    """People can only acknowledge or resolve. `open` and `regressed` are set by FlowOps alone."""

    model_config = ConfigDict(extra="forbid")

    to: Literal["acknowledged", "resolved"]
    actor: Actor = Field(description="Who is doing this. Self-declared: the token proves access, not identity.")
    note: Note | None = None


class TransitionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: int
    from_status: IncidentStatus
    to_status: IncidentStatus
    actor: str = Field(description='"flowops" for a regression')
    note: str | None
    event_id: int | None = Field(description="The failed event that caused a regression")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def mark_as_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc)


class TransitionResponse(BaseModel):
    incident_status: IncidentStatus
    transition: TransitionRead


class ConfirmationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: int
    hypothesis_index: int = Field(ge=0, description="0-based position in that analysis's hypotheses")
    actor: Actor
    note: Note = Field(min_length=1, description="What you checked. Required.")


class ConfirmationRead(BaseModel):
    """A person confirmed this hypothesis. The only 'verified' in FlowOps, and it is human."""

    id: int
    incident_id: int
    analysis_id: int
    hypothesis_index: int
    cause: str | None = Field(description="The hypothesis text, read from the stored analysis")
    confidence: Level | None = Field(description="What the MODEL claimed (not a verification)")
    supported: bool | None = Field(description="Whether FlowOps found valid evidence for it")
    verified_by: str
    note: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def mark_as_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc)


IncidentDetail.model_rebuild()
