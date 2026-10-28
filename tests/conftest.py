"""Shared fixtures. Every test gets its own fresh SQLite file under pytest's tmp_path,
so tests are order-independent and nothing is written into the repo."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.database import create_db_engine, get_db, init_db
from app.main import app


@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    init_db(eng)
    yield eng
    eng.dispose()  # release the file handles, or Windows cannot clean up tmp_path


@pytest.fixture
def db_session(engine):
    with sessionmaker(bind=engine)() as session:
        yield session


@pytest.fixture
def override_get_db(engine):
    """A replacement for app.database.get_db that yields sessions on the test engine.

    Use as: app.dependency_overrides[get_db] = override_get_db
    """
    factory = sessionmaker(bind=engine)

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    return _override


TEST_TOKEN = "test-token-123"


@pytest.fixture
def client(override_get_db):
    """TestClient for the real app, wired to the per-test database and a known ingest token."""
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_name="test", log_level="INFO", database_url="unused", ingest_token=TEST_TOKEN
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    return {"X-FlowOps-Token": TEST_TOKEN}
