"""Dashboard image widget: model, upload/fetch routes and the save-time claim."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.models.db.all_models import declared_tables
from src.models.db.dashboard import Dashboard
from src.models.db.dashboard_image import DashboardImage
from src.models.db.tenant import Tenant
from src.repositories import dashboard_images as dashboard_images_module
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.repositories.dashboard_images import (
    DashboardImageReferenceError,
    check_dashboard_image_references,
)
from src.routes.dashboard_images import _read_capped
from tests.fixtures.client import client, test_app  # noqa

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
AUTH = {"x-api-key": "some-key"}


def test_dashboardimage_is_declared_with_migration_columns():
    assert declared_tables()["dashboardimage"] == {
        "id",
        "tenant_id",
        "dashboard_id",
        "name",
        "content_type",
        "size_bytes",
        "image_blob",
        "created_by",
        "created_at",
    }


def _upload(client, data=PNG, content_type="image/png", name="a.png"):
    return client.post(
        f"/dashboard-images?name={name}",
        content=data,
        headers={**AUTH, "Content-Type": content_type},
    )


def _add_image(
    db_session, tenant_id=SINGLE_TENANT_UUID, dashboard_id=None, age=timedelta(0)
):
    image = DashboardImage(
        tenant_id=tenant_id,
        dashboard_id=dashboard_id,
        name="seed.png",
        content_type="image/png",
        size_bytes=len(PNG),
        image_blob=PNG,
        created_at=datetime.utcnow() - age,
    )
    db_session.add(image)
    db_session.commit()
    return image.id


def _add_tenant(db_session, tenant_id="other-tenant"):
    db_session.add(Tenant(id=tenant_id, name=tenant_id, created_by="tests"))
    db_session.commit()
    return tenant_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "data,content_type",
    [(PNG, "image/png"), (SVG, "image/svg+xml")],
)
def test_upload_then_fetch_round_trip(db_session, client, test_app, data, content_type):
    created = _upload(client, data, content_type)
    assert created.status_code == 201
    body = created.json()
    assert body["content_type"] == content_type
    assert body["size_bytes"] == len(data)

    fetched = client.get(f"/dashboard-images/{body['id']}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.content == data
    assert fetched.headers["content-type"].startswith(content_type)
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert fetched.headers["content-security-policy"] == "sandbox"
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert fetched.headers["content-disposition"] == "attachment"
    assert fetched.headers["cross-origin-resource-policy"] == "same-origin"

    row = db_session.get(DashboardImage, body["id"])
    assert row.dashboard_id is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_unsupported_type_is_415(db_session, client, test_app):
    assert _upload(client, b"<html/>", "text/html").status_code == 415


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_bytes_not_matching_type_is_415(db_session, client, test_app):
    assert _upload(client, SVG, "image/png").status_code == 415


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_empty_is_400(db_session, client, test_app):
    assert _upload(client, b"").status_code == 400


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_over_cap_by_content_length_is_413(
    db_session, client, test_app, monkeypatch
):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_BYTES", "20")
    assert _upload(client, PNG + b"\x00" * 10).status_code == 413
    assert db_session.query(DashboardImage).count() == 0


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_chunked_over_cap_is_413(db_session, client, test_app, monkeypatch):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_BYTES", "20")
    chunks = iter([PNG, b"\x00" * 30])
    response = client.post(
        "/dashboard-images?name=big.png",
        content=chunks,
        headers={**AUTH, "Content-Type": "image/png"},
    )
    assert response.status_code == 413
    assert db_session.query(DashboardImage).count() == 0


def test_read_capped_stops_streams_past_the_limit():
    class _Request:
        async def stream(self):
            yield b"x" * 15
            yield b"x" * 15

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read_capped(_Request(), 20))
    assert exc.value.status_code == 413


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_requires_name(db_session, client, test_app):
    response = client.post(
        "/dashboard-images",
        content=PNG,
        headers={**AUTH, "Content-Type": "image/png"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_pending_cap_is_429(db_session, client, test_app, monkeypatch):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_PENDING", "2")
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 429


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_claimed_images_do_not_count_toward_pending_cap(
    db_session, client, test_app, monkeypatch
):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_PENDING", "1")
    dashboard = Dashboard(
        tenant_id=SINGLE_TENANT_UUID, dashboard_name="d", dashboard_config={}
    )
    db_session.add(dashboard)
    db_session.commit()
    _add_image(db_session, dashboard_id=dashboard.id)
    assert _upload(client).status_code == 201


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_sweeps_only_this_tenants_stale_pending(db_session, client, test_app):
    other = _add_tenant(db_session)
    stale = _add_image(db_session, age=timedelta(hours=25))
    fresh = _add_image(db_session, age=timedelta(hours=1))
    other_stale = _add_image(db_session, tenant_id=other, age=timedelta(hours=25))

    assert _upload(client).status_code == 201

    db_session.expire_all()
    assert db_session.get(DashboardImage, stale) is None
    assert db_session.get(DashboardImage, fresh) is not None
    assert db_session.get(DashboardImage, other_stale) is not None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_fetch_unknown_is_404(db_session, client, test_app):
    assert client.get("/dashboard-images/nope", headers=AUTH).status_code == 404


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_fetch_other_tenants_image_is_404(db_session, client, test_app):
    other = _add_tenant(db_session)
    image_id = _add_image(db_session, tenant_id=other)
    assert client.get(f"/dashboard-images/{image_id}", headers=AUTH).status_code == 404


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


def _put_dashboard(client, dashboard_id, config=None, name=None):
    body = {}
    if config is not None:
        body["dashboard_config"] = config
    if name is not None:
        body["dashboard_name"] = name
    return client.put(f"/dashboard/{dashboard_id}", headers=AUTH, json=body)


def _owner(db_session, image_id):
    db_session.expire_all()
    row = db_session.get(DashboardImage, image_id)
    return "gone" if row is None else row.dashboard_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_create_claims_pending_image(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    response = _create_dashboard(client, "d1", _config(_image_widget(image_id)))
    assert response.status_code == 200
    assert _owner(db_session, image_id) == response.json()["id"]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_create_with_unknown_image_is_400_and_writes_nothing(
    db_session, client, test_app
):
    response = _create_dashboard(client, "d1", _config(_image_widget("missing")))
    assert response.status_code == 400
    assert response.json() == {
        "message": "Dashboard references missing or unavailable images",
        "invalid_image_ids": ["missing"],
    }
    assert (
        db_session.query(Dashboard).filter(Dashboard.dashboard_name == "d1").count()
        == 0
    )


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_widget_without_image_id_is_400(db_session, client, test_app):
    widget = _image_widget(None)
    response = _create_dashboard(client, "d1", _config(widget))
    assert response.status_code == 400
    assert response.json()["invalid_image_ids"] == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_other_tenants_image_is_400(db_session, client, test_app):
    other = _add_tenant(db_session)
    image_id = _add_image(db_session, tenant_id=other)
    response = _create_dashboard(client, "d1", _config(_image_widget(image_id)))
    assert response.status_code == 400
    assert _owner(db_session, image_id) is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_image_owned_by_another_dashboard_is_400(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    first = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()[
        "id"
    ]
    response = _create_dashboard(client, "d2", _config(_image_widget(image_id)))
    assert response.status_code == 400
    assert _owner(db_session, image_id) == first


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_removing_widget_deletes_its_image(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    assert _put_dashboard(client, dashboard_id, _config()).status_code == 200
    assert _owner(db_session, image_id) == "gone"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_replacing_image_swaps_ownership(db_session, client, test_app):
    old = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(old))).json()[
        "id"
    ]
    new = _upload(client).json()["id"]
    assert (
        _put_dashboard(client, dashboard_id, _config(_image_widget(new))).status_code
        == 200
    )
    assert _owner(db_session, old) == "gone"
    assert _owner(db_session, new) == dashboard_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_with_bad_reference_keeps_previous_state(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    response = _put_dashboard(client, dashboard_id, _config(_image_widget("missing")))
    assert response.status_code == 400
    assert _owner(db_session, image_id) == dashboard_id
    db_session.expire_all()
    stored = db_session.get(Dashboard, dashboard_id).dashboard_config
    assert stored["widget_data"][0]["image"]["imageId"] == image_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_rename_without_config_keeps_images(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    assert _put_dashboard(client, dashboard_id, name="renamed").status_code == 200
    assert _owner(db_session, image_id) == dashboard_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_non_image_widget_with_stale_image_field_releases_it(
    db_session, client, test_app
):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    switched = _image_widget(image_id, widget_type="METRIC")
    assert _put_dashboard(client, dashboard_id, _config(switched)).status_code == 200
    assert _owner(db_session, image_id) == "gone"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_url_image_widgets_need_no_upload(db_session, client, test_app):
    widget = _image_widget(None)
    widget["image"] = {
        "source": "url",
        "url": "https://example.com/a.png",
        "fit": "cover",
    }
    assert _create_dashboard(client, "d1", _config(widget)).status_code == 200


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_dashboards_without_image_widgets_unchanged(db_session, client, test_app):
    config = {
        "layout": [],
        "widget_data": [{"i": "w-1", "name": "p", "widgetType": "PRESET"}],
    }
    response = _create_dashboard(client, "plain", config)
    assert response.status_code == 200
    assert response.json()["dashboard_config"] == config


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_delete_dashboard_deletes_its_images(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    assert client.delete(f"/dashboard/{dashboard_id}", headers=AUTH).status_code == 200
    assert _owner(db_session, image_id) == "gone"


def test_claim_race_loser_is_rejected(db_session, monkeypatch):
    """Second of two concurrent saves: it read the image as pending, but the guarded UPDATE matches no row."""
    first = Dashboard(
        tenant_id=SINGLE_TENANT_UUID, dashboard_name="a", dashboard_config={}
    )
    second = Dashboard(
        tenant_id=SINGLE_TENANT_UUID, dashboard_name="b", dashboard_config={}
    )
    db_session.add_all([first, second])
    db_session.commit()
    image_id = _add_image(db_session)
    config = _config(_image_widget(image_id))

    dashboard_images_module.sync_dashboard_images(
        db_session, SINGLE_TENANT_UUID, first.id, config
    )
    db_session.commit()

    monkeypatch.setattr(
        dashboard_images_module,
        "_current_owners",
        lambda session, tenant_id, ids: {i: None for i in ids},
    )
    with pytest.raises(dashboard_images_module.DashboardImageReferenceError) as exc:
        dashboard_images_module.sync_dashboard_images(
            db_session, SINGLE_TENANT_UUID, second.id, config
        )
    db_session.rollback()
    monkeypatch.undo()

    assert exc.value.invalid_image_ids == [image_id]
    assert _owner(db_session, image_id) == first.id


def test_check_references_performs_no_writes(db_session):
    """Verify check_dashboard_image_references does not write to database."""
    config = _config(_image_widget("missing"))
    dashboard_count_before = db_session.query(Dashboard).count()
    image_count_before = db_session.query(DashboardImage).count()

    with pytest.raises(DashboardImageReferenceError):
        check_dashboard_image_references(
            db_session, SINGLE_TENANT_UUID, "test-id", config
        )

    dashboard_count_after = db_session.query(Dashboard).count()
    image_count_after = db_session.query(DashboardImage).count()
    assert dashboard_count_before == dashboard_count_after
    assert image_count_before == image_count_after


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_rename_with_bad_reference_keeps_old_name(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(
        client, "d1", _config(_image_widget(image_id))
    ).json()["id"]
    response = _put_dashboard(
        client, dashboard_id, name="renamed", config=_config(_image_widget("missing"))
    )
    assert response.status_code == 400
    db_session.expire_all()
    stored = db_session.get(Dashboard, dashboard_id)
    assert stored.dashboard_name == "d1"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_image_widget_with_non_dict_image_is_400(db_session, client, test_app):
    widget = {
        "i": "w-1",
        "x": 0,
        "y": 0,
        "w": 4,
        "h": 4,
        "static": False,
        "name": "bad widget",
        "widgetType": "IMAGE",
        "image": "oops",
    }
    response = _create_dashboard(client, "d1", _config(widget))
    assert response.status_code == 400
    assert response.json()["invalid_image_ids"] == []
