import re
from datetime import datetime, timezone

import pytest

from app.models import ExecutionEvent, ExecutionStatus, Workflow
from app.schemas import EventRead
from app.services import dashboard as dashboard_service
from app.web import duration, localtime
from scripts import seed

PAGES = ["/", "/workflows", "/workflows?include_inactive=true", "/events", "/events?status=failed"]


@pytest.fixture
def make_workflow(db_session):
    def _make(name="Daily report", active=True, **extra) -> int:
        w = Workflow(name=name, source="n8n", is_active=active, **extra)
        db_session.add(w)
        db_session.commit()
        return w.id

    return _make


@pytest.fixture
def add_event(db_session):
    def _add(workflow_id, started_at="2026-10-27T09:00:00", status=ExecutionStatus.SUCCESS, **extra):
        e = ExecutionEvent(
            workflow_id=workflow_id,
            status=status,
            started_at=datetime.fromisoformat(started_at) if started_at else None,
            **extra,
        )
        db_session.add(e)
        db_session.commit()
        return e.id

    return _add


def status_cards(html: str) -> dict[str, int]:
    """{'success': 4, ...} read from the 'Executions by status' cards."""
    section = html.split('id="status-counts"')[1].split("</section>")[0]
    return {name: int(n) for name, n in re.findall(r'<div class="n (\w+)">(\d+)</div>', section)}


# ---------- dashboard ----------


def test_dashboard_empty_state_on_a_fresh_install(client):
    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "<h1>Dashboard</h1>" in html
    assert 'id="empty-state"' in html and "Nothing here yet" in html
    assert "scripts.seed" in html  # tells the user what to do next
    assert 'id="summary"' in html and 'id="status-counts"' in html
    assert status_cards(html) == {s.value: 0 for s in ExecutionStatus}
    assert 'id="recent-events"' not in html and 'id="recent-failures"' not in html


def test_dashboard_with_data_has_every_section_and_matching_counts(client, auth_headers):
    seed.run_seed(client, auth_headers["X-FlowOps-Token"])

    html = client.get("/").text

    # What scripts/seed.py produces: 8 distinct events.
    assert status_cards(html) == {
        "success": 4, "failed": 2, "running": 0, "waiting": 1, "canceled": 1, "unknown": 0,
    }
    assert "3</div><div class=\"l\">registered workflows (3 active)" in html
    assert "8</div><div class=\"l\">recorded executions" in html
    assert 'id="empty-state"' not in html
    recent = html.split('id="recent-events"')[1].split("</section>")[0]
    failures = html.split('id="recent-failures"')[1].split("</section>")[0]
    assert "Send invoices (synthetic)" in recent
    assert failures.count('class="badge failed"') == 2
    assert "api_key=[REDACTED]" in failures  # the sanitized summary is what is shown


def test_dashboard_with_workflows_but_no_events_shows_event_empty_states(client, make_workflow):
    make_workflow()

    html = client.get("/").text

    assert 'id="empty-state"' not in html  # there is data, just not events
    assert "No executions recorded yet" in html
    assert "No failed executions" in html


def test_dashboard_counts_ignore_the_recent_window(client, make_workflow, add_event):
    wf = make_workflow()
    for _ in range(14):
        add_event(wf, status=ExecutionStatus.FAILED)

    html = client.get("/").text

    assert status_cards(html)["failed"] == 14  # all of them, not just the 10 shown
    recent = html.split('id="recent-events"')[1].split("</section>")[0]
    assert recent.count('class="badge') == 10
    assert html.split('id="recent-failures"')[1].count('class="badge failed"') == 5


# ---------- safety: nothing sensitive reaches the HTML ----------


def test_error_raw_is_never_rendered_on_any_page(client, make_workflow, add_event):
    wf = make_workflow()
    add_event(
        wf,
        status=ExecutionStatus.FAILED,
        error_raw="RAWSECRET-9f3 Authorization: Bearer LEAKEDTOKEN-77",
        error_summary="Auth failed: Authorization: [REDACTED]",
    )

    for url in PAGES:
        html = client.get(url).text
        assert "RAWSECRET" not in html and "LEAKEDTOKEN" not in html, url
        assert "error_raw" not in html, url
    assert "Auth failed" in client.get("/").text  # while the summary IS shown


def test_event_view_model_has_no_error_raw_field():
    assert "error_raw" not in EventRead.model_fields


def test_user_supplied_text_is_html_escaped(client, make_workflow, add_event):
    wf = make_workflow(name="<script>alert('wf')</script>", description="<b>bold?</b>")
    add_event(
        wf, status=ExecutionStatus.FAILED,
        node_name="<img src=x onerror=alert(1)>",
        error_summary="</td><script>alert('err')</script>",
    )

    for url in ("/", "/workflows", "/events"):
        html = client.get(url).text
        assert "<script>alert" not in html, url
        assert "<img src=x" not in html and "<b>bold?" not in html, url
    assert "&lt;script&gt;alert(&#39;wf&#39;)" in client.get("/workflows").text


# ---------- workflows page ----------


def test_workflows_page_empty_state(client):
    html = client.get("/workflows").text

    assert client.get("/workflows").status_code == 200
    assert 'id="empty-state"' in html and "POST /api/workflows" in html
    assert "workflows-table" not in html


def test_workflows_page_lists_active_and_toggles_inactive(client, make_workflow):
    make_workflow("Active one")
    make_workflow("Retired one", active=False)

    default = client.get("/workflows").text
    everything = client.get("/workflows?include_inactive=true").text

    assert "Active one" in default and "Retired one" not in default
    assert "Active one" in everything and "Retired one" in everything
    assert 'class="badge canceled">inactive' in everything
    assert "/events?workflow_id=1" in default


# ---------- events page ----------


def test_events_page_empty_state(client):
    response = client.get("/events")

    assert response.status_code == 200
    assert "No executions recorded yet" in response.text
    assert "events-results" not in response.text


def test_events_page_lists_events_newest_first(client, make_workflow, add_event):
    wf = make_workflow("Mailer")
    add_event(wf, "2026-10-26T09:00:00", node_name="OLDNODE")
    add_event(wf, "2026-10-28T09:00:00", node_name="NEWNODE")

    html = client.get("/events").text

    assert html.index("NEWNODE") < html.index("OLDNODE")
    assert "1–2 of 2" in html and "Mailer" in html


def test_events_page_filters_reuse_the_api_semantics(client, make_workflow, add_event):
    a, b = make_workflow("Alpha"), make_workflow("Beta")
    add_event(a, "2026-10-27T23:59:59", ExecutionStatus.FAILED, node_name="HIT")
    add_event(a, "2026-10-28T00:00:00", ExecutionStatus.FAILED, node_name="NEXTDAY")
    add_event(a, "2026-10-27T10:00:00", ExecutionStatus.SUCCESS, node_name="WRONGSTATUS")
    add_event(b, "2026-10-27T10:00:00", ExecutionStatus.FAILED, node_name="WRONGWF")
    add_event(a, None, ExecutionStatus.FAILED, node_name="NODATE")

    html = client.get(
        "/events", params={"workflow_id": a, "status": "failed",
                           "started_from": "2026-10-27", "started_to": "2026-10-27"},
    ).text

    assert "HIT" in html  # the bare 'to' date includes all of 27 October
    for excluded in ("NEXTDAY", "WRONGSTATUS", "WRONGWF", "NODATE"):
        assert excluded not in html
    assert "1–1 of 1" in html


def test_events_page_blank_form_fields_mean_no_filter(client, make_workflow, add_event):
    add_event(make_workflow())

    html = client.get(
        "/events", params={"workflow_id": "", "status": "", "started_from": "", "started_to": ""}
    ).text

    assert "1–1 of 1" in html and 'class="error"' not in html


def test_events_page_no_match_empty_state_offers_to_clear_filters(client, make_workflow, add_event):
    add_event(make_workflow())

    html = client.get("/events", params={"status": "failed"}).text

    assert "No executions match these filters" in html and 'href="/events">Clear' in html


@pytest.mark.parametrize(
    "params, message",
    [
        ({"started_from": "yesterday"}, "Started from is not a valid date"),
        ({"started_to": "2026-13-45"}, "Started to is not a valid date"),
        ({"status": "exploded"}, "Unknown status"),
        ({"workflow_id": "abc"}, "Workflow must be a number"),
    ],
)
def test_events_page_bad_filters_show_a_message_not_a_json_error(
    client, make_workflow, add_event, params, message
):
    add_event(make_workflow())

    response = client.get("/events", params=params)

    assert response.status_code == 200
    assert message in response.text
    assert "events-results" not in response.text  # and no misleading unfiltered rows


def test_events_page_pagination_keeps_filters(client, make_workflow, add_event):
    wf = make_workflow()
    for i in range(30):
        add_event(wf, f"2026-10-27T09:{i:02d}:00", ExecutionStatus.FAILED)
    add_event(wf, "2026-10-27T12:00:00", ExecutionStatus.SUCCESS)

    first = client.get("/events", params={"status": "failed"}).text
    second = client.get("/events", params={"status": "failed", "offset": 25}).text

    assert "1–25 of 30" in first and "Older →" in first and "Newer" not in first
    assert "status=failed" in first and "offset=25" in first
    assert "26–30 of 30" in second and "← Newer" in second and "Older" not in second
    assert "offset=0" in second


def test_events_page_negative_offset_is_rejected(client):
    assert client.get("/events", params={"offset": -1}).status_code == 422


def test_events_page_selects_the_current_filter_in_the_form(client, make_workflow):
    wf = make_workflow("Alpha")

    html = client.get("/events", params={"workflow_id": wf, "status": "waiting"}).text

    assert f'<option value="{wf}" selected>Alpha' in html
    assert '<option value="waiting" selected>' in html


# ---------- service layer and helpers ----------


def test_dashboard_service_returns_zero_counts_for_every_status(db_session):
    data = dashboard_service.get_dashboard(db_session)

    assert data.status_counts == {s: 0 for s in ExecutionStatus}
    assert (data.workflows_total, data.events_total, data.recent_events) == (0, 0, [])


def test_pages_are_not_part_of_the_json_api_docs(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert "/" not in paths and "/workflows" not in paths
    assert "/api/workflows" in paths and "/api/events" in paths


def test_localtime_converts_utc_for_display_only():
    naive = datetime(2026, 10, 27, 9, 0, 0)
    aware = naive.replace(tzinfo=timezone.utc)

    assert localtime(naive) == localtime(aware)
    assert localtime(aware) == aware.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    assert localtime(None) == "—"


@pytest.mark.parametrize("ms, text", [(None, "—"), (0, "0 ms"), (999, "999 ms"), (1500, "1.5 s")])
def test_duration_formatting(ms, text):
    assert duration(ms) == text
