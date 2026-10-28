import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.main import app

BODY = {"workflow_id": 1, "status": "success"}


def settings_with(token: str):
    return lambda: Settings(
        app_name="t", log_level="INFO", database_url="unused", ingest_token=token
    )


def test_missing_and_wrong_tokens_get_the_same_401(client):
    missing = client.post("/api/events", json=BODY)
    wrong = client.post("/api/events", json=BODY, headers={"X-FlowOps-Token": "nope"})

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


def test_a_non_ascii_token_is_a_401_not_a_500(client):
    response = client.post(
        "/api/events", json=BODY, headers={"X-FlowOps-Token": "tökén-ünïcode".encode("utf-8")}
    )

    assert response.status_code == 401


def test_a_bad_payload_without_a_token_is_401_not_422(client):
    response = client.post("/api/events", json={"nonsense": True})

    assert response.status_code == 401


def test_an_empty_configured_token_rejects_everything(client):
    app.dependency_overrides[get_settings] = settings_with("")

    for headers in ({}, {"X-FlowOps-Token": ""}):
        assert client.post("/api/events", json=BODY, headers=headers).status_code == 401


def test_the_app_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setattr(main_module, "get_settings", settings_with(""))

    with pytest.raises(RuntimeError, match="FLOWOPS_INGEST_TOKEN"):
        with TestClient(app):
            pass


def test_the_app_starts_with_a_token(monkeypatch):
    monkeypatch.setattr(main_module, "get_settings", settings_with("something"))

    with TestClient(app) as running:
        assert running.get("/health").status_code == 200


def test_a_blank_token_in_the_environment_counts_as_unset(monkeypatch):
    monkeypatch.setenv("FLOWOPS_INGEST_TOKEN", "   ")

    assert get_settings().ingest_token == ""
