from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import WorkflowCreate, WorkflowRead, WorkflowUpdate
from app.services import workflows as service

router = APIRouter(prefix="/api/workflows", tags=["workflows"])



def not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")


@router.post("", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
def create_workflow(data: WorkflowCreate, db: Session = Depends(get_db)):
    return service.create_workflow(db, data)


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
    workflow = service.update_workflow(db, workflow_id, data)
    if workflow is None:
        raise not_found()
    return workflow


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)):
    """Soft delete: sets is_active=False. The row and its events are kept."""
    if not service.deactivate_workflow(db, workflow_id):
        raise not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
