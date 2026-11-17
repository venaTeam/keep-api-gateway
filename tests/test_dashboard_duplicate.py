"""Dashboard duplication: image-copy helpers and POST /dashboard/{id}/duplicate."""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from src.models.db.dashboard import Dashboard
from src.models.db.dashboard_image import DashboardImage
from src.models.db.tenant import Tenant
from src.repositories import db
from src.repositories.dashboard_images import (
    DashboardImageReferenceError,
    check_dashboard_image_references,
    copy_dashboard_images,
    rewrite_image_ids,
)
from src.repositories.dependencies import SINGLE_TENANT_EMAIL, SINGLE_TENANT_UUID
from tests.fixtures.client import client, test_app  # noqa

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
AUTH = {"x-api-key": "some-key"}


def _add_dashboard(
    db_session, name, tenant_id=SINGLE_TENANT_UUID, dashboard_config=None, **fields
):
    dashboard = Dashboard(
        tenant_id=tenant_id,
        dashboard_name=name,
        dashboard_config={} if dashboard_config is None else dashboard_config,
        **fields,
    )
    db_session.add(dashboard)
    db_session.commit()
    return dashboard.id


def _add_tenant(db_session, tenant_id="other-tenant"):
    db_session.add(Tenant(id=tenant_id, name=tenant_id, created_by="tests"))
    db_session.commit()
    return tenant_id


def _add_image(
    db_session, dashboard_id, age=timedelta(0), tenant_id=SINGLE_TENANT_UUID
):
    image = DashboardImage(
        tenant_id=tenant_id,
        dashboard_id=dashboard_id,
        name="seed.png",
        content_type="image/png",
        size_bytes=len(PNG),
        image_blob=PNG,
        created_by="seeder@keephq",
        created_at=datetime.utcnow() - age,
    )
    db_session.add(image)
    db_session.commit()
    return image.id


def test_rewrite_image_ids_swaps_only_upload_image_widgets():
    config = {
        "layout": [{"i": "a"}],
        "widget_data": [
            {
                "widgetType": "IMAGE",
                "image": {"source": "upload", "imageId": "old", "fit": "contain"},
            },
            {
                "widgetType": "IMAGE",
                "image": {"source": "url", "url": "https://x/y.png", "fit": "cover"},
            },
            {"widgetType": "HTML", "html": {"html": "<b>hi</b>"}},
        ],
        "appearance": {"density": "compact"},
    }
    result = rewrite_image_ids(config, {"old": "new"})
    assert result["widget_data"][0]["image"]["imageId"] == "new"
    assert result["widget_data"][1] == config["widget_data"][1]
    assert result["widget_data"][2] == config["widget_data"][2]
    assert result["appearance"] == {"density": "compact"}
    assert config["widget_data"][0]["image"]["imageId"] == "old"


def test_rewrite_image_ids_handles_empty_config():
    assert rewrite_image_ids({}, {}) == {}
    assert rewrite_image_ids(None, {}) == {}


def test_copy_dashboard_images_copies_rows_under_new_ids(db_session):
    source_id = _add_dashboard(db_session, "source")
    new_id = _add_dashboard(db_session, "copy")
    image_id = _add_image(db_session, source_id, age=timedelta(hours=1))

    before = datetime.utcnow()
    copy_dashboard_images(
        db_session,
        SINGLE_TENANT_UUID,
        source_id,
        new_id,
        {image_id: "new-id"},
        "admin@keephq",
    )
    db_session.commit()
    after = datetime.utcnow()

    db_session.expire_all()
    original = db_session.get(DashboardImage, image_id)
    copied = db_session.get(DashboardImage, "new-id")
    assert copied.tenant_id == SINGLE_TENANT_UUID
    assert copied.dashboard_id == new_id
    assert copied.image_blob == PNG
    assert copied.name == original.name
    assert copied.content_type == original.content_type
    assert copied.size_bytes == original.size_bytes
    assert copied.created_by == "admin@keephq"
    assert before <= copied.created_at <= after
    assert original.dashboard_id == source_id
    assert original.created_by == "seeder@keephq"


def test_copy_dashboard_images_rejects_image_not_owned_by_source(db_session):
    source_id = _add_dashboard(db_session, "source")
    new_id = _add_dashboard(db_session, "copy")
    other_id = _add_dashboard(db_session, "other")
    image_id = _add_image(db_session, other_id)

    with pytest.raises(DashboardImageReferenceError) as exc:
        copy_dashboard_images(
            db_session,
            SINGLE_TENANT_UUID,
            source_id,
            new_id,
            {image_id: "new-id"},
            "admin@keephq",
        )
    db_session.rollback()

    assert exc.value.invalid_image_ids == [image_id]
    assert db_session.get(DashboardImage, "new-id") is None


def test_copy_dashboard_images_rejects_source_dashboard_id_in_another_tenant(
    db_session,
):
    source_id = _add_dashboard(db_session, "source")
    new_id = _add_dashboard(db_session, "copy")
    other = _add_tenant(db_session)
    image_id = _add_image(db_session, source_id, tenant_id=other)

    with pytest.raises(DashboardImageReferenceError) as exc:
        copy_dashboard_images(
            db_session,
            SINGLE_TENANT_UUID,
            source_id,
            new_id,
            {image_id: "new-id"},
            "admin@keephq",
        )
    db_session.rollback()

    assert exc.value.invalid_image_ids == [image_id]
    assert db_session.get(DashboardImage, "new-id") is None


def test_copy_dashboard_images_rejects_pending_image(db_session):
    source_id = _add_dashboard(db_session, "source")
    new_id = _add_dashboard(db_session, "copy")
    image_id = _add_image(db_session, None)

    with pytest.raises(DashboardImageReferenceError) as exc:
        copy_dashboard_images(
            db_session,
            SINGLE_TENANT_UUID,
            source_id,
            new_id,
            {image_id: "new-id"},
            "admin@keephq",
        )
    db_session.rollback()

    assert exc.value.invalid_image_ids == [image_id]
    assert db_session.get(DashboardImage, "new-id") is None


def _upload(client, data=PNG, content_type="image/png", name="a.png"):
    return client.post(
        f"/dashboard-images?name={name}",
        content=data,
        headers={**AUTH, "Content-Type": content_type},
    )


def _image_widget(image_id, i="w-1", widget_type="IMAGE"):
    return {
        "i": i,
        "x": 0,
        "y": 0,
        "w": 4,
        "h": 4,
        "static": False,
        "name": f"widget {i}",
        "widgetType": widget_type,
        "image": {"source": "upload", "imageId": image_id, "fit": "contain"},
    }


def _config(*widgets):
    return {"layout": [], "widget_data": list(widgets)}


def _create_dashboard(client, name, config):
    return client.post(
        "/dashboard",
        headers=AUTH,
        json={"dashboard_name": name, "dashboard_config": config},
    )


def _duplicate(client, dashboard_id, name):
    return client.post(
        f"/dashboard/{dashboard_id}/duplicate",
        headers=AUTH,
        json={"dashboard_name": name},
    )


def _owner(db_session, image_id):
    db_session.expire_all()
    row = db_session.get(DashboardImage, image_id)
    return "gone" if row is None else row.dashboard_id


def _first_image_id(dashboard_body):
    return dashboard_body["dashboard_config"]["widget_data"][0]["image"]["imageId"]


def _dashboard_row(db_session, dashboard_id):
    db_session.expire_all()
    return db_session.get(Dashboard, dashboard_id)


@contextmanager
def _sqlite_transactions():
    """Open a real SQLite transaction on each begin while the block runs.

    The shared SQLite fixture's BEGIN listener fails silently, so every
    statement autocommits and a flushed row survives a rollback. This makes
    an uncommitted flush roll back as it does on PostgreSQL and MySQL.
    """

    def begin(conn):
        conn.exec_driver_sql("BEGIN")

    event.listen(db.engine, "begin", begin)
    try:
        yield
    finally:
        event.remove(db.engine, "begin", begin)


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_copies_config_and_sets_metadata(db_session, client, test_app):
    source_config = {
        "layout": [{"i": "w-1", "x": 0, "y": 0, "w": 4, "h": 4, "static": False}],
        "widget_data": [
            {
                "i": "w-1",
                "x": 0,
                "y": 0,
                "w": 4,
                "h": 4,
                "static": False,
                "name": "Critical",
                "widgetType": "PRESET",
                "preset": {"id": "p1", "name": "feed"},
                "presetPanelType": "ALERT_COUNT_PANEL",
                "showFiringOnly": True,
                "thresholds": [{"value": 0, "color": "#22c55e"}],
            },
            {
                "i": "w-2",
                "name": "Logo",
                "widgetType": "IMAGE",
                "image": {
                    "source": "url",
                    "url": "https://example.com/a.png",
                    "fit": "cover",
                },
            },
            {
                "i": "w-3",
                "name": "Note",
                "widgetType": "HTML",
                "html": {"html": "<b>hi</b>"},
            },
        ],
        "appearance": {"backgroundColor": "#f8fafc", "density": "compact"},
    }
    source_id = _add_dashboard(
        db_session,
        "d1",
        dashboard_config=source_config,
        created_by="someone@else",
        updated_by="someone@else",
    )
    source_before = _dashboard_row(db_session, source_id).dict()

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 200
    body = response.json()
    assert body["dashboard_name"] == "d1 (copy)"
    assert body["id"] != source_id
    assert body["dashboard_config"] == source_config

    copy = _dashboard_row(db_session, body["id"])
    assert copy.tenant_id == SINGLE_TENANT_UUID
    assert copy.created_by == "admin@keephq"
    assert copy.updated_by == "admin@keephq"
    assert copy.is_private is False
    source = _dashboard_row(db_session, source_id)
    assert source.created_by == "someone@else"
    assert source.dict() == source_before


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_copies_uploaded_images(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    source_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 200
    body = response.json()
    new_image_id = _first_image_id(body)
    assert new_image_id != image_id
    assert body["dashboard_config"] == _config(_image_widget(new_image_id))
    fetched = client.get(f"/dashboard-images/{new_image_id}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.content == PNG
    assert _owner(db_session, new_image_id) == body["id"]
    assert _owner(db_session, image_id) == source_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_deleting_source_keeps_copy_images(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    source_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    new_image_id = _first_image_id(_duplicate(client, source_id, "d1 (copy)").json())

    assert client.delete(f"/dashboard/{source_id}", headers=AUTH).status_code == 200

    assert client.get(f"/dashboard-images/{image_id}", headers=AUTH).status_code == 404
    fetched = client.get(f"/dashboard-images/{new_image_id}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.content == PNG


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_name_taken_is_409_and_writes_nothing(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    source_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]

    response = _duplicate(client, source_id, "d1")

    assert response.status_code == 409
    assert response.json() == {"detail": "A dashboard with this name already exists"}
    assert db_session.query(Dashboard).count() == 1
    assert db_session.query(DashboardImage).count() == 1


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_unknown_dashboard_is_404(db_session, client, test_app):
    response = _duplicate(client, "missing", "d1 (copy)")

    assert response.status_code == 404
    assert response.json() == {"detail": "Dashboard not found"}
    assert db_session.query(Dashboard).count() == 0


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_other_tenants_dashboard_is_404(db_session, client, test_app):
    other = _add_tenant(db_session)
    source_id = _add_dashboard(db_session, "d1", tenant_id=other)

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 404
    assert response.json() == {"detail": "Dashboard not found"}
    assert db_session.query(Dashboard).count() == 1


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_private_dashboard_of_another_user_is_404(
    db_session, client, test_app
):
    source_id = _add_dashboard(
        db_session, "d1", is_private=True, created_by="someone@else"
    )

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 404
    assert response.json() == {"detail": "Dashboard not found"}
    assert db_session.query(Dashboard).count() == 1


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_keeps_is_private_for_callers_own_private_dashboard(
    db_session, client, test_app
):
    source_id = _add_dashboard(
        db_session, "d1", is_private=True, created_by=SINGLE_TENANT_EMAIL
    )

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 200
    assert _dashboard_row(db_session, response.json()["id"]).is_private is True


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("name", ["", "   "])
def test_duplicate_blank_name_is_422(db_session, client, test_app, name):
    source_id = _add_dashboard(db_session, "d1")

    assert _duplicate(client, source_id, name).status_code == 422
    assert db_session.query(Dashboard).count() == 1


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_trims_name(db_session, client, test_app):
    source_id = _add_dashboard(db_session, "d1")

    response = _duplicate(client, source_id, "  d1 (copy)  ")

    assert response.status_code == 200
    assert response.json()["dashboard_name"] == "d1 (copy)"
    assert _dashboard_row(db_session, response.json()["id"]).dashboard_name == (
        "d1 (copy)"
    )


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_rejects_foreign_image_before_any_write(db_session, client, test_app):
    """A source referencing another dashboard's image is rejected and leaves no copy."""
    owner_id = _add_dashboard(db_session, "owner")
    image_id = _add_image(db_session, owner_id)
    source_id = _add_dashboard(
        db_session, "d1", dashboard_config=_config(_image_widget(image_id))
    )

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 400
    assert response.json() == {
        "message": "Dashboard references missing or unavailable images",
        "invalid_image_ids": [image_id],
    }
    assert db_session.query(Dashboard).count() == 2
    assert (
        db_session.query(Dashboard)
        .filter(Dashboard.dashboard_name == "d1 (copy)")
        .count()
        == 0
    )
    assert db_session.query(DashboardImage).count() == 1
    assert _owner(db_session, image_id) == owner_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_never_commits_when_image_copy_fails(
    db_session, client, test_app, monkeypatch
):
    """An image-copy failure after the new row is flushed returns 400 and never commits."""
    image_id = _upload(client).json()["id"]
    source_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]

    def fail_copy(*args, **kwargs):
        raise DashboardImageReferenceError(["img-x"])

    monkeypatch.setattr(db, "copy_dashboard_images", fail_copy)
    with _sqlite_transactions(), patch.object(
        db.Session, "commit", autospec=True, side_effect=db.Session.commit
    ) as commit:
        response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 400
    assert response.json() == {
        "message": "Dashboard references missing or unavailable images",
        "invalid_image_ids": ["img-x"],
    }
    commit.assert_not_called()
    db_session.expire_all()
    assert (
        db_session.query(Dashboard)
        .filter(Dashboard.dashboard_name == "d1 (copy)")
        .count()
        == 0
    )
    assert _owner(db_session, image_id) == source_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_source_config_stored_as_json_string(db_session, client, test_app):
    source_id = _add_dashboard(db_session, "d1")
    image_id = _add_image(db_session, source_id)
    stored_config = json.dumps(_config(_image_widget(image_id)))
    _dashboard_row(db_session, source_id).dashboard_config = stored_config
    db_session.commit()

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 200
    body = response.json()
    new_image_id = _first_image_id(body)
    assert new_image_id != image_id
    assert body["dashboard_config"] == _config(_image_widget(new_image_id))
    assert _owner(db_session, new_image_id) == body["id"]
    assert _dashboard_row(db_session, source_id).dashboard_config == stored_config


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_empty_config(db_session, client, test_app):
    source_id = _add_dashboard(db_session, "d1")

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 200
    assert response.json()["dashboard_config"] == {}


SQLITE_NAME_CONFLICT = (
    "UNIQUE constraint failed: dashboard.tenant_id, dashboard.dashboard_name"
)


def _integrity_error(message):
    return IntegrityError("INSERT INTO dashboard", {}, sqlite3.IntegrityError(message))


@pytest.mark.parametrize(
    "message, expected",
    [
        (
            'duplicate key value violates unique constraint "unique_dashboard_name_'
            'per_tenant"\nDETAIL:  Key (tenant_id, dashboard_name)=(keep, d1) already'
            " exists.",
            True,
        ),
        (
            "(1062, \"Duplicate entry 'keep-x' for key "
            "'dashboard.unique_dashboard_name_per_tenant'\")",
            True,
        ),
        (SQLITE_NAME_CONFLICT, True),
        (
            'insert or update on table "dashboard" violates foreign key constraint '
            '"dashboard_tenant_id_fkey"\nDETAIL:  Key (tenant_id)=(nope) is not'
            ' present in table "tenant".',
            False,
        ),
        ("FOREIGN KEY constraint failed", False),
        ("NOT NULL constraint failed: dashboard.dashboard_name", False),
    ],
)
def test_is_dashboard_name_conflict(message, expected):
    assert db._is_dashboard_name_conflict(_integrity_error(message)) is expected


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "message, status, logged",
    [(SQLITE_NAME_CONFLICT, 409, False), ("FOREIGN KEY constraint failed", 500, True)],
)
def test_duplicate_maps_only_the_name_constraint_to_409(
    db_session, client, test_app, caplog, message, status, logged
):
    """A flush failure is 409 only for the name constraint; any other is logged and raised."""
    source_id = _add_dashboard(db_session, "d1")
    real_flush = db.Session.flush

    def flush(self, *args, **kwargs):
        if any(isinstance(obj, Dashboard) for obj in self.new):
            raise _integrity_error(message)
        return real_flush(self, *args, **kwargs)

    failing_client = TestClient(client.app, raise_server_exceptions=False)
    with patch.object(db.Session, "flush", flush), caplog.at_level(
        logging.ERROR, logger="src.repositories.db"
    ):
        response = _duplicate(failing_client, source_id, "d1 (copy)")

    assert response.status_code == status
    failures = [
        r for r in caplog.records if r.getMessage() == "Failed to duplicate dashboard"
    ]
    assert len(failures) == (1 if logged else 0)
    if logged:
        assert failures[0].levelno == logging.ERROR
        assert failures[0].exc_info[0] is IntegrityError
        assert failures[0].tenant_id == SINGLE_TENANT_UUID
        assert failures[0].source_dashboard_id == source_id


def test_duplicate_locks_the_source_row(db_session):
    """The source lookup is SELECT ... FOR UPDATE, so a concurrent save or delete waits."""
    source_id = _add_dashboard(db_session, "d1")
    statements = []
    real_exec = db.Session.exec

    def exec_spy(self, statement, *args, **kwargs):
        statements.append(statement)
        return real_exec(self, statement, *args, **kwargs)

    with patch.object(db.Session, "exec", exec_spy):
        db.duplicate_dashboard(SINGLE_TENANT_UUID, source_id, "d1 (copy)", "a@b")

    source_lookup = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "FROM dashboard" in source_lookup
    assert source_lookup.rstrip().endswith("FOR UPDATE")


def test_check_references_without_pending_rejects_pending_images(db_session):
    dashboard_id = _add_dashboard(db_session, "d1")
    pending_id = _add_image(db_session, None)
    owned_id = _add_image(db_session, dashboard_id)
    config = _config(_image_widget(pending_id, "w-1"), _image_widget(owned_id, "w-2"))

    check_dashboard_image_references(
        db_session, SINGLE_TENANT_UUID, dashboard_id, config
    )
    with pytest.raises(DashboardImageReferenceError) as exc:
        check_dashboard_image_references(
            db_session, SINGLE_TENANT_UUID, dashboard_id, config, allow_pending=False
        )

    assert exc.value.invalid_image_ids == [pending_id]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_duplicate_rejects_pending_image_before_any_write(db_session, client, test_app):
    """A source referencing a pending upload is rejected before the new row is written.

    Runs without _sqlite_transactions: the fixture autocommits, so any flushed
    copy would survive and fail the row count.
    """
    image_id = _add_image(db_session, None)
    source_id = _add_dashboard(
        db_session, "d1", dashboard_config=_config(_image_widget(image_id))
    )

    response = _duplicate(client, source_id, "d1 (copy)")

    assert response.status_code == 400
    assert response.json() == {
        "message": "Dashboard references missing or unavailable images",
        "invalid_image_ids": [image_id],
    }
    assert db_session.query(Dashboard).count() == 1
    assert db_session.query(DashboardImage).count() == 1
    assert _owner(db_session, image_id) is None
