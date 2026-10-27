"""Pydantic schemas for the API. Input and output are separate on purpose: the API never
returns a raw ORM object."""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
