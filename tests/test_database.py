import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExecutionEvent, ExecutionStatus, Workflow


def test_sqlite_pragmas_are_applied_to_new_connections(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000


def test_foreign_keys_are_actually_enforced(db_session):
    db_session.add(
        ExecutionEvent(workflow_id=999, status=ExecutionStatus.SUCCESS)
    )

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db_session.commit()


def test_get_db_can_be_overridden_with_a_per_test_database(engine, override_get_db):
    """Proves the fixture wiring M3+ will rely on: the route sees the TEST database."""
    with Session(engine) as seed:
        seed.add(Workflow(name="wf", source="n8n"))
        seed.commit()

    app = FastAPI()

    @app.get("/count")
    def count(db: Session = Depends(get_db)) -> dict[str, int]:
        return {"workflows": db.scalar(select(func.count()).select_from(Workflow))}

    app.dependency_overrides[get_db] = override_get_db

    response = TestClient(app).get("/count")

    assert response.status_code == 200
    assert response.json() == {"workflows": 1}
