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

from app.models import ExecutionStatus, IncidentStatus


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
