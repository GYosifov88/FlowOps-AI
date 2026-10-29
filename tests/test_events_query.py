from datetime import datetime

import pytest

from app.models import ExecutionEvent, ExecutionStatus, Workflow

URL = "/api/events"


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text)  # naive, i.e. UTC


@pytest.fixture
def wf(db_session):
    def _make(name="wf", active=True) -> int:
        w = Workflow(name=name, source="n8n", is_active=active)
        db_session.add(w)
        db_session.commit()
        return w.id

    return _make


@pytest.fixture
def add(db_session):
    """Insert an event straight into the DB so tests control every timestamp."""

    def _add(workflow_id, started_at, status=ExecutionStatus.SUCCESS, **extra) -> int:
        event = ExecutionEvent(
            workflow_id=workflow_id,
            status=status,
            started_at=dt(started_at) if started_at else None,
            **extra,
        )
        db_session.add(event)
        db_session.commit()
        return event.id

    return _add


def ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [e["id"] for e in response.json()["items"]]


def test_empty_database_gives_an_empty_page_not_an_error(client):
    response = client.get(URL)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_no_token_is_needed_to_read(client, wf, add):
    add(wf(), "2026-10-27T09:00:00")

    assert client.get(URL).status_code == 200  # V1: reads are unauthenticated by design


def test_items_have_the_event_shape_and_never_error_raw(client, wf, add):
    add(wf(), "2026-10-27T09:00:00", error_raw="SECRET RAW", error_summary="safe")

    response = client.get(URL)

    item = response.json()["items"][0]
    assert item["error_summary"] == "safe"
    assert item["started_at"] == "2026-10-27T09:00:00Z"
    assert "error_raw" not in item
    assert "SECRET RAW" not in response.text


# ---------- individual filters ----------


def test_filter_by_workflow_id(client, wf, add):
    a, b = wf("a"), wf("b")
    e1 = add(a, "2026-10-27T09:00:00")
    add(b, "2026-10-27T10:00:00")

    assert ids(client.get(URL, params={"workflow_id": a})) == [e1]


def test_unknown_workflow_id_is_an_empty_page(client, wf, add):
    add(wf(), "2026-10-27T09:00:00")

    response = client.get(URL, params={"workflow_id": 999})

    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["total"] == 0


def test_filter_by_status(client, wf, add):
    w = wf()
    add(w, "2026-10-27T09:00:00", ExecutionStatus.SUCCESS)
    failed = add(w, "2026-10-27T10:00:00", ExecutionStatus.FAILED)

    assert ids(client.get(URL, params={"status": "failed"})) == [failed]


def test_events_of_a_deactivated_workflow_are_listed(client, wf, add):
    gone = wf(active=False)
    e = add(gone, "2026-10-27T09:00:00")

    assert ids(client.get(URL)) == [e]


@pytest.mark.parametrize("status", ["error", "SUCCESS", "", "nope"])
def test_bad_status_is_422(client, status):
    assert client.get(URL, params={"status": status}).status_code == 422


# ---------- date semantics ----------


def test_started_from_is_inclusive(client, wf, add):
    w = wf()
    before = add(w, "2026-10-27T08:59:59.999999")
    exact = add(w, "2026-10-27T09:00:00")
    after = add(w, "2026-10-27T09:00:00.000001")

    result = ids(client.get(URL, params={"started_from": "2026-10-27T09:00:00"}))

    assert before not in result
    assert result == [after, exact]


def test_started_to_is_exclusive(client, wf, add):
    w = wf()
    just_before = add(w, "2026-10-27T08:59:59.999999")
    exact = add(w, "2026-10-27T09:00:00")

    result = ids(client.get(URL, params={"started_to": "2026-10-27T09:00:00"}))

    assert result == [just_before]
    assert exact not in result


def test_bare_date_to_includes_the_whole_final_day(client, wf, add):
    w = wf()
    day_before = add(w, "2026-10-26T23:59:59")
    midnight = add(w, "2026-10-27T00:00:00")
    last_second = add(w, "2026-10-27T23:59:59.999999")
    next_midnight = add(w, "2026-10-28T00:00:00")

    result = ids(client.get(URL, params={"started_to": "2026-10-27"}))

    assert result == [last_second, midnight, day_before]
    assert next_midnight not in result  # 28 Oct 00:00 is the exclusive bound


def test_bare_date_from_starts_at_midnight_utc(client, wf, add):
    w = wf()
    add(w, "2026-10-26T23:59:59.999999")
    midnight = add(w, "2026-10-27T00:00:00")

    assert ids(client.get(URL, params={"started_from": "2026-10-27"})) == [midnight]


def test_a_single_day_via_from_and_to_with_the_same_bare_date(client, wf, add):
    w = wf()
    add(w, "2026-10-26T12:00:00")
    inside = add(w, "2026-10-27T12:00:00")
    add(w, "2026-10-28T12:00:00")

    result = ids(client.get(URL, params={"started_from": "2026-10-27", "started_to": "2026-10-27"}))

    assert result == [inside]


def test_aware_bounds_are_converted_to_utc(client, wf, add):
    w = wf()
    before = add(w, "2026-10-27T08:59:59")
    exact = add(w, "2026-10-27T09:00:00")

    # 12:00 at +03:00 is 09:00 UTC. params= encodes the "+" correctly.
    result = ids(client.get(URL, params={"started_from": "2026-10-27T12:00:00+03:00"}))

    assert result == [exact]
    assert before not in result


def test_z_suffix_and_naive_bounds_mean_the_same_instant(client, wf, add):
    w = wf()
    e = add(w, "2026-10-27T09:00:00")

    for value in ("2026-10-27T09:00:00Z", "2026-10-27T09:00:00"):
        assert ids(client.get(URL, params={"started_from": value})) == [e]


@pytest.mark.parametrize("param", ["started_from", "started_to"])
@pytest.mark.parametrize("value", ["yesterday", "2026-13-45", "27/10/2026", "2026-10-27 09 00", ""])
def test_unparseable_dates_are_422(client, param, value):
    response = client.get(URL, params={param: value})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", param]


def test_from_after_to_is_an_empty_page(client, wf, add):
    add(wf(), "2026-10-27T09:00:00")

    response = client.get(URL, params={"started_from": "2026-10-28", "started_to": "2026-10-26"})

    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["total"] == 0


# ---------- NULL started_at ----------


def test_null_started_at_is_excluded_when_a_bound_is_given(client, wf, add):
    w = wf()
    add(w, None)
    dated = add(w, "2026-10-27T09:00:00")

    for params in (
        {"started_from": "2020-01-01"},
        {"started_to": "2030-01-01"},
        {"started_from": "2020-01-01", "started_to": "2030-01-01"},
    ):
        response = client.get(URL, params=params)
        assert ids(response) == [dated], params
        assert response.json()["total"] == 1


def test_null_started_at_is_listed_last_when_no_bound_is_given(client, wf, add):
    w = wf()
    undated = add(w, None)
    old = add(w, "2020-01-01T00:00:00")
    new = add(w, "2026-10-27T09:00:00")

    response = client.get(URL)

    assert ids(response) == [new, old, undated]
    assert response.json()["total"] == 3


# ---------- combining filters ----------


def test_filters_combine_with_and(client, wf, add):
    a, b = wf("a"), wf("b")
    add(a, "2026-10-27T09:00:00", ExecutionStatus.SUCCESS)  # wrong status
    add(b, "2026-10-27T09:00:00", ExecutionStatus.FAILED)  # wrong workflow
    add(a, "2026-10-20T09:00:00", ExecutionStatus.FAILED)  # wrong date
    hit = add(a, "2026-10-27T10:00:00", ExecutionStatus.FAILED)

    response = client.get(
        URL,
        params={
            "workflow_id": a, "status": "failed",
            "started_from": "2026-10-27", "started_to": "2026-10-27",
        },
    )

    assert ids(response) == [hit]
    assert response.json()["total"] == 1


# ---------- ordering and pagination ----------


def test_ordering_is_started_at_desc_then_id_desc(client, wf, add):
    w = wf()
    tie_1 = add(w, "2026-10-27T09:00:00")
    tie_2 = add(w, "2026-10-27T09:00:00")
    tie_3 = add(w, "2026-10-27T09:00:00")
    newest = add(w, "2026-10-28T09:00:00")
    oldest = add(w, "2026-10-26T09:00:00")

    assert ids(client.get(URL)) == [newest, tie_3, tie_2, tie_1, oldest]


def test_paging_through_equal_timestamps_never_repeats_or_skips(client, wf, add):
    w = wf()
    created = [add(w, "2026-10-27T09:00:00") for _ in range(7)]
    created += [add(w, "2026-10-27T10:00:00") for _ in range(3)]

    seen = []
    for offset in range(0, 10, 3):
        seen += ids(client.get(URL, params={"limit": 3, "offset": offset}))

    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen)) == 10


def test_limit_offset_and_total(client, wf, add):
    w = wf()
    created = [add(w, f"2026-10-27T09:00:{s:02d}") for s in range(5)]
    newest_first = list(reversed(created))

    page = client.get(URL, params={"limit": 2, "offset": 1}).json()

    assert [e["id"] for e in page["items"]] == newest_first[1:3]
    assert page["total"] == 5  # total ignores paging
    assert (page["limit"], page["offset"]) == (2, 1)


def test_default_limit_is_50(client, wf, add):
    w = wf()
    for s in range(55):
        add(w, f"2026-10-27T09:{s:02d}:00")

    body = client.get(URL).json()

    assert len(body["items"]) == 50 and body["total"] == 55 and body["limit"] == 50


def test_offset_past_the_end_is_an_empty_page_with_the_real_total(client, wf, add):
    add(wf(), "2026-10-27T09:00:00")

    response = client.get(URL, params={"offset": 100})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 1


def test_limit_200_is_allowed_and_201_is_rejected_not_clamped(client, wf, add):
    add(wf(), "2026-10-27T09:00:00")

    assert client.get(URL, params={"limit": 200}).status_code == 200
    assert client.get(URL, params={"limit": 201}).status_code == 422
    assert client.get(URL, params={"limit": 10000}).status_code == 422


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": -1}, {"offset": -1}, {"limit": "abc"}])
def test_bad_paging_values_are_422(client, params):
    assert client.get(URL, params=params).status_code == 422


# ---------- documentation ----------


def test_openapi_documents_the_date_semantics(client):
    params = {
        p["name"]: p
        for p in client.get("/openapi.json").json()["paths"]["/api/events"]["get"]["parameters"]
    }

    assert "INCLUSIVE" in params["started_from"]["description"]
    assert "EXCLUSIVE" in params["started_to"]["description"]
    assert "NEXT day" in params["started_to"]["description"]
    for name in ("started_from", "started_to"):
        assert "NULL" in params[name]["description"]
        assert "UTC" in params[name]["description"]
    assert params["limit"]["schema"]["maximum"] == 200
    assert params["limit"]["schema"]["default"] == 50
