"""Pull sync: copy executions of registered n8n workflows into FlowOps.

Read-only towards n8n, and it writes through the existing `ingest_event` upsert, so the
M4 rules apply unchanged (a finished event is never overwritten; a non-finished one may be
completed once). See the M7 decision record, plus these corrections made while building M8:

* Executions are ingested OLDEST FIRST, one commit each, so an interrupted run always
  leaves a clean prefix and the next run simply continues.
* The walk through n8n's newest-first pages does not stop at the first known finished
  execution. It stops at one that started more than OVERLAP before the newest event
  FlowOps held when the run began, so an execution that finished late is still seen.
* FlowOps's own non-finished rows are re-read individually (re-check).

Data minimisation:
* The list call never asks for execution data.
* includeData=true is requested for executions that map to `failed`, and only for those.
* Of the error, only an allow-list is kept (see extract_error). Run data, workflow data
  and node parameters/credentials are never read into FlowOps or logged.
* node_name comes ONLY from the error's own node. If n8n gives none it stays NULL.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NON_TERMINAL, TERMINAL, ExecutionEvent, ExecutionStatus, Workflow
from app.n8n_client import N8nClient, N8nError, N8nNotFound
from app.schemas import EventCreate
from app.services.events import EventOwnerMismatch, ingest_event
from app.timeutil import to_naive_utc, utcnow

logger = logging.getLogger(__name__)

SOURCE = "n8n"
ID_PREFIX = "n8n:"
OVERLAP = timedelta(hours=24)
GIVE_UP_AFTER = {ExecutionStatus.WAITING: timedelta(days=30)}  # a Wait node can sleep for days
GIVE_UP_DEFAULT = timedelta(hours=24)
MAX_EXECUTION_PAGES = 500  # guards against a runaway cursor, not a real limit

NAME_CAP = 255
MESSAGE_CAP = 2000
STACK_CAP = 4000

_STATUS_MAP = {
    "success": ExecutionStatus.SUCCESS,
    "error": ExecutionStatus.FAILED,
    "crashed": ExecutionStatus.FAILED,
    "canceled": ExecutionStatus.CANCELED,
    "waiting": ExecutionStatus.WAITING,
    "running": ExecutionStatus.RUNNING,
    "new": ExecutionStatus.RUNNING,
    "unknown": ExecutionStatus.UNKNOWN,
}


# ---------------------------------------------------------------- pure mapping


def map_status(raw: str | None, stopped_at: str | None = None) -> ExecutionStatus:
    """n8n status -> FlowOps status (M7 table).

    The public API returns null for new/running/unknown executions, so null means running
    while there is no stoppedAt, and unknown once there is one. A value we do not know
    becomes `unknown` (with a warning) instead of breaking the run.
    """
    if raw is None:
        return ExecutionStatus.RUNNING if not stopped_at else ExecutionStatus.UNKNOWN
    mapped = _STATUS_MAP.get(str(raw).lower())
    if mapped is None:
        logger.warning("Unrecognised n8n execution status %r; recording as unknown", str(raw)[:30])
        return ExecutionStatus.UNKNOWN
    return mapped


def parse_ts(value) -> datetime | None:
    """n8n ISO timestamp -> aware datetime; None if missing or unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class ErrorDetail:
    available: bool
    summary: str  # readable text; sanitized later by ingest_event
    raw: str | None  # compact JSON of the allow-listed fields, or None
    node_name: str | None  # ONLY from the error's own node


def _text(value, cap: int) -> str | None:
    return value[:cap] if isinstance(value, str) and value.strip() else None


def _http_code(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 20:
        return value
    return None


def _unavailable(reason: str | None) -> ErrorDetail:
    note = "n8n: error details unavailable"
    return ErrorDetail(
        available=False,
        summary=f"{note} ({reason})" if reason else note,
        raw=None,
        node_name=None,
    )


def extract_error(detail: dict) -> ErrorDetail:
    """Pull the error out of an execution fetched with includeData=true.

    ALLOW-LIST: name, message, description, httpCode, stack (capped) and the failing node's
    NAME. The rest of `error.node` (parameters, credentials, ...) and everything else in the
    execution (runData, pinData, workflowData, customData) is never read into a variable,
    stored or logged. The execution's own "last node executed" field is deliberately not
    used: it is the last node that ran, not necessarily the one that failed.
    """
    data = detail.get("data")
    if detail.get("dataTooLargeToDisplay") or (
        isinstance(data, dict) and data.get("dataTooLargeToDisplay")
    ):
        return _unavailable("data too large")
    if isinstance(data, dict):
        info = data.get("redactionInfo")
        if isinstance(info, dict) and info.get("isRedacted"):
            return _unavailable("redacted")

    result_data = data.get("resultData") if isinstance(data, dict) else None
    error = result_data.get("error") if isinstance(result_data, dict) else None
    if not isinstance(error, dict):
        return _unavailable(None)

    node = error.get("node")
    node_name = _text(node.get("name"), NAME_CAP) if isinstance(node, dict) else None

    kept = {
        "name": _text(error.get("name"), NAME_CAP),
        "message": _text(error.get("message"), MESSAGE_CAP),
        "description": _text(error.get("description"), MESSAGE_CAP),
        "httpCode": _http_code(error.get("httpCode")),
        "stack": _text(error.get("stack"), STACK_CAP),
    }
    kept = {k: v for k, v in kept.items() if v is not None}
    if not any(k in kept for k in ("name", "message", "description")):
        return _unavailable(None)
    if node_name:
        kept["node"] = {"name": node_name}

    headline = kept.get("message") or kept.get("description") or kept.get("name")
    summary = f"{kept['name']}: {headline}" if "name" in kept and headline != kept["name"] else headline
    return ErrorDetail(
        available=True,
        summary=summary,
        raw=json.dumps(kept, ensure_ascii=False, sort_keys=True),
        node_name=node_name,
    )


def event_id(execution: dict) -> str | None:
    raw = execution.get("id")
    return f"{ID_PREFIX}{raw}" if raw not in (None, "") else None


def to_event(
    workflow_id: int, execution: dict, status: ExecutionStatus, error: ErrorDetail | None
) -> tuple[EventCreate, str | None]:
    """(EventCreate, summary text) for one n8n execution. The text goes to sanitize_error."""
    mode = execution.get("mode")
    summary = error.summary if error else None
    if str(execution.get("status")).lower() == "crashed":
        summary = (
            f"n8n: crashed - {error.summary}" if error and error.available
            else "n8n: crashed (error details unavailable)"
        )
    event = EventCreate(
        workflow_id=workflow_id,
        external_event_id=event_id(execution),
        status=status,
        started_at=parse_ts(execution.get("startedAt")),
        # A waiting execution has a stoppedAt (its current run stopped); it has not finished.
        finished_at=parse_ts(execution.get("stoppedAt")) if status in TERMINAL else None,
        node_name=error.node_name if error else None,
        mode=str(mode)[:50] if mode else None,
        error_raw=error.raw if error else None,
    )
    return event, summary


# ---------------------------------------------------------------- the sync run


@dataclass
class WorkflowSyncResult:
    workflow_id: int
    external_id: str
    name: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        logger.warning("n8n sync [workflow %s / n8n %s] %s", self.workflow_id, self.external_id, message)

    @property
    def changed(self) -> int:
        return self.created + self.updated


@dataclass
class SyncReport:
    workflows: list[WorkflowSyncResult] = field(default_factory=list)
    unregistered: list[dict] = field(default_factory=list)  # active in n8n, unknown to FlowOps

    @property
    def totals(self) -> dict[str, int]:
        return {
            key: sum(getattr(w, key) for w in self.workflows)
            for key in ("created", "updated", "unchanged", "skipped")
        }


def registered_workflows(db: Session) -> list[Workflow]:
    return list(
        db.scalars(
            select(Workflow)
            .where(
                Workflow.source == SOURCE,
                Workflow.is_active.is_(True),
                Workflow.external_id.is_not(None),
            )
            .order_by(Workflow.id)
        )
    )


def sync_all(db: Session, client: N8nClient, *, now: datetime | None = None) -> SyncReport:
    """Sync every registered, active n8n workflow, then report unregistered ones.

    Auth, network and unexpected-response errors abort the run (they propagate). Problems
    with a single workflow or execution become warnings.
    """
    now = now or utcnow()
    report = SyncReport()
    for workflow in registered_workflows(db):
        report.workflows.append(sync_workflow(db, client, workflow, now=now))

    known_ids = set(
        db.scalars(
            select(Workflow.external_id).where(
                Workflow.source == SOURCE, Workflow.external_id.is_not(None)
            )
        )
    )
    for item in client.list_active_workflows():
        if str(item.get("id")) not in known_ids:
            report.unregistered.append(
                {"id": str(item.get("id")), "name": str(item.get("name") or "")}
            )
    return report


def sync_workflow(
    db: Session, client: N8nClient, workflow: Workflow, *, now: datetime | None = None
) -> WorkflowSyncResult:
    now = now or utcnow()
    external_id = workflow.external_id
    result = WorkflowSyncResult(workflow.id, external_id, workflow.name)

    try:
        _check_workflow_settings(client.get_workflow(external_id), result)
    except N8nNotFound:
        result.warn("n8n has no workflow with this id; skipped")
        return result

    known = _known_events(db, workflow.id)
    known_terminal = {eid for eid, status, _ in known if status in TERMINAL}
    anchor = max((started for _, _, started in known if started), default=None)
    cutoff = anchor - OVERLAP if anchor else None

    pending = _walk_n8n(client, external_id, known_terminal, cutoff, anchor, result)

    touched: set[str] = set()
    for execution in reversed(pending):  # oldest first
        touched.add(event_id(execution))
        _ingest_execution(db, client, workflow, execution, result)

    _recheck_open_events(db, client, workflow, touched, now, result)

    logger.info(
        "n8n sync [workflow %s / n8n %s] created=%d updated=%d unchanged=%d skipped=%d warnings=%d",
        workflow.id, external_id, result.created, result.updated, result.unchanged,
        result.skipped, len(result.warnings),
    )
    return result


def _check_workflow_settings(detail: dict, result: WorkflowSyncResult) -> None:
    """Only the two save-data settings are read; the rest of the workflow is ignored."""
    settings = detail.get("settings")
    if not isinstance(settings, dict):
        return
    if settings.get("saveDataErrorExecution") == "none":
        result.warn("n8n does not save this workflow's FAILED executions; FlowOps cannot see its failures")
    if settings.get("saveDataSuccessExecution") == "none":
        result.warn("n8n does not save this workflow's SUCCESSFUL executions; FlowOps cannot see its successes")


def _known_events(db: Session, workflow_id: int) -> list[tuple[str, ExecutionStatus, datetime | None]]:
    rows = db.execute(
        select(ExecutionEvent.external_event_id, ExecutionEvent.status, ExecutionEvent.started_at).where(
            ExecutionEvent.workflow_id == workflow_id,
            ExecutionEvent.external_event_id.like(f"{ID_PREFIX}%"),
        )
    )
    return [(eid, status, started) for eid, status, started in rows]


def _walk_n8n(
    client: N8nClient,
    external_id: str,
    known_terminal: set[str],
    cutoff: datetime | None,
    anchor: datetime | None,
    result: WorkflowSyncResult,
) -> list[dict]:
    """Page through n8n's executions (newest first); return those still to be ingested.

    Known finished executions are skipped, but the walk continues past them until one that
    started before `cutoff` (see module docstring). If n8n runs out of pages without ever
    meeting a known finished execution although FlowOps holds some, n8n pruned history in
    between: warn about the gap (data is never invented).
    """
    pending: list[dict] = []
    cursor = None
    seen_cursors: set[str] = set()
    met_known = False
    stopped = False
    oldest_started: datetime | None = None

    for _ in range(MAX_EXECUTION_PAGES):
        page = client.list_executions(external_id, cursor=cursor)
        for execution in page.items:
            started = parse_ts(execution.get("startedAt"))
            if started is not None:
                started_naive = to_naive_utc(started)
                if oldest_started is None or started_naive < oldest_started:
                    oldest_started = started_naive
            eid = event_id(execution)
            if eid is None:
                result.skipped += 1
                result.warn("skipped an n8n execution without an id")
                continue
            if eid in known_terminal:
                met_known = True
                if cutoff is not None and started is not None and to_naive_utc(started) < cutoff:
                    stopped = True
                    break
                continue
            pending.append(execution)
        if stopped or not page.next_cursor:
            break
        if page.next_cursor in seen_cursors:
            raise N8nError("n8n returned a repeating cursor; stopping")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    else:
        raise N8nError("n8n execution list did not finish paging")

    if not stopped and known_terminal and not met_known:
        result.warn(
            "possible gap: n8n no longer has executions between "
            f"{anchor:%Y-%m-%d %H:%M} UTC (newest FlowOps knew) and "
            f"{oldest_started:%Y-%m-%d %H:%M} UTC (oldest n8n still has); "
            "they were probably pruned before this sync ran"
            if anchor and oldest_started
            else "possible gap: none of FlowOps's known executions appear in n8n's history anymore"
        )
    return pending


def _fetch_error(client: N8nClient, execution_id: str) -> ErrorDetail:
    """includeData=true, for a failed execution only. 404 (pruned) is not fatal."""
    try:
        return extract_error(client.get_execution(execution_id, include_data=True))
    except N8nNotFound:
        return _unavailable(None)


def _ingest_execution(
    db: Session, client: N8nClient, workflow: Workflow, execution: dict, result: WorkflowSyncResult
) -> None:
    status = map_status(execution.get("status"), execution.get("stoppedAt"))
    error = _fetch_error(client, str(execution["id"])) if status == ExecutionStatus.FAILED else None
    try:
        data, summary = to_event(workflow.id, execution, status, error)
        outcome = ingest_event(db, data, summary_source=summary).outcome
    except EventOwnerMismatch:
        result.skipped += 1
        result.warn(f"execution {execution.get('id')} already belongs to another FlowOps workflow; skipped")
        return
    except ValidationError:
        result.skipped += 1
        result.warn(f"execution {execution.get('id')} has an unexpected shape; skipped")
        return
    setattr(result, outcome, getattr(result, outcome) + 1)


def _recheck_open_events(
    db: Session,
    client: N8nClient,
    workflow: Workflow,
    touched: set[str],
    now: datetime,
    result: WorkflowSyncResult,
) -> None:
    """Re-read FlowOps's own running/waiting/unknown rows that this run did not just cover."""
    rows = db.scalars(
        select(ExecutionEvent).where(
            ExecutionEvent.workflow_id == workflow.id,
            ExecutionEvent.external_event_id.like(f"{ID_PREFIX}%"),
            ExecutionEvent.status.in_(NON_TERMINAL),
        )
    ).all()
    stale = 0
    for row in rows:
        if row.external_event_id in touched:
            continue
        age = now - (row.started_at or row.created_at)
        if age > GIVE_UP_AFTER.get(row.status, GIVE_UP_DEFAULT):
            stale += 1  # no longer re-checked; the row stays as it is
            continue
        execution_id = row.external_event_id[len(ID_PREFIX):]
        try:
            execution = client.get_execution(execution_id)
        except N8nNotFound:
            result.warn(f"execution {execution_id} is gone from n8n; left as {row.status.value}")
            continue
        _ingest_execution(db, client, workflow, execution, result)
    if stale:
        result.warn(f"{stale} unfinished execution(s) are past the give-up age and are no longer re-checked")
