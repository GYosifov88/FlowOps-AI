"""Logging setup: one handler, level from the environment, one line per record.

Every record carries `request_id` (from `request_id_var`, "-" outside a request), so a
single request can be followed through the log. Timestamps are UTC, like the database.
"""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

FORMATS = ("text", "json")
_HANDLER_TAG = "_flowops_handler"


def _install_record_factory() -> None:
    """Stamp request_id on every record at creation time, whatever handler sees it.

    Done with a record factory rather than a handler filter, so records reaching other
    handlers (pytest's caplog, uvicorn's) carry it too, and so a record created in a
    threadpool thread still gets the id of the request that thread is serving.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_flowops", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    factory._flowops = True
    logging.setLogRecordFactory(factory)


_install_record_factory()


def _timestamp(record: logging.LogRecord) -> str:
    created = datetime.fromtimestamp(record.created, timezone.utc)
    return created.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TextFormatter(logging.Formatter):
    """`2026-10-29T09:15:02.123Z INFO  app.access [req=...] message`, then any traceback.

    Newlines in the message are escaped, so client-controlled text (a URL path) cannot
    forge extra log lines. Only a traceback may span several lines.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().replace("\r", "\\r").replace("\n", "\\n")
        line = (
            f"{_timestamp(record)} {record.levelname:<5} {record.name} "
            f"[req={getattr(record, 'request_id', '-')}] {message}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line (json.dumps escapes newlines, tracebacks included)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def parse_level(level: str) -> int:
    value = logging.getLevelNamesMapping().get(level.strip().upper())
    if value is None or level.strip().upper() == "NOTSET":
        raise ValueError(
            f"LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL (got {level!r})"
        )
    return value


def configure_logging(level: str, fmt: str) -> None:
    """Install the FlowOps handler on the root logger. Safe to call more than once.

    Only our own (tagged) handler is replaced; other handlers, such as pytest's, are left
    alone. Uvicorn's loggers are routed through the same handler, and uvicorn's access
    log is switched off because the request middleware writes a better one.
    """
    numeric_level = parse_level(level)
    fmt = fmt.strip().lower()
    if fmt not in FORMATS:
        raise ValueError(f"LOG_FORMAT must be 'text' or 'json' (got {fmt!r})")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    setattr(handler, _HANDLER_TAG, True)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.NullHandler()]
    access.propagate = False
