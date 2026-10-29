import json
import logging
import re
import sys

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.logging_config import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    request_id_var,
)
from app.main import app


def flowops_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, "_flowops_handler", False)]


def make_record(message="hello", level=logging.INFO, exc_info=None, request_id="req-1"):
    token = request_id_var.set(request_id)
    try:
        return logging.getLogger("app.test").makeRecord(
            "app.test", level, __file__, 1, message, (), exc_info
        )
    finally:
        request_id_var.reset(token)


def settings_with(**overrides):
    values = dict(app_name="t", log_level="INFO", database_url="unused", ingest_token="tok")
    values.update(overrides)
    return lambda: Settings(**values)


# ---------- configure_logging ----------


@pytest.mark.parametrize("level, expected", [("DEBUG", logging.DEBUG), ("warning", logging.WARNING)])
def test_level_comes_from_the_setting(level, expected):
    configure_logging(level, "text")

    assert logging.getLogger().level == expected
    assert logging.getLogger("app.services.events").getEffectiveLevel() == expected


@pytest.mark.parametrize("level", ["LOUD", "", "NOTSET", "10"])
def test_an_invalid_level_is_rejected(level):
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        configure_logging(level, "text")


def test_an_invalid_format_is_rejected():
    with pytest.raises(ValueError, match="LOG_FORMAT"):
        configure_logging("INFO", "xml")


def test_configuring_twice_leaves_one_handler():
    configure_logging("INFO", "text")
    configure_logging("DEBUG", "json")

    handlers = flowops_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_other_handlers_are_left_alone():
    foreign = logging.NullHandler()
    logging.getLogger().addHandler(foreign)

    configure_logging("INFO", "text")

    assert foreign in logging.getLogger().handlers


def test_uvicorn_logs_go_through_our_handler_and_its_access_log_is_off():
    configure_logging("INFO", "text")

    assert logging.getLogger("uvicorn").handlers == []
    assert logging.getLogger("uvicorn").propagate is True
    assert logging.getLogger("uvicorn.access").propagate is False


# ---------- formatters ----------


def test_text_format_has_utc_time_level_logger_and_request_id():
    line = TextFormatter().format(make_record("hello world"))

    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z INFO  app\.test \[req=req-1\] hello world", line
    )


def test_text_format_escapes_newlines_so_lines_cannot_be_forged():
    line = TextFormatter().format(make_record("GET /x\n2026-01-01T00:00:00.000Z INFO fake"))

    assert "\n" not in line
    assert "\\n2026-01-01" in line


def test_text_format_appends_the_traceback():
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        record = make_record("failed", logging.ERROR, exc_info=sys.exc_info())

    out = TextFormatter().format(record)

    assert out.splitlines()[0].endswith("failed")
    assert "Traceback (most recent call last)" in out and "RuntimeError: kaboom" in out


def test_json_format_is_one_parseable_line_with_all_fields():
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        record = make_record("multi\nline", logging.ERROR, exc_info=sys.exc_info())

    out = JsonFormatter().format(record)

    assert "\n" not in out
    entry = json.loads(out)
    assert entry["level"] == "ERROR" and entry["logger"] == "app.test"
    assert entry["request_id"] == "req-1" and entry["message"] == "multi\nline"
    assert entry["ts"].endswith("Z")
    assert "RuntimeError: kaboom" in entry["exc"]


def test_records_outside_a_request_get_a_dash():
    record = logging.getLogger("x").makeRecord("x", logging.INFO, __file__, 1, "m", (), None)

    assert record.request_id == "-"


# ---------- startup ----------


def test_startup_applies_the_logging_settings(monkeypatch):
    monkeypatch.setattr(main_module, "get_settings", settings_with(log_level="DEBUG", log_format="json"))

    with TestClient(app):
        assert logging.getLogger().level == logging.DEBUG
        assert isinstance(flowops_handlers()[0].formatter, JsonFormatter)


@pytest.mark.parametrize("overrides, message", [
    ({"log_level": "LOUD"}, "LOG_LEVEL"),
    ({"log_format": "xml"}, "LOG_FORMAT"),
])
def test_startup_refuses_invalid_logging_settings(monkeypatch, overrides, message):
    monkeypatch.setattr(main_module, "get_settings", settings_with(**overrides))

    with pytest.raises(ValueError, match=message):
        with TestClient(app):
            pass


def test_log_format_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")

    assert get_settings().log_format == "json"
