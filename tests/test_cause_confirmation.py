"""V2-M4: a person confirms ONE hypothesis of a stored analysis; the only place "verified" exists."""
import logging

import pytest
from sqlalchemy import func, select

from app.analysis.analyzer import AnalyzerResult
from app.analysis.schema import AnalysisOutput, CheckedAnalysis, Hypothesis
import app.analysis.schema as analysis_schema
from app.main import app
from app.routers.incidents import get_analyzer
from app.models import (
    ExecutionEvent,
    ExecutionStatus,
    Incident,
    IncidentAnalysis,
    IncidentCauseConfirmation,
    IncidentTransition,
)
from app.services.incidents import rebuild_all
from tests.analysis_helpers import FakeAnalyzer, ev, good_output, ok_result
from tests.approval_helpers import (  # noqa: F401
    fail, fake_analyzer, incident_id, move, now, status_of, workflow_id,
)


def analyze(client, headers, incident_id, force=False):
    response = client.post(f"/api/incidents/{incident_id}/analyze?force={str(force).lower()}", headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()["analysis"]["id"]


def confirm(client, headers, incident_id, analysis_id, index=0, actor="alice", note="checked in n8n", **extra):
    return client.post(
        f"/api/incidents/{incident_id}/confirmations", headers=headers,
        json={"analysis_id": analysis_id, "hypothesis_index": index, "actor": actor, "note": note, **extra},
    )


def count(db, model):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


@pytest.fixture
def analysis_id(client, auth_headers, incident_id, fake_analyzer):
    return analyze(client, auth_headers, incident_id)


# ====================================================================== the happy path


def test_confirming_a_hypothesis_records_who_when_and_why(client, auth_headers, incident_id, fake_analyzer):
    grounded = Hypothesis(cause="The OAuth connection of the credential expired or was revoked.",
                          confidence="medium", supporting_evidence=[ev("untrusted.node_name", "HTTP")],
                          would_confirm=["Open the credential in n8n"], verification_steps=[])
    fake_analyzer._results = [ok_result(good_output(hypotheses=[grounded]))]
    analysis_id = analyze(client, auth_headers, incident_id)
    response = confirm(client, auth_headers, incident_id, analysis_id, note="Reconnected it, worked")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["analysis_id"] == analysis_id and body["hypothesis_index"] == 0
    assert body["verified_by"] == "alice" and body["note"] == "Reconnected it, worked"
    assert body["created_at"].endswith("Z")
    # the hypothesis is read back from the stored analysis, not copied from the request
    assert body["cause"] == "The OAuth connection of the credential expired or was revoked."
    assert body["confidence"] == "medium" and body["supported"] is True

    detail = client.get(f"/api/incidents/{incident_id}").json()
    assert detail["confirmed_cause"]["id"] == body["id"]


def test_an_incident_without_a_confirmation_has_none(client, incident_id):
    assert client.get(f"/api/incidents/{incident_id}").json()["confirmed_cause"] is None
    assert client.get(f"/api/incidents/{incident_id}/confirmations").json() == []


def test_confirming_changes_no_status_and_applies_nothing(client, auth_headers, incident_id, analysis_id, db_session):
    confirm(client, auth_headers, incident_id, analysis_id)
    assert status_of(client, incident_id) == "open"
    assert count(db_session, IncidentTransition) == 0
    assert count(db_session, IncidentAnalysis) == 1  # the analysis row is untouched, nothing was re-run


def test_the_newest_confirmation_is_current_and_the_old_one_stays(client, auth_headers, incident_id, analysis_id):
    first = confirm(client, auth_headers, incident_id, analysis_id, note="first guess").json()
    second = confirm(client, auth_headers, incident_id, analysis_id, note="turned out the same", actor="bob").json()
    assert client.get(f"/api/incidents/{incident_id}").json()["confirmed_cause"]["id"] == second["id"]
    history = client.get(f"/api/incidents/{incident_id}/confirmations").json()
    assert [h["id"] for h in history] == [second["id"], first["id"]]  # newest first
    assert history[1]["note"] == "first guess"


def test_a_hypothesis_the_checks_flagged_unsupported_may_still_be_confirmed(
    client, auth_headers, incident_id, fake_analyzer
):
    bad = Hypothesis(cause="The tenant was suspended.", confidence="medium",
                     supporting_evidence=[ev("untrusted.error.message", "this text is not in the message")],
                     would_confirm=["Ask the admin"], verification_steps=[])
    fake_analyzer._results = [ok_result(good_output(hypotheses=[bad]))]
    analysis = analyze(client, auth_headers, incident_id)
    response = confirm(client, auth_headers, incident_id, analysis, note="The admin confirmed it by phone")
    assert response.status_code == 201
    assert response.json()["supported"] is False  # the read still shows FlowOps found no evidence


def test_confirming_works_in_any_status(client, auth_headers, incident_id, analysis_id):
    move(client, auth_headers, incident_id, "acknowledged")
    move(client, auth_headers, incident_id, "resolved")
    assert confirm(client, auth_headers, incident_id, analysis_id).status_code == 201
    assert status_of(client, incident_id) == "resolved"


# ====================================================================== what is refused


def test_another_incidents_analysis_is_refused(client, auth_headers, incident_id, analysis_id, fail, db_session):
    other = fail("x1", message="a completely different failure 7", node="OTHER")["event"]["incident_id"]
    assert other != incident_id
    response = confirm(client, auth_headers, other, analysis_id)
    assert response.status_code == 422 and "this incident" in response.json()["detail"]
    assert count(db_session, IncidentCauseConfirmation) == 0


def test_an_unknown_analysis_is_refused(client, auth_headers, incident_id):
    assert confirm(client, auth_headers, incident_id, 9999).status_code == 422


@pytest.mark.parametrize("result", [
    AnalyzerResult("refused", refusal="No."),
    AnalyzerResult("incomplete", error_type="max_output_tokens"),
    AnalyzerResult("invalid", error_type="no_parsed_output"),
])
def test_an_analysis_that_is_not_ok_has_nothing_to_confirm(client, auth_headers, incident_id, fake_analyzer, result):
    fake_analyzer._results = [result]
    analysis = analyze(client, auth_headers, incident_id)
    response = confirm(client, auth_headers, incident_id, analysis)
    assert response.status_code == 422 and "status ok" in response.json()["detail"]


def test_the_status_is_checked_even_if_a_non_ok_row_somehow_holds_an_analysis(
    client, auth_headers, incident_id, analysis_id, db_session
):
    """Defence in depth: the invariant 'only ok rows have analysis_json' is not trusted blindly."""
    ok_row = db_session.get(IncidentAnalysis, analysis_id)
    refused = IncidentAnalysis(
        incident_id=incident_id, context_hash=ok_row.context_hash, context_version=ok_row.context_version,
        prompt_version="1", provider="fake", model="fake-model-2", status="refused",
        analysis_json=ok_row.analysis_json,
    )
    db_session.add(refused)
    db_session.commit()
    response = confirm(client, auth_headers, incident_id, refused.id)
    assert response.status_code == 422 and "status ok" in response.json()["detail"]


@pytest.mark.parametrize("index", [1, 2, 99])
def test_an_index_outside_the_stored_hypotheses_is_refused(client, auth_headers, incident_id, analysis_id, index):
    assert confirm(client, auth_headers, incident_id, analysis_id, index=index).status_code == 422


def test_an_analysis_with_no_hypotheses_cannot_be_confirmed(client, auth_headers, incident_id, fake_analyzer):
    fake_analyzer._results = [ok_result(good_output(hypotheses=[], insufficient_evidence=True))]
    analysis = analyze(client, auth_headers, incident_id)
    response = confirm(client, auth_headers, incident_id, analysis)
    assert response.status_code == 422 and "no hypotheses" in response.json()["detail"]


@pytest.mark.parametrize("override", [
    {"note": ""}, {"note": "   "}, {"note": "n" * 1001}, {"actor": ""}, {"actor": "a;b"},
    {"hypothesis_index": -1}, {"extra": 1}, {"analysis_id": "x"},
])
def test_bad_input_is_422(client, auth_headers, incident_id, analysis_id, db_session, override):
    body = {"analysis_id": analysis_id, "hypothesis_index": 0, "actor": "alice", "note": "ok", **override}
    response = client.post(f"/api/incidents/{incident_id}/confirmations", headers=auth_headers, json=body)
    assert response.status_code == 422
    assert count(db_session, IncidentCauseConfirmation) == 0


def test_a_missing_note_is_422(client, auth_headers, incident_id, analysis_id):
    body = {"analysis_id": analysis_id, "hypothesis_index": 0, "actor": "alice"}
    assert client.post(f"/api/incidents/{incident_id}/confirmations", headers=auth_headers, json=body).status_code == 422


def test_confirmations_need_the_token_first(client, incident_id, analysis_id, db_session):
    url = f"/api/incidents/{incident_id}/confirmations"
    assert client.post(url, json={"analysis_id": analysis_id, "hypothesis_index": 0, "actor": "a", "note": "n"}).status_code == 401
    assert client.post(url, headers={"X-FlowOps-Token": "wrong"}, json={}).status_code == 401  # beats 422
    assert client.post("/api/incidents/999/confirmations", json={}).status_code == 401  # beats 404
    assert count(db_session, IncidentCauseConfirmation) == 0


def test_unknown_incident_is_404(client, auth_headers, analysis_id):
    assert confirm(client, auth_headers, 999, analysis_id).status_code == 404
    assert client.get("/api/incidents/999/confirmations").status_code == 404


def test_the_note_is_redacted_and_never_logged(client, auth_headers, incident_id, analysis_id, caplog):
    caplog.set_level(logging.DEBUG)
    response = confirm(client, auth_headers, incident_id, analysis_id, actor="logged-actor-name",
                       note="it was Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in response.json()["note"]
    assert "logged-actor-name" not in caplog.text and "abcdefghijklmnop" not in caplog.text


# ====================================================================== history survives a rebuild


def test_rebuild_keeps_an_orphan_incident_that_has_history_and_deletes_an_empty_one(
    client, auth_headers, incident_id, analysis_id, fail, db_session
):
    confirm(client, auth_headers, incident_id, analysis_id)
    move(client, auth_headers, incident_id, "acknowledged")
    empty = fail("x1", message="a different failure 5", node="OTHER")["event"]["incident_id"]
    # both incidents lose all their events (as if a signature rule had changed)
    for event in db_session.scalars(select(ExecutionEvent)):
        event.incident_id = None
        event.status = ExecutionStatus.SUCCESS
    db_session.commit()

    result = rebuild_all(db_session)  # must not fail on the foreign keys
    assert (result.incidents_removed, result.incidents_kept, result.incidents_total) == (1, 1, 1)
    db_session.expire_all()
    assert db_session.get(Incident, incident_id) is not None and db_session.get(Incident, empty) is None
    assert count(db_session, IncidentAnalysis) == 1
    assert count(db_session, IncidentCauseConfirmation) == 1
    assert count(db_session, IncidentTransition) == 1
    again = rebuild_all(db_session)  # idempotent
    assert (again.incidents_removed, again.incidents_kept, again.incidents_total) == (0, 1, 1)


# ====================================================================== "verified" is human-only


def schema_names_with_verified(node, path=""):
    """Every OpenAPI/JSON-schema property or schema name containing 'verified' -> its path."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if "verified" in str(key).lower():
                found.append(f"{path}/{key}")
            found += schema_names_with_verified(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += schema_names_with_verified(value, f"{path}[{i}]")
    return found


def test_the_word_verified_appears_only_in_the_confirmation_schemas(client):
    spec = client.get("/openapi.json").json()
    hits = schema_names_with_verified(spec["components"]["schemas"])
    assert hits, "the confirmation schema should carry verified_by"
    assert all(h.startswith("/ConfirmationRead/") for h in hits), hits
    # nothing else in the API surface (paths, parameters) uses the word either
    assert schema_names_with_verified(spec["paths"]) == []


def test_the_model_output_schemas_have_no_verified_field_at_all():
    for model in (AnalysisOutput, CheckedAnalysis):
        assert schema_names_with_verified(model.model_json_schema()) == []
    assert not hasattr(analysis_schema, "VerifiedAnalysis")


def test_nothing_the_model_returns_can_confirm_anything(client, auth_headers, incident_id, db_session):
    """Analyzing, even with a model that insists on certainty, never creates a confirmation."""
    shouty = good_output(summary="THE ROOT CAUSE IS VERIFIED AND CONFIRMED.")
    app.dependency_overrides[get_analyzer] = lambda: FakeAnalyzer([ok_result(shouty)])
    analyze(client, auth_headers, incident_id)
    assert count(db_session, IncidentCauseConfirmation) == 0
    assert client.get(f"/api/incidents/{incident_id}").json()["confirmed_cause"] is None
