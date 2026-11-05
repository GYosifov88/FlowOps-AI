import logging

import httpx2
import pytest

from app.n8n_client import (
    N8nAuthError,
    N8nClient,
    N8nConfigError,
    N8nError,
    N8nNotFound,
    N8nUnavailable,
    normalize_base_url,
)
from tests.n8n_helpers import load

KEY = "KEY-SECRET-DO-NOT-LOG-123"
BASE = "https://n8n.example.com"


def make_client(handler, base=BASE):
    seen: list[httpx2.Request] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    return N8nClient(base, KEY, transport=httpx2.MockTransport(recording)), seen


def json_ok(body):
    return lambda request: httpx2.Response(200, json=body)


# ---------- base URL and key validation ----------


@pytest.mark.parametrize("raw, expected", [
    ("https://n8n.example.com", "https://n8n.example.com"),
    ("https://n8n.example.com/", "https://n8n.example.com"),
    ("https://n8n.example.com/api/v1", "https://n8n.example.com"),
    ("https://n8n.example.com/api/v1/", "https://n8n.example.com"),
    ("https://example.com/n8n/api/v1", "https://example.com/n8n"),
    ("  https://n8n.example.com  ", "https://n8n.example.com"),
    ("http://localhost:5678", "http://localhost:5678"),
    ("http://127.0.0.1:5678/", "http://127.0.0.1:5678"),
])
def test_base_url_is_normalised(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "n8n.example.com", "ftp://n8n.example.com",
    "http://n8n.example.com",  # plain http to a remote host
    "http://192.168.1.5:5678",
    "https://user:pass@n8n.example.com", "https://n8n.example.com?x=1", "https://",
])
def test_unusable_base_urls_are_refused(raw):
    with pytest.raises(N8nConfigError):
        normalize_base_url(raw)


def test_a_missing_key_is_a_config_error():
    for key in ("", "   "):
        with pytest.raises(N8nConfigError, match="N8N_API_KEY"):
            N8nClient(BASE, key)


def test_the_key_never_shows_in_repr_or_config_errors():
    client, _ = make_client(json_ok({"data": [], "nextCursor": None}))

    assert KEY not in repr(client)
    with pytest.raises(N8nConfigError) as caught:
        N8nClient("http://remote.example.com", KEY)
    assert KEY not in str(caught.value)


# ---------- requests: GET only, key header, no data unless asked ----------


def test_every_request_is_a_get_with_the_key_header_and_no_redirect_following():
    client, seen = make_client(json_ok({"data": [], "nextCursor": None}))

    client.list_executions(load("F5_workflow.json")["id"])
    client.list_active_workflows()

    assert [r.method for r in seen] == ["GET", "GET"]
    assert all(r.headers["X-N8N-API-KEY"] == KEY for r in seen)
    assert all(str(r.url).startswith("https://n8n.example.com/api/v1/") for r in seen)
    assert client._http.follow_redirects is False


def test_the_client_has_no_way_to_write():
    public = {n for n in dir(N8nClient) if not n.startswith("_")}

    assert public == {"close", "get_execution", "get_workflow", "list_active_workflows", "list_executions"}


def test_list_executions_sends_the_workflow_limit_and_cursor_but_never_data_flags():
    client, seen = make_client(json_ok({"data": [], "nextCursor": None}))

    client.list_executions("wf_fixture_001", limit=100, cursor="abc")
    client.list_executions("wf_fixture_001")

    first, second = (dict(r.url.params) for r in seen)
    assert first == {"workflowId": "wf_fixture_001", "limit": "100", "cursor": "abc"}
    assert "cursor" not in second
    for request in seen:
        query = str(request.url.query, "ascii").lower()
        assert "includedata" not in query and "ignoredatasizelimit" not in query
        assert "redactexecutiondata" not in query


def test_get_execution_asks_for_data_only_when_told_to():
    client, seen = make_client(json_ok({"id": "1"}))

    client.get_execution("20628")
    client.get_execution("20628", include_data=True)

    assert dict(seen[0].url.params) == {}
    assert dict(seen[1].url.params) == {"includeData": "true"}
    assert all("ignoreDataSizeLimit" not in str(r.url) for r in seen)
    assert all("redactExecutionData" not in str(r.url) for r in seen)
    assert seen[0].url.path == "/api/v1/executions/20628"


@pytest.mark.parametrize("bad", ["../secret", "1/2", "a b", "", "x" * 65, "1?includeData=true"])
def test_ids_that_could_alter_the_path_are_refused(bad):
    client, seen = make_client(json_ok({}))

    with pytest.raises(N8nError):
        client.get_execution(bad)
    with pytest.raises(N8nError):
        client.get_workflow(bad)
    with pytest.raises(N8nError):
        client.list_executions(bad)
    assert seen == []


# ---------- parsing the real fixtures ----------


def test_the_real_list_fixtures_parse_into_pages():
    client, _ = make_client(json_ok(load("F1_list_page1.json")))

    page = client.list_executions("wf_fixture_001", limit=5)

    assert [e["id"] for e in page.items] == ["20637", "20636", "20635", "20634", "20633"]
    assert page.next_cursor == load("F1_list_page1.json")["nextCursor"]
    last = make_client(json_ok(load("F2_list_page2.json")))[0].list_executions("wf_fixture_001")
    assert last.next_cursor is None


def test_active_workflows_follow_the_cursor_until_it_runs_out():
    pages = {
        None: {"data": [{"id": "a"}, {"id": "b"}], "nextCursor": "c1"},
        "c1": {"data": [{"id": "c"}], "nextCursor": None},
    }

    def handler(request):
        return httpx2.Response(200, json=pages[request.url.params.get("cursor")])

    client, seen = make_client(handler)

    assert [w["id"] for w in client.list_active_workflows()] == ["a", "b", "c"]
    assert all(r.url.params["active"] == "true" for r in seen)


def test_a_repeating_cursor_does_not_loop_forever():
    client, seen = make_client(json_ok({"data": [{"id": "a"}], "nextCursor": "same"}))

    with pytest.raises(N8nError, match="repeating cursor"):
        client.list_active_workflows()
    assert len(seen) < 100


# ---------- errors: classified, and never leaking the key or bodies ----------


@pytest.mark.parametrize("status, exc", [
    (401, N8nAuthError), (403, N8nAuthError), (404, N8nNotFound),
    (429, N8nUnavailable), (500, N8nUnavailable), (503, N8nUnavailable),
    (301, N8nUnavailable), (302, N8nUnavailable),  # redirects are not followed
])
def test_http_errors_are_classified(status, exc):
    client, _ = make_client(lambda r: httpx2.Response(status, text="BODY-WITH-PERSONAL-DATA jane@corp.com"))

    with pytest.raises(exc) as caught:
        client.get_execution("1")

    assert "BODY-WITH-PERSONAL-DATA" not in str(caught.value)
    assert KEY not in str(caught.value)
    assert "/executions/1" in str(caught.value)


@pytest.mark.parametrize("error", [httpx2.ConnectError("boom KEY"), httpx2.ReadTimeout("slow")])
def test_network_failures_become_unavailable_without_details(error):
    def handler(request):
        raise error

    client, _ = make_client(handler)

    with pytest.raises(N8nUnavailable) as caught:
        client.get_workflow("wf1")

    assert KEY not in str(caught.value) and "boom" not in str(caught.value)


@pytest.mark.parametrize("response", [
    httpx2.Response(200, text="<html>not json</html>"),
    httpx2.Response(200, json=[1, 2, 3]),
    httpx2.Response(200, json={"data": "nope", "nextCursor": None}),
    httpx2.Response(200, json={"data": [1], "nextCursor": None}),
    httpx2.Response(200, json={"data": [], "nextCursor": 5}),
])
def test_unexpected_response_shapes_are_errors(response):
    client, _ = make_client(lambda r: response)

    with pytest.raises(N8nError):
        client.list_executions("wf1")


def test_nothing_the_client_logs_contains_the_key(caplog):
    client, _ = make_client(lambda r: httpx2.Response(401))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(N8nAuthError):
            client.get_workflow("wf1")

    assert KEY not in caplog.text
