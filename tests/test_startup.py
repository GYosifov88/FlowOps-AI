"""The app must create missing tables itself on startup.

Every other test uses the `engine` fixture, which calls init_db() for you -- so none of them
could notice that the real app never did. These tests deliberately start from a database
file that has NO schema.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.main as main_module
from app.config import Settings, get_settings
from app.database import create_db_engine, get_db
from app.main import app
from app.models import ExecutionEvent, Workflow

TOKEN = "startup-test-token"


def settings_with(token: str):
    return lambda: Settings(
        app_name="t", log_level="INFO", database_url="unused", ingest_token=token
    )


@pytest.fixture
def blank_engine(tmp_path, monkeypatch):
    """An engine on a brand-new file with no tables, installed as the app's engine."""
    eng = create_db_engine(f"sqlite:///{(tmp_path / 'blank.db').as_posix()}")
    assert inspect(eng).get_table_names() == []
    monkeypatch.setattr(database, "engine", eng)  # what init_db() falls back to
    factory = sessionmaker(bind=eng)

    def get_blank_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = get_blank_db
    app.dependency_overrides[get_settings] = settings_with(TOKEN)
    monkeypatch.setattr(main_module, "get_settings", settings_with(TOKEN))
    yield eng
    app.dependency_overrides.clear()
    eng.dispose()


def tables(eng) -> set[str]:
    return set(inspect(eng).get_table_names())


def test_startup_creates_the_schema_on_an_empty_database(blank_engine):
    with TestClient(app):
        assert {"workflows", "execution_events"} <= tables(blank_engine)


def test_a_fresh_database_serves_real_requests_without_reset_db(blank_engine):
    headers = {"X-FlowOps-Token": TOKEN}
    with TestClient(app) as client:
        created = client.post("/api/workflows", json={"name": "W", "source": "n8n"})
        assert created.status_code == 201
        posted = client.post(
            "/api/events",
            json={"workflow_id": created.json()["id"], "status": "success"},
            headers=headers,
        )
        assert posted.status_code == 201
        listing = client.get("/api/events")
        assert listing.status_code == 200 and listing.json()["total"] == 1
        assert client.get("/api/workflows").json()[0]["name"] == "W"


def test_startup_is_idempotent_and_never_touches_existing_data(blank_engine):
    with TestClient(app) as client:
        client.post("/api/workflows", json={"name": "keep me", "source": "n8n"})

    with TestClient(app) as client:  # a restart over the same file
        names = [w["name"] for w in client.get("/api/workflows").json()]

    assert names == ["keep me"]


def test_startup_does_not_seed_any_data(blank_engine):
    with TestClient(app):
        pass

    with sessionmaker(bind=blank_engine)() as session:
        assert session.scalar(select(func.count()).select_from(Workflow)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionEvent)) == 0


def test_refusing_to_start_for_a_missing_token_leaves_the_database_alone(
    blank_engine, monkeypatch
):
    monkeypatch.setattr(main_module, "get_settings", settings_with(""))

    with pytest.raises(RuntimeError, match="FLOWOPS_INGEST_TOKEN"):
        with TestClient(app):
            pass

    assert tables(blank_engine) == set()
