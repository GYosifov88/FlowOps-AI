"""POST /api/incidents/{id}/analyze and GET /api/incidents/{id}/analyses."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.analysis.analyzer import AnalyzerResult
from app.config import Settings
from app.main import app
from app.models import ExecutionEvent, IncidentAnalysis
from app.routers.incidents import get_analyzer
from tests.analysis_helpers import FakeAnalyzer, ok_result
from tests.corpus_helpers import ingest_case, load_corpus, register_workflows

CORPUS = load_corpus()
CASES = {case["id"]: case for case in CORPUS["cases"]}


@pytest.fixture
def incident_id(db_session):
    workflow_ids = register_workflows(db_session, CORPUS)
    event = None
    for case_id in ("c01", "c02", "c03"):
        event = ingest_case(db_session, CASES[case_id], workflow_ids)
    db_session.expire_all()
    return db_session.get(ExecutionEvent, event.id).incident_id


@pytest.fixture
def fake(client):
    analyzer = FakeAnalyzer()
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    return analyzer


def url(incident_id, suffix="analyze"):
    return f"/api/incidents/{incident_id}/{suffix}"


def stored(db_session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(IncidentAnalysis))


# ====================================================================== auth and configuration


def test_analyze_requires_the_token_before_anything_else(client, fake, incident_id, db_session):
    assert client.post(url(incident_id)).status_code == 401
    assert client.post(url(incident_id), headers={"X-FlowOps-Token": "wrong"}).status_code == 401
    assert fake.calls == [] and stored(db_session) == 0


def test_a_missing_token_beats_a_missing_api_key(client, incident_id):
    """No analyzer override and no key configured: still 401 (auth first), never 503."""
    assert client.post(url(incident_id)).status_code == 401


def test_without_an_api_key_analyze_is_unavailable_but_nothing_else_is(client, auth_headers, incident_id, db_session):
    response = client.post(url(incident_id), headers=auth_headers)
    assert response.status_code == 503 and "OPENAI_API_KEY" in response.json()["detail"]
    assert stored(db_session) == 0
    assert client.get(url(incident_id, "context")).status_code == 200
    assert client.get(url(incident_id, "analyses")).json() == []


def test_the_app_starts_without_an_openai_key(monkeypatch):
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(
        app_name="t", log_level="INFO", database_url="unused", ingest_token="tok"))
    with TestClient(app) as started:
        assert started.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("field, value", [
    ("openai_reasoning_effort", "turbo"), ("openai_max_output_tokens", 0),
    ("openai_timeout_seconds", 0), ("openai_model", ""),
])
def test_a_bad_openai_setting_stops_startup(monkeypatch, field, value):
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(
        app_name="t", log_level="INFO", database_url="unused", ingest_token="tok", **{field: value}))
    with pytest.raises(ValueError):
        with TestClient(app):
            pass


def test_get_analyzer_refuses_without_a_key():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        get_analyzer(Settings(app_name="t", log_level="INFO", database_url="unused"))
    assert caught.value.status_code == 503


# ====================================================================== analyzing


def test_analyze_creates_then_reuses(client, auth_headers, fake, incident_id, db_session):
    first = client.post(url(incident_id), headers=auth_headers)
    second = client.post(url(incident_id), headers=auth_headers)

    assert (first.status_code, second.status_code) == (201, 200)
    body = first.json()
    assert body["result"] == "created" and second.json()["result"] == "reused"
    assert second.json()["analysis"]["id"] == body["analysis"]["id"]
    analysis = body["analysis"]
    assert analysis["status"] == "ok" and analysis["model"] == "fake-model-1" and analysis["forced"] is False
    assert analysis["incident_id"] == incident_id and len(analysis["context_hash"]) == 64
    assert analysis["analysis"]["category"] == "auth_credential"
    assert analysis["analysis"]["hypotheses"][0]["supported"] is True
    assert analysis["validation_notes"] == [] and analysis["created_at"].endswith("Z")
    assert len(fake.calls) == 1 and stored(db_session) == 1


def test_the_response_has_no_verified_flag_and_does_not_echo_the_context(client, auth_headers, fake, incident_id):
    text = client.post(url(incident_id), headers=auth_headers).text
    # (evidence refs legitimately contain the word "untrusted"; the context's own fields must not appear)
    for forbidden in ("verified", "root_cause", "error_raw", '"context"', '"occurrences"',
                      '"normalized_message"', '"signature"', '"workflow_name"'):
        assert forbidden not in text


def test_force_runs_again_and_history_lists_newest_first(client, auth_headers, fake, incident_id):
    first = client.post(url(incident_id), headers=auth_headers).json()["analysis"]["id"]
    forced = client.post(url(incident_id) + "?force=true", headers=auth_headers)

    assert forced.status_code == 201 and forced.json()["analysis"]["forced"] is True
    listing = client.get(url(incident_id, "analyses")).json()
    assert [a["id"] for a in listing] == [forced.json()["analysis"]["id"], first]
    assert len(fake.calls) == 2


def test_unknown_incident(client, auth_headers, fake):
    assert client.post(url(999), headers=auth_headers).status_code == 404
    assert client.get(url(999, "analyses")).status_code == 404
    assert client.post("/api/incidents/abc/analyze", headers=auth_headers).status_code == 422
    assert fake.calls == []


def test_analyze_is_post_only(client, fake, incident_id):
    assert client.get(url(incident_id)).status_code == 405


# ====================================================================== failures


def test_a_refusal_is_stored_and_returned_with_its_status(client, auth_headers, incident_id):
    app.dependency_overrides[get_analyzer] = lambda: FakeAnalyzer([AnalyzerResult("refused", refusal="No.")])
    response = client.post(url(incident_id), headers=auth_headers)
    assert response.status_code == 201
    analysis = response.json()["analysis"]
    assert (analysis["status"], analysis["refusal"], analysis["analysis"]) == ("refused", "No.", None)


def test_an_incomplete_answer_is_stored_and_returned(client, auth_headers, incident_id):
    app.dependency_overrides[get_analyzer] = lambda: FakeAnalyzer(
        [AnalyzerResult("incomplete", error_type="max_output_tokens", output_tokens=16000)])
    analysis = client.post(url(incident_id), headers=auth_headers).json()["analysis"]
    assert (analysis["status"], analysis["error_type"], analysis["analysis"]) == ("incomplete", "max_output_tokens", None)


def test_an_invalid_answer_is_stored_and_returned(client, auth_headers, incident_id):
    app.dependency_overrides[get_analyzer] = lambda: FakeAnalyzer([AnalyzerResult("invalid", error_type="no_parsed_output")])
    assert client.post(url(incident_id), headers=auth_headers).json()["analysis"]["status"] == "invalid"


def test_a_failed_call_is_a_502_but_the_attempt_is_stored(client, auth_headers, incident_id, db_session):
    app.dependency_overrides[get_analyzer] = lambda: FakeAnalyzer(
        [AnalyzerResult("error", error_type="APITimeoutError")])

    response = client.post(url(incident_id), headers=auth_headers)

    assert response.status_code == 502
    assert "APITimeoutError" in response.json()["detail"] and "stored as analysis" in response.json()["detail"]
    assert stored(db_session) == 1
    (attempt,) = client.get(url(incident_id, "analyses")).json()
    assert attempt["status"] == "error" and attempt["analysis"] is None


def test_after_a_failure_a_plain_retry_calls_the_model_again(client, auth_headers, incident_id):
    analyzer = FakeAnalyzer([AnalyzerResult("error", error_type="APITimeoutError"), ok_result()])
    app.dependency_overrides[get_analyzer] = lambda: analyzer
    assert client.post(url(incident_id), headers=auth_headers).status_code == 502
    assert client.post(url(incident_id), headers=auth_headers).status_code == 201
    assert len(analyzer.calls) == 2


# ====================================================================== never automatic


def test_nothing_but_analyze_ever_calls_the_model(client, auth_headers, fake, incident_id):
    workflow_id = client.post("/api/workflows", json={"name": "W", "source": "n8n"}).json()["id"]
    client.post("/api/events", headers=auth_headers, json={
        "workflow_id": workflow_id, "status": "failed", "error_raw": "boom 11"})
    for path in ("/api/incidents", f"/api/incidents/{incident_id}", url(incident_id, "context"),
                 url(incident_id, "analyses"), "/", "/events", "/workflows"):
        assert client.get(path).status_code == 200, path
    assert fake.calls == []
