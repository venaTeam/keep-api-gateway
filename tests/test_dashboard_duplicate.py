"""Dashboard duplication: image-copy helpers."""

from datetime import datetime, timedelta

import pytest

from src.models.db.dashboard import Dashboard
from src.models.db.dashboard_image import DashboardImage
from src.repositories.dashboard_images import (
    DashboardImageReferenceError,
    copy_dashboard_images,
    rewrite_image_ids,
)
from src.repositories.dependencies import SINGLE_TENANT_UUID

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _add_dashboard(db_session, name):
    dashboard = Dashboard(
        tenant_id=SINGLE_TENANT_UUID, dashboard_name=name, dashboard_config={}
    )
    db_session.add(dashboard)
    db_session.commit()
    return dashboard.id


def _add_image(db_session, dashboard_id, age=timedelta(0)):
    image = DashboardImage(
        tenant_id=SINGLE_TENANT_UUID,
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
