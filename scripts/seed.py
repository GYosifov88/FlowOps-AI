"""Seed a RUNNING FlowOps server with synthetic workflows and execution events.

    python -m scripts.seed          (from the repo root, with the app running)

Goes through the real API, so it exercises auth and the upsert. Safe to run repeatedly:
workflows are found by (source, external_id) and events carry stable external ids, so a
second run only reports "unchanged". All data is made up.
"""
import os
import sys
from collections import Counter

from dotenv import load_dotenv

WORKFLOWS = {
    "invoices": {
        "name": "Send invoices (synthetic)",
        "source": "n8n",
        "external_id": "seed-wf-invoices",
        "description": "Synthetic workflow created by scripts/seed.py",
    },
    "crm-sync": {
        "name": "CRM sync (synthetic)",
        "source": "n8n",
        "external_id": "seed-wf-crm-sync",
        "description": "Synthetic workflow created by scripts/seed.py",
    },
    "nightly": {
        "name": "Nightly cleanup (synthetic)",
        "source": "n8n",
        "external_id": "seed-wf-nightly",
        "description": "Synthetic workflow created by scripts/seed.py",
    },
}


def _event(workflow, ext_id, status, start, finish=None, **extra):
    event = {
        "workflow": workflow,
        "external_event_id": ext_id,
        "status": status,
        "started_at": f"2026-10-27T{start}Z",
        **extra,
    }
    if finish:
        event["finished_at"] = f"2026-10-27T{finish}Z"
    return event


# Order matters: the running events come before their terminal updates.
EVENTS = [
    _event("invoices", "seed-exec-001", "success", "08:00:00", "08:00:04", mode="trigger"),
    _event("invoices", "seed-exec-002", "success", "09:00:00", "09:00:03", mode="trigger"),
    _event("invoices", "seed-exec-002", "success", "09:00:00", "09:00:03", mode="trigger"),  # exact duplicate
    _event(
        "invoices", "seed-exec-003", "failed", "10:00:00", "10:00:01",
        node_name="HTTP Request", mode="trigger",
        error_raw=(
            "NodeApiError: 401 Unauthorized calling "
            "https://api.example.com/v1/invoices?api_key=seed-not-a-real-key "
            "Authorization: Bearer seed-not-a-real-token"
        ),
    ),
    # The pair that exercises the upsert: running first, then the terminal update.
    _event("crm-sync", "seed-exec-004", "running", "11:00:00", node_name="Fetch contacts"),
    _event("crm-sync", "seed-exec-004", "success", "11:00:00", "11:00:12"),
    _event("crm-sync", "seed-exec-005", "running", "12:00:00", mode="webhook"),
    _event("crm-sync", "seed-exec-005", "running", "12:00:00", mode="webhook"),  # duplicate, no change
    _event("crm-sync", "seed-exec-005", "failed", "12:00:00", "12:00:30", node_name="Upsert contact",
           error_raw='{"message": "Rate limit exceeded", "password": "seed-not-a-real-password"}'),
    _event("nightly", "seed-exec-006", "waiting", "13:00:00", mode="trigger"),
    _event("nightly", "seed-exec-007", "canceled", "14:00:00", "14:00:02", mode="manual"),
    _event("nightly", "seed-exec-008", "success", "15:00:00", "15:01:00", mode="trigger"),
]


class SeedError(Exception):
    pass


def _check(response, expected: tuple[int, ...], what: str):
    if response.status_code not in expected:
        raise SeedError(f"{what} failed: HTTP {response.status_code} {response.text[:200]}")
    return response


def ensure_workflows(client, headers) -> dict[str, int]:
    """Find-or-create the synthetic workflows (inactive ones included); key -> id."""
    existing = _check(
        client.get("/api/workflows", params={"include_inactive": "true"}, headers=headers),
        (200,), "Listing workflows",
    ).json()
    by_identity = {(w["source"], w["external_id"]): w["id"] for w in existing}
    ids = {}
    for key, spec in WORKFLOWS.items():
        found = by_identity.get((spec["source"], spec["external_id"]))
        if found is None:
            created = _check(
                client.post("/api/workflows", json=spec, headers=headers),
                (201,), f"Creating workflow {key}",
            )
            found = created.json()["id"]
        ids[key] = found
    return ids


def run_seed(client, token: str) -> Counter:
    """Post the whole batch. `client` is anything with httpx-style get/post (a TestClient works)."""
    headers = {"X-FlowOps-Token": token}
    workflow_ids = ensure_workflows(client, headers)
    results = Counter()
    for event in EVENTS:
        body = {k: v for k, v in event.items() if k != "workflow"}
        body["workflow_id"] = workflow_ids[event["workflow"]]
        response = _check(
            client.post("/api/events", json=body, headers=headers),
            (200, 201), f"Posting {event['external_event_id']}",
        )
        results[response.json()["result"]] += 1
    return results


def main() -> int:
    load_dotenv()
    token = os.getenv("FLOWOPS_INGEST_TOKEN", "").strip()
    base_url = os.getenv("FLOWOPS_BASE_URL", "http://localhost:8000")
    if not token:
        print("FLOWOPS_INGEST_TOKEN is not set (see .env.example).")
        return 1

    import httpx2

    try:
        with httpx2.Client(base_url=base_url, timeout=10) as client:
            results = run_seed(client, token)
    except httpx2.TransportError as exc:
        print(f"Could not reach {base_url}: is the app running? ({type(exc).__name__})")
        return 1
    except SeedError as exc:
        print(exc)
        return 1

    print(f"Seeded {base_url}: " + ", ".join(f"{n} {name}" for name, n in sorted(results.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
