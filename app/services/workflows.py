"""Workflow registry logic. Routes stay thin; all DB work lives here.

Workflows are never hard-deleted: "delete" means is_active=False.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Workflow
from app.schemas import WorkflowCreate, WorkflowUpdate


class DuplicateWorkflow(Exception):
    """Another workflow already has this (source, external_id)."""


def _commit_or_duplicate(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DuplicateWorkflow from None


def create_workflow(db: Session, data: WorkflowCreate) -> Workflow:
    workflow = Workflow(**data.model_dump())
    db.add(workflow)
    _commit_or_duplicate(db)
    db.refresh(workflow)
    return workflow


def get_workflow(db: Session, workflow_id: int) -> Workflow | None:
    """Returns inactive workflows too, so they can be inspected and re-activated."""
    return db.get(Workflow, workflow_id)


def list_workflows(db: Session, include_inactive: bool = False) -> list[Workflow]:
    stmt = select(Workflow).order_by(Workflow.id)
    if not include_inactive:
        stmt = stmt.where(Workflow.is_active.is_(True))
    return list(db.scalars(stmt))


def update_workflow(
    db: Session, workflow_id: int, data: WorkflowUpdate
) -> Workflow | None:
    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(workflow, field, value)
    _commit_or_duplicate(db)
    db.refresh(workflow)
    return workflow


def deactivate_workflow(db: Session, workflow_id: int) -> bool:
    """Soft delete. Idempotent: deactivating an inactive workflow still succeeds."""
    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        return False
    workflow.is_active = False
    db.commit()
    return True
