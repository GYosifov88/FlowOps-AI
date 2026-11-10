"""Loads tests/corpus/incidents.json and ingests it into a TEST database.

The corpus is never loaded into the real database (so the dashboard counters stay real).
n8n-shaped cases go through the real sync mapping (map_status / extract_error / to_event),
so they exercise the same code as a live pull; API-shaped cases go through EventCreate.
"""
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import ExecutionStatus, Workflow
from app.schemas import EventCreate
from app.services.events import ingest_event
from app.services.n8n_sync import extract_error, map_status, to_event

CORPUS_PATH = Path(__file__).parent / "corpus" / "incidents.json"


def load_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def register_workflows(db: Session, corpus: dict) -> dict[str, int]:
    ids = {}
    for label, spec in corpus["workflows"].items():
        workflow = Workflow(name=spec["name"], source=spec["source"], external_id=spec["external_id"])
        db.add(workflow)
        db.commit()
        ids[label] = workflow.id
    return ids


def ingest_case(db: Session, case: dict, workflow_ids: dict[str, int]):
    workflow_id = workflow_ids[case["workflow"]]
    if case["source"] == "n8n":
        execution = case["execution"]
        status = map_status(execution.get("status"), execution.get("stoppedAt"))
        error = extract_error(execution) if status == ExecutionStatus.FAILED else None
        data, summary = to_event(workflow_id, execution, status, error)
        return ingest_event(db, data, summary_source=summary).event
    data = EventCreate(workflow_id=workflow_id, status=ExecutionStatus.FAILED, **case["event"])
    return ingest_event(db, data).event


def ingest_corpus(db: Session, corpus: dict | None = None, *, reverse: bool = False):
    """Returns (workflow_ids, {case id: event id})."""
    corpus = corpus or load_corpus()
    workflow_ids = register_workflows(db, corpus)
    cases = list(reversed(corpus["cases"])) if reverse else corpus["cases"]
    event_ids = {case["id"]: ingest_case(db, case, workflow_ids).id for case in cases}
    return workflow_ids, event_ids
