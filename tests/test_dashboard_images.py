"""Dashboard image widget: model, upload/fetch routes and the save-time claim."""

from src.models.db.all_models import declared_tables


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
