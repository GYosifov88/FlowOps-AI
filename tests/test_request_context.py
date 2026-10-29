import logging
import re

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.logging_config import TextFormatter, request_id_var
from app.main import app
from app.middleware import is_valid_request_id
from app.models import ExecutionEvent, ExecutionStatus, Workflow

HEX_ID = re.compile(r"[0-9a-f]{32}")


@pytest.fixture
def workflow_id(client):
    return client.post("/api/workflows", json={"name": "W", "source": "n8n"}).json()["id"]


@pytest.fixture
def broken_db(client):
    """Make every DB-backed route fail with an unexpected error."""

    def boom():
        raise RuntimeError("boom-SECRET-detail")

    app.dependency_overrides[get_db] = boom
    return client


def access_records(caplog):
    return [r for r in caplog.records if r.name == "app.access"]


# ---------- request id ----------


def test_a_request_id_is_generated_when_none_is_sent(client):
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]

    assert HEX_ID.fullmatch(first) and HEX_ID.fullmatch(second)
    assert first != second


def test_a_well_formed_incoming_id_is_reused(client):
    response = client.get("/health", headers={"X-Request-ID": "n8n-exec-123"})

    assert response.headers["X-Request-ID"] == "n8n-exec-123"


@pytest.mark.parametrize("hostile", ["a" * 65, "has space", "<script>", "semi;colon", "ünïcode"])
def test_a_malformed_incoming_id_is_replaced_and_never_logged(client, caplog, hostile):
    with caplog.at_level(logging.INFO):
        response = client.get("/health", headers={"X-Request-ID": hostile.encode("utf-8")})

    assert HEX_ID.fullmatch(response.headers["X-Request-ID"])
    assert all(r.request_id != hostile for r in caplog.records)
    assert hostile not in caplog.text


@pytest.mark.parametrize("value", ["abc\ninjected", "abc\r\nX: y", ""])
def test_ids_with_line_breaks_or_empty_are_invalid(value):
    assert not is_valid_request_id(value)


def test_the_header_is_on_every_kind_of_response(client, workflow_id, auth_headers):
    responses = {
        "200 json": client.get("/api/workflows"),
        "201": client.post("/api/workflows", json={"name": "X", "source": "n8n"}),
        "401": client.post("/api/events", json={"workflow_id": workflow_id, "status": "success"}),
        "404": client.get("/api/workflows/999"),
        "422": client.get("/api/events", params={"limit": 999}),
        "html": client.get("/"),
        "unknown route": client.get("/no-such-page"),
    }

    for name, response in responses.items():
        assert HEX_ID.fullmatch(response.headers.get("X-Request-ID", "")), name


def test_the_request_id_reaches_logs_written_inside_services(client, workflow_id, auth_headers, caplog):
    client.post("/api/events", json={"workflow_id": workflow_id, "external_event_id": "e",
                                     "status": "success"}, headers=auth_headers)

    with caplog.at_level(logging.WARNING, logger="app.services.events"):
        response = client.post(
            "/api/events",
            json={"workflow_id": workflow_id, "external_event_id": "e", "status": "failed"},
            headers={**auth_headers, "X-Request-ID": "trace-me-42"},
        )

    warning = next(r for r in caplog.records if r.name == "app.services.events")
    assert "already terminal" in warning.getMessage()
    assert warning.request_id == "trace-me-42" == response.headers["X-Request-ID"]


def test_the_request_id_does_not_leak_past_the_request(client):
    client.get("/health", headers={"X-Request-ID": "short-lived"})

    assert request_id_var.get() == "-"


# ---------- access log ----------


def test_one_access_line_per_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.access"):
        response = client.get("/api/workflows/999")

    (record,) = access_records(caplog)
    assert re.fullmatch(r"GET /api/workflows/999 404 \d+ms", record.getMessage())
    assert record.request_id == response.headers["X-Request-ID"]


def test_access_log_has_no_token_query_string_or_body(client, workflow_id, auth_headers, caplog):
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/events",
            json={"workflow_id": workflow_id, "status": "failed",
                  "error_raw": "password=BODY-SECRET-1"},
            headers=auth_headers,
        )
        client.get("/api/events", params={"status": "failed", "started_from": "2026-10-27"})

    token = auth_headers["X-FlowOps-Token"]
    assert token not in caplog.text
    assert "BODY-SECRET-1" not in caplog.text
    # The test client (httpx2) logs its own request URL; only our app's records matter here.
    app_text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app."))
    assert "started_from" not in app_text and "?" not in app_text
    assert [r.getMessage().split(" ")[:3] for r in access_records(caplog)] == [
        ["POST", "/api/events", "201"],
        ["GET", "/api/events", "200"],
    ]


def test_a_newline_in_the_url_path_cannot_forge_a_log_line(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.access"):
        client.get("/api/workflows/1%0A2026-01-01T00:00:00.000Z%20INFO%20forged")

    (record,) = access_records(caplog)
    assert "\n" in record.getMessage()  # the decoded path really contains it...
    assert "\n" not in TextFormatter().format(record)  # ...but the log line does not


# ---------- global exception handler ----------


def test_an_unhandled_error_becomes_a_clean_json_500(broken_db):
    response = broken_db.get("/api/workflows")

    assert response.status_code == 500
    request_id = response.headers["X-Request-ID"]
    assert response.json() == {"detail": "Internal Server Error", "request_id": request_id}
    assert "Traceback" not in response.text and "boom" not in response.text


def test_the_full_traceback_is_logged_server_side_with_the_request_id(broken_db, caplog):
    with caplog.at_level(logging.INFO):
        response = broken_db.get("/api/workflows", headers={"X-Request-ID": "fail-7"})

    (error,) = [r for r in caplog.records if r.name == "app.errors"]
    assert error.levelno == logging.ERROR and error.request_id == "fail-7"
    assert error.exc_info and "boom-SECRET-detail" in str(error.exc_info[1])
    assert "Traceback" in TextFormatter().format(error)
    (access,) = access_records(caplog)
    assert access.getMessage().startswith("GET /api/workflows 500")
    assert response.headers["X-Request-ID"] == "fail-7"


def test_html_pages_get_the_same_clean_500(broken_db):
    response = broken_db.get("/")

    assert response.status_code == 500
    assert response.json()["detail"] == "Internal Server Error"


def test_expected_http_errors_are_unchanged(client):
    response = client.get("/api/workflows/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Workflow not found"}


# ---------- DB errors keep their values out of the log ----------


def test_db_errors_do_not_include_parameter_values(db_session):
    db_session.add(Workflow(name="w", source="n8n"))
    db_session.commit()
    for _ in range(2):
        db_session.add(ExecutionEvent(workflow_id=1, external_event_id="VALUE-SECRET-9",
                                      status=ExecutionStatus.SUCCESS))
    with pytest.raises(IntegrityError) as caught:
        db_session.commit()

    assert "VALUE-SECRET-9" not in str(caught.value)
    assert "hidden due to hide_parameters" in str(caught.value)
