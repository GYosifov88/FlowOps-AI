from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import WorkflowCreate, WorkflowRead, WorkflowUpdate
from app.services import workflows as service

router = APIRouter(prefix="/api/workflows", tags=["workflows"])



def duplicate() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        "A workflow with this source and external_id already exists",
    )


def not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")


@router.post("", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
def create_workflow(data: WorkflowCreate, db: Session = Depends(get_db)):
    try:
        return service.create_workflow(db, data)
    except service.DuplicateWorkflow:
        raise duplicate()


@router.get("", response_model=list[WorkflowRead])
def list_workflows(include_inactive: bool = False, db: Session = Depends(get_db)):
    """Active workflows only, unless `include_inactive=true`."""
    return service.list_workflows(db, include_inactive)


@router.get("/{workflow_id}", response_model=WorkflowRead)
def get_workflow(workflow_id: int, db: Session = Depends(get_db)):
    """Also returns deactivated workflows."""
    workflow = service.get_workflow(db, workflow_id)
    if workflow is None:
        raise not_found()
    return workflow


@router.patch("/{workflow_id}", response_model=WorkflowRead)
def update_workflow(
    workflow_id: int, data: WorkflowUpdate, db: Session = Depends(get_db)
):
    """Partial update. Send `is_active: true` to re-activate a deactivated workflow."""
    try:
        workflow = service.update_workflow(db, workflow_id, data)
    except service.DuplicateWorkflow:
        raise duplicate()
    if workflow is None:
        raise not_found()
    return workflow


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)):
    """Soft delete: sets is_active=False. The row and its events are kept."""
    if not service.deactivate_workflow(db, workflow_id):
        raise not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
