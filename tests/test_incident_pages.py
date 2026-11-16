"""V2-M5: the incident pages (list, detail, dashboard card) and their form actions."""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.analysis.analyzer import AnalyzerResult
from app.analysis.schema import Fact, Hypothesis, RecommendedAction
from app.models import IncidentAnalysis, IncidentCauseConfirmation, IncidentTransition
from app.schemas import EventCreate
from app.services.events import ingest_event
from tests.analysis_helpers import FakeAnalyzer, ev, good_output, ok_result
from tests.approval_helpers import fail, incident_id, now, status_of, workflow_id  # noqa: F401
from tests.ui_helpers import (  # noqa: F401
    act, csrf_of, make_ui, ui, ui_analyzer, unlock, unlocked,
)

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
SCRIPT = "<script>alert(1)</script>"


def count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def force_status(db, incident_id, status):
    db.execute(text("UPDATE incidents SET status = :s WHERE id = :i"), {"s": status, "i": incident_id})
    db.commit()


def grounded(cause="The credential's connection expired.", confidence="medium"):
    return Hypothesis(cause=cause, confidence=confidence, supporting_evidence=[ev("untrusted.node_name", "HTTP")],
                      would_confirm=["Open the credential in n8n."], verification_steps=["Reconnect it."])


def ungrounded(cause="The tenant was suspended."):
    return Hypothesis(cause=cause, confidence="medium",
                      supporting_evidence=[ev("untrusted.error.message", "this text is nowhere in the data")],
                      would_confirm=["Ask the admin."], verification_steps=[])


def output(*hypotheses, **overrides):
    facts = [Fact(statement="The failing node is HTTP.", evidence=[ev("untrusted.node_name", "HTTP")])]
    return good_output(hypotheses=list(hypotheses) or [grounded()], facts=facts, **overrides)


@pytest.fixture
def me(client):
    """(browser, csrf) for an unlocked session as 'alice'."""
    return unlocked("alice")


def analyze(browser, csrf, incident_id, force=False):
    fields = {"force": "1"} if force else {}
    return act(browser, f"/incidents/{incident_id}/analyze", csrf, **fields)


def detail(browser, incident_id) -> str:
    response = browser.get(f"/incidents/{incident_id}")
    assert response.status_code == 200
    return response.text


def section(html: str, element_id: str) -> str:
    match = re.search(rf'<section id="{element_id}"[^>]*>(.*?)</section>', html, re.S)
    assert match, f"no section #{element_id}"
    return match.group(1)


# ====================================================================== list page


def test_the_list_defaults_to_active_incidents(ui, fail, db_session):  # noqa: F811
    ids = {name: fail(name, message=f"{name} failed", node=name)["event"]["incident_id"]
           for name in ("open", "acknowledged", "resolved", "regressed")}
    for name, incident in ids.items():
        force_status(db_session, incident, name)
    html = ui.get("/incidents").text
    assert f'href="/incidents/{ids["open"]}"' in html and f'href="/incidents/{ids["regressed"]}"' in html
    assert f'href="/incidents/{ids["acknowledged"]}"' not in html and f'href="/incidents/{ids["resolved"]}"' not in html
    assert "1–2 of 2" in html


@pytest.mark.parametrize("view, expected", [
    ("all", {"open", "acknowledged", "resolved", "regressed"}),
    ("active", {"open", "regressed"}),
    ("open", {"open"}), ("acknowledged", {"acknowledged"}), ("resolved", {"resolved"}), ("regressed", {"regressed"}),
])
def test_every_view_shows_exactly_its_statuses(ui, fail, db_session, view, expected):  # noqa: F811
    ids = {}
    for name in ("open", "acknowledged", "resolved", "regressed"):
        ids[name] = fail(name, message=f"{name} failed", node=name)["event"]["incident_id"]
        force_status(db_session, ids[name], name)
    html = ui.get(f"/incidents?view={view}").text
    shown = {name for name, incident in ids.items() if f'href="/incidents/{incident}"' in html}
    assert shown == expected


def test_regressed_rows_stand_out(ui, fail, db_session):  # noqa: F811
    regressed = fail("r", message="r failed", node="R")["event"]["incident_id"]
    opened = fail("o", message="o failed", node="O")["event"]["incident_id"]
    force_status(db_session, regressed, "regressed")
    html = ui.get("/incidents").text
    assert html.count('class="row-regressed"') == 1
    assert 'badge regressed' in html and f'href="/incidents/{opened}"' in html


def test_an_unknown_view_shows_a_message_not_json(ui, incident_id):
    response = ui.get("/incidents?view=bogus")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    assert "Unknown view." in response.text and f'href="/incidents/{incident_id}"' not in response.text


def test_the_empty_list_says_what_to_do(ui):
    html = ui.get("/incidents").text
    assert 'id="no-active"' in html and "No open incidents." in html
    assert "No incidents match" in ui.get("/incidents?view=resolved").text


def test_the_list_pages(ui, fail):  # noqa: F811
    for n in range(27):
        fail(f"p{n}", message="same words", node=f"Node{n:02d}")
    page_one = ui.get("/incidents").text
    assert "1–25 of 27" in page_one and "view=active&amp;offset=25" in page_one and "Newer" not in page_one
    page_two = ui.get("/incidents?offset=25").text
    assert "26–27 of 27" in page_two and "Newer" in page_two and "Older" not in page_two


# ====================================================================== detail page


def test_an_unknown_incident_is_an_html_404(ui):
    response = ui.get("/incidents/999")
    assert response.status_code == 404 and "text/html" in response.headers["content-type"]
    assert "Incident not found" in response.text


def test_the_detail_page_shows_the_incident_and_its_occurrences(ui, fail, incident_id):  # noqa: F811
    fail("e2", message="boom 1")  # a second occurrence of the same incident
    html = detail(ui, incident_id)
    assert f"Incident #{incident_id}" in html and "2 occurrence(s)" in html and "boom" in html
    occurrences = section(html, "occurrences")
    assert occurrences.count("<tr>") == 3  # header + 2 events
    assert "The 2 newest of 2" in occurrences


def test_the_context_preview_is_exactly_what_the_api_previews(ui, client, incident_id):
    html = detail(ui, incident_id)
    api = client.get(f"/api/incidents/{incident_id}/context").json()
    assert api["context_hash"] in section(html, "context")
    assert "&#34;trusted&#34;" in section(html, "context") and "&#34;untrusted&#34;" in section(html, "context")


def test_no_analysis_yet_says_so_and_nothing_runs(ui, ui_analyzer, incident_id):
    html = detail(ui, incident_id)
    assert "No analysis yet" in html and "only runs when someone clicks Analyze" in html
    assert ui_analyzer.calls == []


def test_an_unknown_notice_key_shows_nothing_and_echoes_nothing(ui, incident_id):
    html = ui.get(f"/incidents/{incident_id}?notice={SCRIPT}&error=<b>x</b>").text
    assert 'id="notice"' not in html and 'id="error"' not in html and "alert(1)" not in html


def test_known_keys_show_their_fixed_messages(ui, incident_id):
    assert "Incident acknowledged." in ui.get(f"/incidents/{incident_id}?notice=acknowledged").text
    assert "That move is not allowed" in ui.get(f"/incidents/{incident_id}?error=not_allowed").text


def test_error_raw_never_reaches_any_page(ui, db_session, workflow_id):  # noqa: F811
    """Ingested like the n8n sync does: a readable summary, and the raw JSON stored apart."""
    raw = ('{"name":"NodeApiError","message":"Request failed with status code 429","secretKey":"SENTINEL-NEVER-SHOWN",'
           '"node":{"name":"HTTP","credentials":{"token":"SENTINEL-NEVER-SHOWN"}}}')
    event = ingest_event(db_session, EventCreate(
        workflow_id=workflow_id, external_event_id="raw1", status="failed",
        started_at=now() - timedelta(hours=1), node_name="HTTP", error_raw=raw,
    ), summary_source="Request failed with status code 429").event
    incident = event.incident_id
    for path in ("/", "/incidents", f"/incidents/{incident}", "/events"):
        html = ui.get(path).text
        assert "SENTINEL" not in html and "secretKey" not in html and "credentials" not in html, path


# ====================================================================== controls: locked and unlocked


def test_locked_pages_show_no_controls_even_with_an_analysis(ui, analyzed, incident_id):
    """`analyzed` is a different, unlocked browser; `ui` is a visitor who has not unlocked."""
    html = detail(ui, incident_id)
    assert "AI hypothesis" in html  # the analysis itself is readable
    assert f"/unlock?next=/incidents/{incident_id}" in html
    for action in ("transition", "confirm", "analyze"):
        assert f'action="/incidents/{incident_id}/{action}"' not in html, action
    assert 'action="/lock"' not in html and "Confirm this cause" not in html and ">Analyze</button>" not in html


@pytest.mark.parametrize("current, buttons", [
    ("open", {"acknowledged"}), ("acknowledged", {"resolved"}), ("resolved", set()), ("regressed", {"acknowledged"}),
])
def test_only_the_allowed_moves_are_offered(me, incident_id, db_session, current, buttons):
    browser, _ = me
    force_status(db_session, incident_id, current)
    html = section(detail(browser, incident_id), "approval")
    offered = set(re.findall(r'name="to" value="(\w+)"', html))
    assert offered == buttons
    if not buttons:
        assert "reopens as <em>regressed</em>" in html


def test_acknowledge_then_resolve_records_the_session_actor(me, incident_id, db_session):
    browser, csrf = me
    first = act(browser, f"/incidents/{incident_id}/transition", csrf, to="acknowledged", note="on it")
    assert first.status_code == 303 and first.headers["location"] == f"/incidents/{incident_id}?notice=acknowledged"
    assert "Incident acknowledged." in browser.get(first.headers["location"]).text
    second = act(browser, f"/incidents/{incident_id}/transition", csrf, to="resolved")
    assert second.headers["location"].endswith("notice=resolved") and status_of(browser, incident_id) == "resolved"
    rows = db_session.scalars(select(IncidentTransition).order_by(IncidentTransition.id)).all()
    assert [(r.from_status.value, r.to_status.value, r.actor) for r in rows] == [
        ("open", "acknowledged", "alice"), ("acknowledged", "resolved", "alice")]
    assert rows[0].note == "on it"
    history = section(detail(browser, incident_id), "approval")
    assert "Status history (2)" in history and "on it" in history


@pytest.mark.parametrize("to, key", [("resolved", "not_allowed"), ("open", "bad_input"), ("", "bad_input"), ("regressed", "bad_input")])
def test_an_invalid_move_is_refused_with_a_message_and_writes_nothing(me, incident_id, db_session, to, key):
    browser, csrf = me
    response = act(browser, f"/incidents/{incident_id}/transition", csrf, to=to)
    assert response.status_code == 303 and response.headers["location"] == f"/incidents/{incident_id}?error={key}"
    assert count(db_session, IncidentTransition) == 0 and status_of(browser, incident_id) == "open"


def test_a_regression_shows_up_on_the_page(me, incident_id, fail):  # noqa: F811
    browser, csrf = me
    act(browser, f"/incidents/{incident_id}/transition", csrf, to="acknowledged")
    act(browser, f"/incidents/{incident_id}/transition", csrf, to="resolved")
    fail("again", started=now() + timedelta(minutes=1))
    html = detail(browser, incident_id)
    assert 'badge regressed' in html and 'name="to" value="acknowledged"' in html
    assert "resolved → regressed · flowops" in html


def test_actions_on_an_unknown_incident_go_back_to_the_list(me):
    browser, csrf = me
    assert act(browser, "/incidents/999/transition", csrf, to="acknowledged").headers["location"] == "/incidents"
    assert act(browser, "/incidents/999/confirm", csrf, analysis_id="1", hypothesis_index="0", note="x").headers["location"] == "/incidents"


# ====================================================================== analyze


def test_analyze_runs_the_fake_stores_and_then_reuses(me, ui_analyzer, incident_id, db_session):
    browser, csrf = me
    ui_analyzer._results = [ok_result(output())]
    first = analyze(browser, csrf, incident_id)
    assert first.status_code == 303 and first.headers["location"].endswith("notice=analyzed")
    second = analyze(browser, csrf, incident_id)
    assert second.headers["location"].endswith("notice=reused") and len(ui_analyzer.calls) == 1
    assert "no model call, no cost" in browser.get(second.headers["location"]).text
    third = analyze(browser, csrf, incident_id, force=True)
    assert third.headers["location"].endswith("notice=analyzed") and len(ui_analyzer.calls) == 2
    db_session.expire_all()
    assert [r.forced for r in db_session.scalars(select(IncidentAnalysis).order_by(IncidentAnalysis.id))] == [False, True]


def test_rendering_a_page_never_calls_the_analyzer(me, ui, ui_analyzer, incident_id):
    browser, csrf = me
    for client_ in (ui, browser):
        for path in ("/", "/incidents", f"/incidents/{incident_id}", "/unlock", "/events"):
            client_.get(path)
    assert ui_analyzer.calls == []


def test_without_a_key_analyze_says_so_and_stores_nothing(me, incident_id, db_session):
    browser, csrf = me  # no analyzer override: the settings have no OPENAI_API_KEY
    response = analyze(browser, csrf, incident_id)
    assert response.headers["location"] == f"/incidents/{incident_id}?error=no_key"
    assert "OPENAI_API_KEY is not configured" in browser.get(response.headers["location"]).text
    assert count(db_session, IncidentAnalysis) == 0


@pytest.mark.parametrize("result, key", [
    (AnalyzerResult("error", error_type="APIConnectionError"), "analysis_failed"),
    (AnalyzerResult("refused", refusal="No."), "analysis_unusable"),
    (AnalyzerResult("incomplete", error_type="max_output_tokens"), "analysis_unusable"),
    (AnalyzerResult("invalid", error_type="no_parsed_output"), "analysis_unusable"),
])
def test_a_failed_attempt_is_stored_shown_and_reported(me, ui_analyzer, incident_id, db_session, result, key):
    browser, csrf = me
    ui_analyzer._results = [result]
    response = analyze(browser, csrf, incident_id)
    assert response.headers["location"].endswith(f"error={key}")
    html = detail(browser, incident_id)
    assert 'id="latest-attempt-failed"' in html and result.status in section(html, "analysis")
    assert count(db_session, IncidentAnalysis) == 1 and "AI hypothesis" not in section(html, "analysis")


def test_the_buttons_are_labelled_with_their_cost(me, ui_analyzer, incident_id):
    browser, csrf = me
    html = detail(browser, incident_id)
    assert ">Analyze</button>" in html and "this costs money" in html and "Run again" not in html
    analyze(browser, csrf, incident_id)
    again = detail(browser, incident_id)
    assert "Run again (new model call, costs money)" in again and 'name="force" value="1"' in again


# ====================================================================== facts / hypotheses / confirmed cause


@pytest.fixture
def analyzed(me, ui_analyzer, incident_id):
    browser, csrf = me
    ui_analyzer._results = [ok_result(output(grounded(), ungrounded(), insufficient_evidence=False))]
    analyze(browser, csrf, incident_id)
    return browser, csrf


def test_facts_and_hypotheses_carry_distinct_badges(analyzed, incident_id):
    browser, _ = analyzed
    html = section(detail(browser, incident_id), "analysis")
    assert html.count("Fact · checked") == 1
    assert html.count("AI hypothesis") == 2 and html.count("confidence: medium") == 2
    assert html.count("unsupported: no evidence matched the data") == 1  # only the ungrounded one
    assert "Suggestions for a person" in html and "FlowOps runs nothing" in html
    assert "Confirmed cause" not in html and "kind-confirmed" not in html
    boxes = re.findall(r'<div class="box hyp">(.*?)</form>\s*</div>', html, re.S)
    assert len(boxes) == 2 and "unsupported" not in boxes[0] and "unsupported" in boxes[1]
    assert "<code>untrusted.node_name</code>" in html  # the evidence reference is shown


def test_the_insufficient_evidence_banner(me, ui_analyzer, incident_id):
    browser, csrf = me
    ui_analyzer._results = [ok_result(output(insufficient_evidence=True))]
    analyze(browser, csrf, incident_id)
    assert 'id="insufficient-evidence"' in detail(browser, incident_id)


def test_validation_notes_are_available(me, ui_analyzer, incident_id):
    browser, csrf = me
    ui_analyzer._results = [ok_result(output(ungrounded()))]
    analyze(browser, csrf, incident_id)
    assert "What the server removed or lowered" in detail(browser, incident_id)


def test_a_confirmation_needs_a_note_and_a_real_hypothesis(analyzed, incident_id, db_session):
    browser, csrf = analyzed
    url = f"/incidents/{incident_id}/confirm"
    cases = [
        (dict(analysis_id="1", hypothesis_index="0", note=""), "note_required"),
        (dict(analysis_id="1", hypothesis_index="0", note="   "), "note_required"),
        (dict(analysis_id="1", hypothesis_index="9", note="x"), "bad_confirmation"),
        (dict(analysis_id="77", hypothesis_index="0", note="x"), "bad_confirmation"),
        (dict(analysis_id="x", hypothesis_index="0", note="x"), "bad_input"),
        (dict(analysis_id="1", hypothesis_index="", note="x"), "bad_input"),
    ]
    for form, key in cases:
        assert act(browser, url, csrf, **form).headers["location"].endswith(f"error={key}"), form
    assert count(db_session, IncidentCauseConfirmation) == 0


def test_confirming_shows_the_confirmed_box_with_who_and_why(analyzed, incident_id, db_session):
    browser, csrf = analyzed
    response = act(browser, f"/incidents/{incident_id}/confirm", csrf, analysis_id="1", hypothesis_index="0",
                   note="Reconnected the credential and it worked")
    assert response.headers["location"].endswith("notice=confirmed")
    html = detail(browser, incident_id)
    box = section(html, "confirmed-cause")
    assert "Verified by alice" in box and "The credential&#39;s connection expired." in box
    assert "Reconnected the credential and it worked" in box and "Confirmed cause" in box
    assert "A person confirmed this one (see above)." in section(html, "analysis")
    assert status_of(browser, incident_id) == "open"  # confirming changes no status


def test_a_newer_confirmation_replaces_the_current_one_and_keeps_the_old(analyzed, incident_id):
    browser, csrf = analyzed
    url = f"/incidents/{incident_id}/confirm"
    act(browser, url, csrf, analysis_id="1", hypothesis_index="0", note="first")
    act(browser, url, csrf, analysis_id="1", hypothesis_index="1", note="second, after checking again")
    box = section(detail(browser, incident_id), "confirmed-cause")
    assert "The tenant was suspended." in box.split("Earlier confirmations")[0]
    assert "Earlier confirmations (1)" in box and "first" in box.split("Earlier confirmations")[1]


# ====================================================================== the word "verified"


def only_outside_the_confirmed_box(html: str) -> str:
    match = re.search(r'<section id="confirmed-cause".*?</section>', html, re.S)
    return html.replace(match.group(0), "") if match else html


def test_verified_appears_nowhere_before_a_person_confirms(analyzed, ui, incident_id, fail):  # noqa: F811
    browser, _ = analyzed
    for client_ in (ui, browser):
        for path in ("/", "/incidents", f"/incidents/{incident_id}", "/unlock", "/events", "/workflows",
                     "/incidents?view=all", "/incidents/999"):
            assert "verified" not in client_.get(path).text.lower(), path


def test_verified_appears_only_inside_the_confirmed_cause_box(analyzed, ui, incident_id):
    browser, csrf = analyzed
    act(browser, f"/incidents/{incident_id}/confirm", csrf, analysis_id="1", hypothesis_index="0", note="checked it")
    html = detail(browser, incident_id)
    box = re.search(r'<section id="confirmed-cause".*?</section>', html, re.S).group(0)
    assert "verified" in box.lower()
    assert "verified" not in only_outside_the_confirmed_box(html).lower()
    for path in ("/", "/incidents", "/incidents?view=all"):  # other pages never say it, even afterwards
        assert "verified" not in browser.get(path).text.lower(), path


def test_the_confirmed_box_is_distinct_from_the_hypothesis_boxes(analyzed, incident_id):
    browser, csrf = analyzed
    act(browser, f"/incidents/{incident_id}/confirm", csrf, analysis_id="1", hypothesis_index="0", note="checked it")
    html = detail(browser, incident_id)
    assert html.count('id="confirmed-cause"') == 1 and "box confirmed" in html
    analysis = section(html, "analysis")
    assert "kind-confirmed" not in analysis and "Confirmed cause" not in analysis and "Verified" not in analysis


# ====================================================================== escaping


def test_model_and_user_text_is_escaped_everywhere(me, ui_analyzer, fail, incident_id, db_session):  # noqa: F811
    browser, csrf = me
    nasty = Hypothesis(
        cause=f"cause {SCRIPT}", confidence="medium", supporting_evidence=[ev("untrusted.node_name", "HTTP")],
        would_confirm=[f"check {SCRIPT}"], verification_steps=[f"step {SCRIPT}"])
    out = good_output(
        summary=f"summary {SCRIPT}",
        facts=[Fact(statement=f"fact {SCRIPT}", evidence=[ev("untrusted.node_name", "HTTP")])],
        hypotheses=[nasty], recommended_actions=[RecommendedAction(step=f"act {SCRIPT}", risk="low")])
    ui_analyzer._results = [ok_result(out)]
    analyze(browser, csrf, incident_id)
    act(browser, f"/incidents/{incident_id}/transition", csrf, to="acknowledged", note=f"note {SCRIPT}")
    act(browser, f"/incidents/{incident_id}/confirm", csrf, analysis_id="1", hypothesis_index="0", note=f"why {SCRIPT}")
    fail("hostile", message=f"{SCRIPT} boom 1")  # third-party error text in the same incident
    html = detail(browser, incident_id)
    for needle in ("summary", "fact", "cause", "check", "step", "act", "note", "why"):
        assert f"{needle} &lt;script&gt;alert(1)&lt;/script&gt;" in html, needle
    assert "&lt;script&gt;" in html and "<script" not in html.lower()
    for path in ("/", "/incidents", "/incidents?view=all", "/events"):
        assert "<script" not in browser.get(path).text.lower(), path


def test_the_templates_have_no_safe_filter_and_no_script_tag():
    for template in TEMPLATES.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", "") and "{%autoescape" not in source.replace(" ", ""), template.name
        assert "<script" not in source.lower() and "Markup" not in source, template.name
        assert not re.search(r"""(src|href)=["']https?://""", source), template.name


def test_no_python_marks_text_as_safe():
    for path in (TEMPLATES.parent).rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "Markup(" not in source and "mark_safe" not in source, path.name


def test_pages_have_no_script_and_no_external_resources(me, ui, incident_id):
    browser, _ = me
    for client_ in (ui, browser):
        for path in ("/", "/incidents", f"/incidents/{incident_id}", "/unlock"):
            html = client_.get(path).text
            assert "<script" not in html.lower() and not re.search(r"""(src|href)=["']https?://""", html), path


# ====================================================================== dashboard card


def test_the_dashboard_card_counts_only_incidents_that_need_attention(ui, fail, db_session):  # noqa: F811
    ids = [fail(f"d{n}", message=f"d{n} failed", node=f"D{n}")["event"]["incident_id"] for n in range(5)]
    for incident, status in zip(ids, ["open", "open", "regressed", "acknowledged", "resolved"]):
        force_status(db_session, incident, status)
    html = ui.get("/").text
    card = re.search(r'id="open-incidents-card".*?</a>', html, re.S).group(0)
    assert '<div class="n failed">3</div>' in card and "(1 regressed)" in card and 'href="/incidents"' in card
    listed = section(html, "open-incidents")
    for incident in ids[:3]:
        assert f'href="/incidents/{incident}"' in listed
    for incident in ids[3:]:
        assert f'href="/incidents/{incident}"' not in listed


def test_the_dashboard_card_when_there_is_nothing_to_do(ui, fail, db_session):  # noqa: F811
    incident = fail("only")["event"]["incident_id"]
    force_status(db_session, incident, "resolved")
    html = ui.get("/").text
    assert '<div class="n ">0</div>' in html and "No open incidents." in html


def test_the_dashboard_shows_at_most_five_incidents(ui, fail):  # noqa: F811
    for n in range(7):
        fail(f"m{n}", message="same words", node=f"M{n}")
    html = ui.get("/").text
    assert section(html, "open-incidents").count('href="/incidents/') == 5
    assert '<div class="n failed">7</div>' in html


def test_the_empty_dashboard_still_shows_its_empty_state(ui):
    html = ui.get("/").text
    assert 'id="empty-state"' in html and "open incidents (0 regressed)" in html
