"""Dashboard image widget: model, upload/fetch routes and the save-time claim."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.models.db.all_models import declared_tables
from src.models.db.dashboard_image import DashboardImage
from src.models.db.tenant import Tenant
from src.repositories.dependencies import SINGLE_TENANT_UUID
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
    from src.routes.dashboard_images import _read_capped

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
    from src.models.db.dashboard import Dashboard

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
