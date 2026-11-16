"""Shared setup for the V2-M5 page tests: a browser-like client, unlock helpers, a fake analyzer."""
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.routers.incidents import get_optional_analyzer
from tests.analysis_helpers import FakeAnalyzer
from tests.conftest import TEST_TOKEN

SETTINGS = Settings(app_name="t", log_level="INFO", database_url="unused", ingest_token=TEST_TOKEN)
LOCAL = {"Origin": "http://localhost"}


def make_ui(base_url="http://localhost", headers=None) -> TestClient:
    """A client that behaves like a browser on localhost: own cookie jar, no auto-redirects."""
    return TestClient(app, base_url=base_url, follow_redirects=False, headers=headers if headers is not None else LOCAL)


@pytest.fixture
def ui(client):
    """`client` first, so the per-test database and settings overrides are in place."""
    return make_ui()


@pytest.fixture
def ui_analyzer(client):
    """The HTML pages' analyzer is this fake; nothing here can reach the network."""
    analyzer = FakeAnalyzer()
    app.dependency_overrides[get_optional_analyzer] = lambda: analyzer
    return analyzer


def unlock(browser: TestClient, actor="alice", token=TEST_TOKEN, next_url=None):
    form = {"token": token, "actor": actor}
    if next_url is not None:
        form["next"] = next_url
    return browser.post("/unlock", data=form)


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF field on the page (is the browser unlocked?)"
    return match.group(1)


def unlocked(actor="alice") -> tuple[TestClient, str]:
    """(browser, csrf) for a freshly unlocked session."""
    browser = make_ui()
    response = unlock(browser, actor)
    assert response.status_code == 303, response.text
    return browser, csrf_of(browser.get("/").text)


def act(browser: TestClient, path: str, csrf: str | None, **fields):
    """POST a form action the way a browser would (urlencoded, CSRF field included)."""
    data = dict(fields)
    if csrf is not None:
        data["csrf"] = csrf
    return browser.post(path, data=data)
