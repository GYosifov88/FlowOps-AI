"""Database models: Workflow and ExecutionEvent.

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
    # The last node that ran; not necessarily the one that failed.
    node_name: Mapped[str | None] = mapped_column(String(255))
    mode: Mapped[str | None] = mapped_column(String(50))
    # Sanitized and length-capped; the only error text that may be rendered.
    error_summary: Mapped[str | None] = mapped_column(Text)
    # Original error payload, stored as received. NEVER render or return it.
    error_raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(NaiveUTCDateTime, default=utcnow)

    workflow: Mapped[Workflow] = relationship(back_populates="events")
