"""Shared test helpers for the n8n sync: fixture loading and a fake n8n client.

The fixtures in tests/fixtures/n8n/ were captured from a real instance and scrubbed
(fake ids, domains and names). Everything dated or shaped differently from them is
built by hand here, from the n8n API documentation.
"""
import copy
import json
from pathlib import Path

from app.n8n_client import ExecutionPage, N8nNotFound

FIXTURES = Path(__file__).parent / "fixtures" / "n8n"
WF = "wf_fixture_001"  # the scrubbed n8n workflow id used in every fixture


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_executions() -> list[dict]:
    """F1 + F2 together: nine executions, newest first (20637 ... 20628); 20628 failed."""
    return load("F1_list_page1.json")["data"] + load("F2_list_page2.json")["data"]


def make_execution(exec_id, status="success", started="2026-10-30T10:00:00.000Z",
                   stopped="2026-10-30T10:00:05.000Z", mode="webhook"):
    return {
        "id": str(exec_id), "finished": status == "success", "mode": mode, "retryOf": None,
        "retrySuccessId": None, "status": status, "startedAt": started, "stoppedAt": stopped,
        "workflowId": WF, "waitTill": None,
    }


def error_detail(execution: dict, **error) -> dict:
    """A detail response (includeData=true) for `execution` carrying the given error."""
    detail = copy.deepcopy(execution)
    detail["data"] = {"resultData": {"error": error}}
    return detail


_DEFAULT = object()


class FakeN8n:
    """Stands in for N8nClient. Records every call so tests can assert what was fetched."""

    def __init__(self, executions=None, details=None, workflow=_DEFAULT, active=None, page_size=5,
                 missing=()):
        self.executions = list(executions if executions is not None else fixture_executions())
        # By default the one real failure (20628) has its real, scrubbed detail (F3).
        self.details = dict(details) if details is not None else {"20628": load("F3_error_with_data.json")}
        self.workflow = load("F5_workflow.json") if workflow is _DEFAULT else workflow  # None = n8n 404
        self.active = active if active is not None else load("F5_active_list.json")["data"]
        self.page_size = page_size
        self.missing = {str(m) for m in missing}  # execution ids n8n answers 404 for
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_workflow(self, workflow_id):
        self.calls.append(("workflow", workflow_id))
        if self.workflow is None:
            raise N8nNotFound("workflow")
        return copy.deepcopy(self.workflow)

    def list_executions(self, workflow_id, *, limit=100, cursor=None):
        self.calls.append(("list", workflow_id, cursor))
        start = int(cursor) if cursor else 0
        items = self.executions[start:start + self.page_size]
        more = start + self.page_size < len(self.executions)
        return ExecutionPage(items=copy.deepcopy(items),
                             next_cursor=str(start + self.page_size) if more else None)

    def get_execution(self, execution_id, *, include_data=False):
        self.calls.append(("get", str(execution_id), include_data))
        if str(execution_id) in self.missing:
            raise N8nNotFound(execution_id)
        detail = self.details.get(str(execution_id))
        if detail is None:
            for item in self.executions:
                if item["id"] == str(execution_id):
                    detail = item
        if detail is None:
            raise N8nNotFound(execution_id)
        detail = copy.deepcopy(detail)
        if not include_data:
            detail.pop("data", None)
        return detail

    def list_active_workflows(self):
        self.calls.append(("active",))
        return copy.deepcopy(self.active)

    # ---- helpers for assertions
    def fetched(self, include_data=None) -> list[str]:
        return [c[1] for c in self.calls
                if c[0] == "get" and (include_data is None or c[2] == include_data)]

    def list_calls(self) -> int:
        return sum(1 for c in self.calls if c[0] == "list")
