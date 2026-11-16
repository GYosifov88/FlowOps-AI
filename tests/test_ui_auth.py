"""V2-M5: the pages' own authentication (unlock, signed session cookie, CSRF, Origin/Host)."""
import base64
import hashlib
import json
import logging
import time

import pytest
from sqlalchemy import func, select

import app.ui_auth as ui_auth
from app.models import IncidentTransition
from tests.approval_helpers import fail, incident_id, status_of, workflow_id  # noqa: F401
from tests.conftest import TEST_TOKEN
from tests.ui_helpers import (  # noqa: F401
    LOCAL, SETTINGS, act, csrf_of, make_ui, ui, ui_analyzer, unlock, unlocked,
)

HOUR = 3600


def written(db_session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(IncidentTransition))


def ack(browser, incident_id, csrf, **headers):
    return browser.post(
        f"/incidents/{incident_id}/transition", data={"csrf": csrf, "to": "acknowledged"}, headers=headers or None
    )


# ====================================================================== unlock


def test_the_unlock_page_is_a_plain_form(ui):
    html = ui.get("/unlock").text
    assert 'type="password"' in html and 'name="actor"' in html and 'action="/unlock"' in html
    assert "<script" not in html.lower()


def test_a_wrong_token_gets_401_and_no_cookie(ui, caplog):
    caplog.set_level(logging.DEBUG)
    response = unlock(ui, token="not-the-token-99")
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert "That token is not correct" in response.text
    assert "not-the-token-99" not in caplog.text and TEST_TOKEN not in caplog.text


@pytest.mark.parametrize("token", ["", " ", TEST_TOKEN + "x", TEST_TOKEN[:-1], TEST_TOKEN.upper(), "tökén"])
def test_near_misses_of_the_token_are_refused(ui, token):
    response = unlock(ui, token=token)
    assert response.status_code == 401 and "set-cookie" not in response.headers


@pytest.mark.parametrize("actor", ["", "   ", "<script>", "a;b", "x" * 101, "ünï"])
def test_a_bad_name_is_refused_even_with_the_right_token(ui, actor):
    response = unlock(ui, actor=actor)
    assert response.status_code == 400 and "set-cookie" not in response.headers


def test_a_good_unlock_sets_a_locked_down_signed_cookie(ui):
    response = unlock(ui, actor="  carol  ")
    assert response.status_code == 303 and response.headers["location"] == "/"
    header = response.headers["set-cookie"]
    value = ui.cookies.get("flowops_ui")
    assert header.startswith("flowops_ui=")
    assert "HttpOnly" in header and "samesite=strict" in header.lower() and "Path=/" in header
    assert f"Max-Age={12 * HOUR}" in header
    # neither the token, nor anything derived from it by a plain hash, is in the cookie
    body = base64.urlsafe_b64decode(value.split(".")[0] + "==").decode()
    assert json.loads(body)["a"] == "carol"
    for secret in (TEST_TOKEN, hashlib.sha256(TEST_TOKEN.encode()).hexdigest(),
                   base64.b64encode(TEST_TOKEN.encode()).decode(), base64.urlsafe_b64encode(TEST_TOKEN.encode()).decode()):
        assert secret not in value and secret not in body
    assert TEST_TOKEN not in ui.get("/").text


def test_every_unlock_is_a_new_session(ui):
    unlock(ui)
    first = ui.cookies.get("flowops_ui")
    unlock(ui)
    assert ui.cookies.get("flowops_ui") != first


@pytest.mark.parametrize("target", ["//evil.example", "http://evil.example/x", "https://evil.example", "/\\evil.example",
                                    "javascript:alert(1)", "evil", "", "/a\r\nSet-Cookie: x=1", "///evil.example"])
def test_next_never_redirects_off_site(ui, target):
    response = unlock(ui, next_url=target)
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_next_keeps_a_local_path(ui):
    assert unlock(ui, next_url="/incidents/5").headers["location"] == "/incidents/5"
    assert ui_auth.safe_next("/incidents?view=all") == "/incidents?view=all"


def test_the_unlock_page_does_not_echo_an_unsafe_next(ui):
    assert "evil.example" not in ui.get("/unlock?next=//evil.example").text


def test_a_cross_origin_unlock_is_refused(ui):
    response = ui.post("/unlock", data={"token": TEST_TOKEN, "actor": "alice"}, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403 and "set-cookie" not in response.headers


def test_the_header_shows_who_is_unlocked_and_offers_lock(ui):
    assert "Unlock</a>" in ui.get("/").text
    unlock(ui, "dana")
    html = ui.get("/").text
    assert "Unlocked as dana" in html and 'action="/lock"' in html


def test_lock_clears_the_session(ui, incident_id, db_session):
    unlock(ui)
    csrf = csrf_of(ui.get("/").text)
    response = act(ui, "/lock", csrf)
    assert response.status_code == 303
    assert "flowops_ui=" in response.headers["set-cookie"] and "Max-Age=0" in response.headers["set-cookie"]
    assert ack(ui, incident_id, csrf).status_code == 303 and written(db_session) == 0  # back to "unlock first"


# ====================================================================== no session at all


@pytest.mark.parametrize("path, form", [
    ("/incidents/{id}/transition", {"to": "acknowledged"}),
    ("/incidents/{id}/confirm", {"analysis_id": "1", "hypothesis_index": "0", "note": "x"}),
    ("/incidents/{id}/analyze", {"x": "1"}),  # a browser form always has at least one field
    ("/lock", {"x": "1"}),
])
def test_actions_without_a_session_redirect_to_unlock_and_write_nothing(
    ui, incident_id, db_session, ui_analyzer, path, form
):
    response = ui.post(path.format(id=incident_id), data=form)
    assert response.status_code == 303
    expected = f"/unlock?next=%2Fincidents%2F{incident_id}" if "{id}" in path else "/unlock?next=%2F"
    assert response.headers["location"] == expected
    assert written(db_session) == 0 and status_of(ui, incident_id) == "open" and ui_analyzer.calls == []


def test_actions_cannot_be_reached_with_get(ui, incident_id):
    for path in ("transition", "confirm", "analyze"):
        assert ui.get(f"/incidents/{incident_id}/{path}").status_code == 405
    assert ui.get("/lock").status_code == 405


def test_reading_needs_no_session(ui, incident_id):
    for path in ("/", "/incidents", f"/incidents/{incident_id}", "/events", "/workflows"):
        assert ui.get(path).status_code == 200


# ====================================================================== a session, but it must be valid


def good_session():
    browser, csrf = unlocked()
    return browser, csrf, browser.cookies.get("flowops_ui")


def with_cookie(cookie_value: str):
    return {"Cookie": f"flowops_ui={cookie_value}"}


def csrf_for(cookie_value: str) -> str:
    """The CSRF value that matches this cookie's own nonce under the CURRENT key, so that only
    the cookie itself can be what is wrong (otherwise a CSRF mismatch would hide the bug)."""
    nonce = json.loads(base64.urlsafe_b64decode(cookie_value.split(".")[0] + "=="))["n"]
    return ui_auth.csrf_token(SETTINGS, nonce)


def post_with(cookie_value: str, incident_id: int):
    return make_ui().post(
        f"/incidents/{incident_id}/transition",
        data={"csrf": csrf_for(cookie_value), "to": "acknowledged"},
        headers={**LOCAL, **with_cookie(cookie_value)},
    )


def tampered_variants(value: str) -> dict[str, str]:
    body, signature = value.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    swapped = base64.urlsafe_b64encode(json.dumps({**payload, "a": "mallory"}).encode()).rstrip(b"=").decode()
    flip = lambda s: s[:-1] + ("A" if s[-1] != "A" else "B")  # noqa: E731
    return {
        "signature flipped": f"{body}.{flip(signature)}",
        "payload flipped": f"{flip(body)}.{signature}",
        "actor swapped, old signature": f"{swapped}.{signature}",
        "no signature": body,
        "extra part": f"{value}.x",
        "garbage": "not-a-cookie",
        "empty": "",
        "unsigned": f"{body}.",
    }


def test_a_tampered_cookie_is_403(incident_id, db_session):
    victim, csrf, value = good_session()
    attacker = make_ui()
    for label, bad in tampered_variants(value).items():
        response = attacker.post(
            f"/incidents/{incident_id}/transition", data={"csrf": csrf, "to": "acknowledged"}, headers={**LOCAL, **with_cookie(bad)}
        )
        assert response.status_code == 403, label
        assert "flowops_ui=" in response.headers["set-cookie"] and "Max-Age=0" in response.headers["set-cookie"], label
    assert written(db_session) == 0


def test_an_expired_cookie_is_403(incident_id, db_session):
    old = ui_auth.issue_cookie(SETTINGS, "alice", now=time.time() - 12 * HOUR - 120)
    response = post_with(old, incident_id)
    assert response.status_code == 403 and written(db_session) == 0


def test_a_cookie_from_the_future_is_403(incident_id, db_session):
    future = ui_auth.issue_cookie(SETTINGS, "alice", now=time.time() + 2 * HOUR)
    response = post_with(future, incident_id)
    assert response.status_code == 403 and written(db_session) == 0


def test_a_cookie_made_under_an_old_token_is_403(incident_id, db_session):
    """Rotating FLOWOPS_INGEST_TOKEN logs every browser out."""
    from dataclasses import replace

    stale = ui_auth.issue_cookie(replace(SETTINGS, ingest_token="the-previous-token"), "alice")
    response = post_with(stale, incident_id)
    assert response.status_code == 403 and written(db_session) == 0


def test_a_fresh_cookie_just_inside_the_lifetime_still_works(incident_id, db_session):
    almost = ui_auth.issue_cookie(SETTINGS, "alice", now=time.time() - 12 * HOUR + 120)
    session = ui_auth.read_session(SETTINGS, almost)
    assert session is not None
    response = make_ui().post(
        f"/incidents/{incident_id}/transition", data={"csrf": session.csrf, "to": "acknowledged"},
        headers={**LOCAL, **with_cookie(almost)},
    )
    assert response.status_code == 303 and "notice=acknowledged" in response.headers["location"]


def test_a_stale_cookie_does_not_break_reading(incident_id):
    stale = make_ui(headers={**LOCAL, **with_cookie("garbage")})
    html = stale.get(f"/incidents/{incident_id}").text
    assert "Unlock</a>" in html and "Unlocked as" not in html and "action=\"/incidents/" not in html


# ====================================================================== CSRF


def test_a_missing_or_wrong_csrf_token_is_403(incident_id, db_session):
    browser, csrf, _ = good_session()
    for label, response in {
        "missing": act(browser, f"/incidents/{incident_id}/transition", None, to="acknowledged"),
        "empty": act(browser, f"/incidents/{incident_id}/transition", "", to="acknowledged"),
        "wrong": act(browser, f"/incidents/{incident_id}/transition", "0" * len(csrf), to="acknowledged"),
        "truncated": act(browser, f"/incidents/{incident_id}/transition", csrf[:-1], to="acknowledged"),
        "padded": act(browser, f"/incidents/{incident_id}/transition", csrf + "0", to="acknowledged"),
    }.items():
        assert response.status_code == 403, label
    assert written(db_session) == 0 and status_of(browser, incident_id) == "open"


def test_another_sessions_csrf_token_is_403(incident_id, db_session):
    first, _, _ = good_session()
    _, other_csrf = unlocked("bob")
    response = act(first, f"/incidents/{incident_id}/transition", other_csrf, to="acknowledged")
    assert response.status_code == 403 and written(db_session) == 0


def test_the_csrf_token_is_per_session_and_never_the_session_secret(ui):
    unlock(ui)
    token = csrf_of(ui.get("/").text)
    cookie = ui.cookies.get("flowops_ui")
    assert token not in cookie and TEST_TOKEN not in token and len(token) == 64
    other = make_ui()
    unlock(other)
    assert csrf_of(other.get("/").text) != token


def test_csrf_protects_every_action(incident_id, db_session, ui_analyzer):
    browser, csrf, _ = good_session()
    for path, form in (
        (f"/incidents/{incident_id}/transition", {"to": "acknowledged"}),
        (f"/incidents/{incident_id}/confirm", {"analysis_id": "1", "hypothesis_index": "0", "note": "n"}),
        (f"/incidents/{incident_id}/analyze", {}),
        ("/lock", {}),
    ):
        assert act(browser, path, "bad", **form).status_code == 403, path
    assert written(db_session) == 0 and ui_analyzer.calls == []
    assert browser.get("/").text.count("Unlocked as") == 1  # lock with a bad token did not log out


# ====================================================================== Origin / Host


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "http://localhost:9999"},       # another local port (n8n, a dev server): not this site
    {"Origin": "https://localhost"},
    {"Origin": "null"},
    {"Origin": "http://localhost.evil.example"},
    {"Origin": "http://127.0.0.1"},            # a different name for the same machine is a different origin
])
def test_a_wrong_origin_is_403(incident_id, db_session, headers):
    browser, csrf, _ = good_session()
    response = browser.post(
        f"/incidents/{incident_id}/transition", data={"csrf": csrf, "to": "acknowledged"}, headers=headers
    )
    assert response.status_code == 403 and written(db_session) == 0


def test_a_missing_origin_is_403(incident_id, db_session):
    browser, csrf, _ = good_session()
    bare = make_ui(headers={})
    bare.cookies.set("flowops_ui", browser.cookies.get("flowops_ui"))
    response = bare.post(f"/incidents/{incident_id}/transition", data={"csrf": csrf, "to": "acknowledged"})
    assert response.status_code == 403 and written(db_session) == 0


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "192.168.1.5", "example.com:8000", "[::2]"])
def test_a_non_local_host_is_403_even_with_a_matching_origin(incident_id, db_session, host):
    """DNS rebinding: the attacker's page makes Host and Origin agree, but the name is not local."""
    browser, csrf, _ = good_session()
    response = browser.post(
        f"/incidents/{incident_id}/transition", data={"csrf": csrf, "to": "acknowledged"},
        headers={"Host": host, "Origin": f"http://{host}"},
    )
    assert response.status_code == 403 and written(db_session) == 0


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"])
def test_local_hosts_with_a_matching_origin_pass(incident_id, host):
    browser, csrf, _ = good_session()
    response = browser.post(
        f"/incidents/{incident_id}/transition", data={"csrf": csrf, "to": "acknowledged"},
        headers={"Host": host, "Origin": f"http://{host}"},
    )
    assert response.status_code == 303 and "notice=acknowledged" in response.headers["location"]


def test_origin_is_checked_before_the_session(incident_id):
    """No cookie and a hostile origin: refused outright, not even sent to the unlock page."""
    response = make_ui().post(
        f"/incidents/{incident_id}/transition", data={"to": "acknowledged"}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


# ====================================================================== form limits


def test_only_small_urlencoded_forms_are_read(incident_id, db_session):
    browser, csrf, _ = good_session()
    url = f"/incidents/{incident_id}/transition"
    assert browser.post(url, json={"csrf": csrf, "to": "acknowledged"}).status_code == 415
    assert browser.post(url, content=b"x" * 20_000, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
    many = "&".join(f"f{n}=1" for n in range(60))
    assert browser.post(url, content=many.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert written(db_session) == 0


def test_a_note_in_the_session_actor_is_what_gets_recorded_not_a_form_field(incident_id, db_session):
    """The actor comes from the signed cookie; a forged `actor` form field is ignored."""
    browser, csrf, _ = good_session()
    act(browser, f"/incidents/{incident_id}/transition", csrf, to="acknowledged", actor="mallory")
    db_session.expire_all()
    assert db_session.scalar(select(IncidentTransition.actor)) == "alice"
