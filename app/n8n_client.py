"""Read-only client for the n8n public REST API (GET requests only).

Deliberately exposes no way to write: there is no post/put/patch/delete method, and nothing
here can change anything in n8n. Error messages carry the HTTP status and the path, never
the API key and never response bodies (which may contain personal data).

Data minimisation lives in the signatures: `list_executions` has no way to ask for
execution data at all, and `get_execution` only sends `includeData` when asked. The
parameters `ignoreDataSizeLimit` and `redactExecutionData` are never sent.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx2

API_PREFIX = "/api/v1"
LIST_TIMEOUT = 10.0
DETAIL_TIMEOUT = 30.0
EXECUTIONS_PAGE_SIZE = 100
WORKFLOWS_PAGE_SIZE = 250
MAX_WORKFLOW_PAGES = 40  # a runaway-cursor guard, not a real limit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class N8nError(Exception):
    """Base class. Never carries the API key or a response body."""


class N8nConfigError(N8nError):
    """Missing or unusable N8N_BASE_URL / N8N_API_KEY."""


class N8nAuthError(N8nError):
    """401 or 403: the key is wrong, expired or lacks permission."""


class N8nNotFound(N8nError):
    """404 for a single resource (a workflow or execution that n8n does not have)."""


class N8nUnavailable(N8nError):
    """Network failure, timeout, redirect, 429 or 5xx."""


@dataclass(frozen=True)
class ExecutionPage:
    items: list[dict]
    next_cursor: str | None


def normalize_base_url(raw: str) -> str:
    """Validate N8N_BASE_URL; returns it without a trailing slash or /api/v1."""
    text = (raw or "").strip()
    if not text:
        raise N8nConfigError("N8N_BASE_URL is not set")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise N8nConfigError("N8N_BASE_URL must look like https://your-n8n-host")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        raise N8nConfigError("N8N_BASE_URL must use https:// (http is allowed only for localhost)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise N8nConfigError("N8N_BASE_URL must not contain credentials, a query or a fragment")
    path = parts.path.rstrip("/")
    if path.endswith(API_PREFIX):
        path = path[: -len(API_PREFIX)]
    return f"{parts.scheme}://{parts.netloc}{path}"


def _safe_id(value: str, what: str) -> str:
    text = str(value)
    if not _SAFE_ID.fullmatch(text):
        raise N8nError(f"refusing to use an unexpected {what} id")
    return quote(text, safe="")


class N8nClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        if not (api_key or "").strip():
            raise N8nConfigError("N8N_API_KEY is not set")
        self._http = httpx2.Client(
            base_url=normalize_base_url(base_url) + API_PREFIX,
            headers={"X-N8N-API-KEY": api_key.strip(), "accept": "application/json"},
            transport=transport,
            follow_redirects=False,  # never forward the key to another host
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # ---- the only HTTP verb in this class is GET ----

    def _get(self, path: str, params: dict | None = None, timeout: float = LIST_TIMEOUT) -> dict:
        try:
            response = self._http.get(path, params=params, timeout=timeout)
        except httpx2.HTTPError as exc:  # timeouts, connection errors, protocol errors
            raise N8nUnavailable(f"GET {path} failed: {type(exc).__name__}") from None
        status = response.status_code
        if status in (401, 403):
            raise N8nAuthError(f"n8n rejected the API key (HTTP {status}) for GET {path}")
        if status == 404:
            raise N8nNotFound(f"GET {path}: not found")
        if status != 200:
            raise N8nUnavailable(f"GET {path} returned HTTP {status}")
        try:
            body = response.json()
        except ValueError:
            raise N8nError(f"GET {path} did not return JSON") from None
        if not isinstance(body, dict):
            raise N8nError(f"GET {path} returned an unexpected JSON shape")
        return body

    @staticmethod
    def _page(body: dict, path: str) -> ExecutionPage:
        items = body.get("data")
        cursor = body.get("nextCursor")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise N8nError(f"GET {path} returned an unexpected list shape")
        if cursor is not None and not isinstance(cursor, str):
            raise N8nError(f"GET {path} returned an unexpected cursor")
        return ExecutionPage(items=items, next_cursor=cursor or None)

    def list_executions(
        self, workflow_id: str, *, limit: int = EXECUTIONS_PAGE_SIZE, cursor: str | None = None
    ) -> ExecutionPage:
        """One page of a workflow's executions, newest first. Never includes data."""
        params = {"workflowId": _safe_id(workflow_id, "workflow"), "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._page(self._get("/executions", params), "/executions")

    def get_execution(self, execution_id: str, *, include_data: bool = False) -> dict:
        """One execution. `include_data=True` is for failed executions only (see n8n_sync)."""
        path = f"/executions/{_safe_id(execution_id, 'execution')}"
        params = {"includeData": "true"} if include_data else None
        return self._get(path, params, timeout=DETAIL_TIMEOUT)

    def get_workflow(self, workflow_id: str) -> dict:
        return self._get(f"/workflows/{_safe_id(workflow_id, 'workflow')}")

    def list_active_workflows(self) -> list[dict]:
        """Every active workflow (all pages). Callers read only id, name and settings."""
        found: list[dict] = []
        cursor = None
        for _ in range(MAX_WORKFLOW_PAGES):
            params = {"active": "true", "limit": WORKFLOWS_PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            page = self._page(self._get("/workflows", params), "/workflows")
            found.extend(page.items)
            if not page.next_cursor:
                return found
            if page.next_cursor == cursor:
                raise N8nError("GET /workflows returned a repeating cursor; stopping")
            cursor = page.next_cursor
        raise N8nError("GET /workflows did not finish paging")
