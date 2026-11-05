import pytest

from app.models import ExecutionEvent, ExecutionStatus, Workflow

URL = "/api/workflows"


def make(client, **overrides):
    body = {"name": "Daily report", "source": "n8n", **overrides}
    response = client.post(URL, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_returns_201_and_the_output_shape(client):
    response = client.post(
        URL,
        json={
            "name": "  Daily report  ",
            "source": "n8n",
            "description": "Sends the report",
            "external_id": "abc123",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] >= 1
    assert body["name"] == "Daily report"  # whitespace stripped
    assert body["description"] == "Sends the report"
    assert body["source"] == "n8n"
    assert body["external_id"] == "abc123"
    assert body["is_active"] is True
    assert body["created_at"].endswith("Z")  # naive UTC in the DB, explicit UTC in the API
    assert set(body) == {
        "id", "name", "description", "source", "external_id", "is_active", "created_at",
    }


def test_create_with_only_required_fields(client):
    body = make(client)

    assert body["description"] is None
    assert body["external_id"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "n8n"},  # name missing
        {"name": "x"},  # source missing
        {"name": "", "source": "n8n"},
        {"name": "   ", "source": "n8n"},
        {"name": "x", "source": ""},
        {"name": "x" * 256, "source": "n8n"},
        {"name": "x", "source": "s" * 101},
        {"name": "x", "source": "n8n", "external_id": ""},
        {"name": "x", "source": "n8n", "external_id": "e" * 256},
        {"name": None, "source": "n8n"},
        {"name": 123, "source": "n8n"},
        {"name": "x", "source": "n8n", "is_active": False},  # not settable on create
        {"name": "x", "source": "n8n", "typo_field": 1},
    ],
)
def test_create_rejects_bad_input_with_422(client, payload):
    assert client.post(URL, json=payload).status_code == 422
    assert client.get(URL).json() == []  # nothing was stored


def test_get_returns_the_workflow(client):
    created = make(client)

    response = client.get(f"{URL}/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_id_is_404(client):
    response = client.get(f"{URL}/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Workflow not found"}


def test_get_non_integer_id_is_422(client):
    assert client.get(f"{URL}/abc").status_code == 422


def test_list_is_empty_at_first(client):
    response = client.get(URL)

    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_workflows_in_id_order(client):
    first = make(client, name="A")
    second = make(client, name="B")

    assert [w["id"] for w in client.get(URL).json()] == [first["id"], second["id"]]


def test_patch_changes_only_the_fields_sent(client):
    created = make(client, description="old", external_id="ext-1")

    response = client.patch(f"{URL}/{created['id']}", json={"name": "Renamed"})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "old"
    assert body["external_id"] == "ext-1"
    assert body["created_at"] == created["created_at"]
    assert client.get(f"{URL}/{created['id']}").json() == body  # persisted


def test_patch_can_clear_optional_fields_with_null(client):
    created = make(client, description="old", external_id="ext-1")

    body = client.patch(
        f"{URL}/{created['id']}", json={"description": None, "external_id": None}
    ).json()

    assert body["description"] is None
    assert body["external_id"] is None


def test_patch_empty_body_changes_nothing(client):
    created = make(client)

    response = client.patch(f"{URL}/{created['id']}", json={})

    assert response.status_code == 200
    assert response.json() == created


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": None},
        {"source": None},
        {"is_active": None},
        {"is_active": "maybe"},
        {"name": "x" * 256},
        {"id": 5},  # not patchable
        {"created_at": "2020-01-01T00:00:00"},  # not patchable
    ],
)
def test_patch_rejects_bad_input_with_422(client, payload):
    created = make(client)

    assert client.patch(f"{URL}/{created['id']}", json=payload).status_code == 422
    assert client.get(f"{URL}/{created['id']}").json() == created  # unchanged


def test_patch_unknown_id_is_404(client):
    assert client.patch(f"{URL}/999", json={"name": "x"}).status_code == 404


def test_delete_is_a_soft_delete_returning_204(client, db_session):
    created = make(client)

    response = client.delete(f"{URL}/{created['id']}")

    assert response.status_code == 204
    assert response.content == b""
    row = db_session.get(Workflow, created["id"])
    assert row is not None  # the row is still there
    assert row.is_active is False


def test_deactivated_workflow_is_hidden_from_the_default_list(client):
    keep = make(client, name="keep")
    gone = make(client, name="gone")
    client.delete(f"{URL}/{gone['id']}")

    assert [w["id"] for w in client.get(URL).json()] == [keep["id"]]


def test_include_inactive_shows_deactivated_workflows(client):
    keep = make(client, name="keep")
    gone = make(client, name="gone")
    client.delete(f"{URL}/{gone['id']}")

    body = client.get(URL, params={"include_inactive": "true"}).json()

    assert [(w["id"], w["is_active"]) for w in body] == [
        (keep["id"], True),
        (gone["id"], False),
    ]


def test_get_by_id_still_returns_a_deactivated_workflow(client):
    created = make(client)
    client.delete(f"{URL}/{created['id']}")

    response = client.get(f"{URL}/{created['id']}")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_delete_twice_is_idempotent(client):
    created = make(client)

    assert client.delete(f"{URL}/{created['id']}").status_code == 204
    assert client.delete(f"{URL}/{created['id']}").status_code == 204


def test_delete_unknown_id_is_404(client):
    assert client.delete(f"{URL}/999").status_code == 404


def test_patch_reactivates_a_deactivated_workflow(client):
    created = make(client)
    client.delete(f"{URL}/{created['id']}")

    response = client.patch(f"{URL}/{created['id']}", json={"is_active": True})

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    assert [w["id"] for w in client.get(URL).json()] == [created["id"]]


def test_events_of_a_deactivated_workflow_are_still_readable(client, db_session):
    created = make(client)
    db_session.add_all(
        [
            ExecutionEvent(workflow_id=created["id"], status=ExecutionStatus.SUCCESS),
            ExecutionEvent(workflow_id=created["id"], status=ExecutionStatus.FAILED),
        ]
    )
    db_session.commit()

    assert client.delete(f"{URL}/{created['id']}").status_code == 204

    # There is no events endpoint until M5, so read through the ORM relationship.
    db_session.expire_all()
    workflow = db_session.get(Workflow, created["id"])
    assert workflow.is_active is False
    assert sorted(e.status for e in workflow.events) == [
        ExecutionStatus.FAILED,
        ExecutionStatus.SUCCESS,
    ]


# ---------- unique (source, external_id) ----------


def test_the_same_external_id_under_the_same_source_is_a_409(client):
    make(client, source="n8n", external_id="abc")

    response = client.post(URL, json={"name": "Second", "source": "n8n", "external_id": "abc"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    assert len(client.get(URL).json()) == 1


def test_the_same_external_id_under_another_source_is_fine(client):
    make(client, source="n8n", external_id="abc")

    assert client.post(URL, json={"name": "Z", "source": "zapier", "external_id": "abc"}).status_code == 201


def test_workflows_without_an_external_id_never_collide(client):
    make(client, name="A")
    make(client, name="B")

    assert len(client.get(URL).json()) == 2


def test_patching_to_a_taken_external_id_is_a_409_and_changes_nothing(client):
    make(client, source="n8n", external_id="taken")
    other = make(client, name="Other", source="n8n", external_id="free")

    response = client.patch(f"{URL}/{other['id']}", json={"external_id": "taken"})

    assert response.status_code == 409
    assert client.get(f"{URL}/{other['id']}").json()["external_id"] == "free"


def test_a_deactivated_workflow_keeps_its_external_id(client):
    first = make(client, source="n8n", external_id="abc")
    client.delete(f"{URL}/{first['id']}")

    assert client.post(URL, json={"name": "Again", "source": "n8n", "external_id": "abc"}).status_code == 409
    assert client.patch(f"{URL}/{first['id']}", json={"is_active": True}).status_code == 200
