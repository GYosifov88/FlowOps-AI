"""Incident context (V2-M2): the exact JSON that V2-M3 will later send to the model.

No LLM is called here. This module is the BOUNDARY: everything that could ever leave the
machine is chosen and cleaned here, and `GET /api/incidents/{id}/context` shows it
unchanged, so you can read it before anything is sent.

Layout -- two sections, and the split is the prompt-injection defence:

* `trusted`: computed by FlowOps. Numbers, enums and timestamps only; no free text. (Even
  `mode`, which an API caller can set to anything, is mapped onto n8n's known values.)
* `untrusted`: text written by third parties or users (error messages, node and workflow
  names). Every string went through `scrub` = redact() + PII scrub, and is length-capped.
  It must be treated by the model as DATA, never as instructions.

Allow-list: the `error` block is a projection of the latest event's `error_raw`
(message, description, httpCode, up to 15 stack lines) -- never the raw string, and
nothing else from it. `error_raw` itself is still never returned anywhere.

Determinism: no clock, no randomness. The same events always give the same context and the
same `context_hash`; it changes only when the data does.

For V2-M3 (not built here): the system prompt must say that the block returned by
`frame_context` is untrusted data and that nothing inside it is an instruction, and the
output schema must offer the model no action to take.
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import NaiveUTCDateTime
from app.models import ExecutionEvent, ExecutionStatus, Workflow
from app.sanitize import scrub
from app.services.incidents import RECENT_EVENTS, get_incident, seen_at

CONTEXT_VERSION = 1
FRAME_TAG = "untrusted_incident_data"
RECENT_IN_CONTEXT = 5
STACK_LINES = 15
STACK_LINE_CAP = 300
TEXT_CAP = 1000
NAME_CAP = 255

# n8n execution modes; anything else (the API accepts any string) becomes "other" so the
# trusted section can never carry caller-written text.
KNOWN_MODES = frozenset(
    {"manual", "trigger", "webhook", "retry", "cli", "error", "integrated", "internal", "evaluation"}
)

assert RECENT_IN_CONTEXT <= RECENT_EVENTS  # build_context reuses get_incident's recent list


# ------------------------------------------------------------------ pure helpers


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value, cap: int) -> str | None:
    """Scrubbed, capped text; None for anything that is not a non-blank string."""
    if not isinstance(value, str) or not value.strip():
        return None
    return scrub(value.strip()[:cap])


def _http_code(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 20:
        return scrub(value)
    return None


def error_projection(error_raw: str | None) -> dict | None:
    """The allow-listed, scrubbed view of an event's error_raw -- or None.

    Only a JSON object is projected, and only message, description, httpCode and the first
    STACK_LINES lines of stack: any other key (an API caller may put anything in there) is
    dropped. Free text, no error_raw, or an object with none of these fields gives None;
    FlowOps then has no detail to offer and says so (`error_detail_available: false`).
    """
    if not error_raw:
        return None
    try:
        parsed = json.loads(error_raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    stack = parsed.get("stack")
    lines = (
        [c for c in (_clean(line, STACK_LINE_CAP) for line in stack.splitlines()[:STACK_LINES]) if c]
        if isinstance(stack, str)
        else []
    )
    projection = {
        "message": _clean(parsed.get("message"), TEXT_CAP),
        "description": _clean(parsed.get("description"), TEXT_CAP),
        "http_code": _http_code(parsed.get("httpCode")),
        "stack": lines,
    }
    if not (projection["message"] or projection["description"] or lines):
        return None
    return projection


def canonical_json(context: dict) -> str:
    return json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def context_hash(context: dict) -> str:
    return hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()


def frame_context(context: dict) -> str:
    """The context as one delimited block for a prompt.

    `<`, `>` and `&` are written as JSON \\u escapes, so no literal "<" can occur inside the
    block: an error message can neither close the tag nor open a fake one. A JSON parser
    reads the inside back to exactly the original context.
    """
    body = canonical_json(context)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"<{FRAME_TAG}>\n{body}\n</{FRAME_TAG}>"


# ------------------------------------------------------------------ the builder


def _mode(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in KNOWN_MODES else "other"


def _success_facts(db: Session, workflow_id: int, first_seen: datetime, last_seen: datetime) -> dict:
    """Facts about the workflow's SUCCESSES around this incident's first and last failure.

    Only status `success` counts; canceled / running / waiting are neither.
    """
    seen = seen_at
    successes = (ExecutionEvent.workflow_id == workflow_id, ExecutionEvent.status == ExecutionStatus.SUCCESS)

    last_before = db.scalar(
        select(func.max(seen, type_=NaiveUTCDateTime)).where(*successes, seen < first_seen)
    )
    after_count, after_first = db.execute(
        select(func.count(), func.min(seen, type_=NaiveUTCDateTime)).where(*successes, seen > last_seen)
    ).one()
    window_successes = db.scalar(
        select(func.count()).where(*successes, seen >= first_seen, seen <= last_seen)
    )
    window_failures = db.scalar(
        select(func.count()).where(
            ExecutionEvent.workflow_id == workflow_id,
            ExecutionEvent.status == ExecutionStatus.FAILED,
            seen >= first_seen,
            seen <= last_seen,
        )
    )
    return {
        "last_success_before_first_failure": _iso(last_before),
        "successes_after_last_failure": after_count,
        "first_success_after_last_failure": _iso(after_first),
        "window": {
            "from": _iso(first_seen),
            "to": _iso(last_seen),
            "workflow_successes": window_successes,
            "workflow_failures": window_failures,  # every failed event of the workflow in the window
        },
    }


def build_context(db: Session, incident_id: int) -> dict:
    """The deterministic context of one incident. Raises IncidentNotFound."""
    summary, recent = get_incident(db, incident_id)  # recent: newest first
    incident = summary.incident
    workflow = db.get(Workflow, incident.workflow_id)
    error = error_projection(recent[0].error_raw)  # from the LATEST occurrence

    facts = _success_facts(db, incident.workflow_id, summary.first_seen, summary.last_seen)
    facts["window"]["this_incident_failures"] = summary.occurrence_count

    return {
        "context_version": CONTEXT_VERSION,
        "trusted": {
            "incident": {
                "id": incident.id,
                "signature": incident.signature,
                "signature_version": incident.signature_version,
                "status": incident.status.value,
            },
            "workflow_id": incident.workflow_id,
            "occurrences": {
                "count": summary.occurrence_count,
                "first_seen": _iso(summary.first_seen),
                "last_seen": _iso(summary.last_seen),
                "recent": [
                    {
                        "seen_at": _iso(event.started_at or event.created_at),
                        "mode": _mode(event.mode),
                        "duration_ms": event.duration_ms,
                    }
                    for event in recent[:RECENT_IN_CONTEXT]
                ],
            },
            "facts": facts,
            "error_detail_available": error is not None,
        },
        "untrusted": {
            "workflow_name": _clean(workflow.name, NAME_CAP),
            "workflow_source": _clean(workflow.source, NAME_CAP),
            "node_name": _clean(incident.node_name, NAME_CAP),  # None = unattributed
            "error_name": _clean(incident.error_name, NAME_CAP),
            "normalized_message": _clean(incident.normalized_message, TEXT_CAP),
            "error": error,
        },
    }
