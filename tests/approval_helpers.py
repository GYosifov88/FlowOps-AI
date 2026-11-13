"""Shared setup for the V2-M4 approval tests (lifecycle, regression, confirmation)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.main import app
from app.routers.incidents import get_analyzer
from tests.analysis_helpers import FakeAnalyzer


def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def workflow_id(client):
    return client.post("/api/workflows", json={"name": "W", "source": "n8n"}).json()["id"]


@pytest.fixture
def fail(client, auth_headers, workflow_id):
    """fail(ext, started=<datetime>, message=..., status="failed") -> the ingest response body."""

    def _fail(ext, *, started=None, message="boom 1", status="failed", node="HTTP"):
        body = {
            "workflow_id": workflow_id, "external_event_id": ext, "status": status,
            "started_at": (started or now() - timedelta(hours=2)).isoformat(),
            "node_name": node, "error_raw": message,
        }
        response = client.post("/api/events", headers=auth_headers, json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    return _fail


@pytest.fixture
def incident_id(fail):
    """One open incident (a failure two hours ago)."""
    return fail("e1")["event"]["incident_id"]


@pytest.fixture
def fake_analyzer(client):
    analyzer = FakeAnalyzer()
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    return analyzer


def move(client, headers, incident_id, to, actor="alice", **extra):
    return client.post(
        f"/api/incidents/{incident_id}/transitions", headers=headers, json={"to": to, "actor": actor, **extra}
    )


def status_of(client, incident_id) -> str:
    return client.get(f"/api/incidents/{incident_id}").json()["status"]
