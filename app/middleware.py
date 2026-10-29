"""Per-request context: request id, access log line, and the catch-all error handler.

A pure ASGI middleware (not BaseHTTPMiddleware), so it sees every response, including
the 500 it produces itself. Starlette's `exception_handler(Exception)` would run outside
user middleware: the 500 would lack the request id header and uvicorn would log the
traceback a second time.
"""
import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

from app.logging_config import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"
# Client-supplied ids are reused only if they are short and plain, so they cannot inject
# fake log lines or markup. n8n can send its execution id here to trace one event.
_VALID_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

access_logger = logging.getLogger("app.access")
error_logger = logging.getLogger("app.errors")


def is_valid_request_id(value: str) -> bool:
    return _VALID_ID.fullmatch(value) is not None


def _incoming_request_id(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            text = value.decode("latin-1")
            return text if is_valid_request_id(text) else None
    return None


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # Full traceback server-side only; the client gets a clean body with the id.
            error_logger.exception(
                "Unhandled error during %s %s", scope["method"], scope["path"]
            )
            if response_started:
                raise  # too late to send a clean response; let the server close it
            response = JSONResponse(
                {"detail": "Internal Server Error", "request_id": request_id},
                status_code=500,
                headers={REQUEST_ID_HEADER: request_id},
            )
            await response(scope, receive, send)
            status_code = 500
        finally:
            # Path only: no query string, headers (the ingest token) or bodies.
            access_logger.info(
                "%s %s %s %dms",
                scope["method"],
                scope["path"],
                status_code,
                (time.perf_counter() - started) * 1000,
            )
            request_id_var.reset(token)
