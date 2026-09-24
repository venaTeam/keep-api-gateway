"""Keeps dashboardimage rows in step with the dashboards that reference them.

Called from the dashboard write path in src/repositories/db.py with that
function's session, so claims and deletions commit atomically with the
dashboard row. Only columns are read here, never image blobs.
"""

from sqlalchemy import delete, update
from sqlmodel import Session, col, select

from src.models.db.dashboard_image import DashboardImage

IMAGE_WIDGET_TYPE = "IMAGE"
UPLOAD_SOURCE = "upload"


class DashboardImageReferenceError(ValueError):
    """A dashboard references images that are missing or not available to it."""

    def __init__(self, invalid_image_ids: list[str]):
        super().__init__(f"invalid dashboard image references: {invalid_image_ids}")
        self.invalid_image_ids = invalid_image_ids


def referenced_image_ids(dashboard_config: dict | None) -> set[str]:
    """Uploaded-image ids used by IMAGE widgets in `dashboard_config`.

    Raises DashboardImageReferenceError([]) for an upload widget without an id,
    which is a widget whose upload never completed.
    """
    ids = set()
    for widget in (dashboard_config or {}).get("widget_data") or []:
        if (
            not isinstance(widget, dict)
            or widget.get("widgetType") != IMAGE_WIDGET_TYPE
        ):
            continue
        image = widget.get("image") or {}
        if image.get("source") != UPLOAD_SOURCE:
            continue
        image_id = image.get("imageId")
        if not isinstance(image_id, str) or not image_id:
            raise DashboardImageReferenceError([])
        ids.add(image_id)
    return ids


def _current_owners(session: Session, tenant_id: str, ids: set[str]) -> dict:
    """{image id -> dashboard_id or None} for the tenant's images among `ids`."""
    rows = session.exec(
        select(DashboardImage.id, DashboardImage.dashboard_id).where(
            DashboardImage.tenant_id == tenant_id, col(DashboardImage.id).in_(ids)
        )
    ).all()
    return {image_id: owner for image_id, owner in rows}


def sync_dashboard_images(
    session: Session, tenant_id: str, dashboard_id: str, dashboard_config: dict | None
) -> None:
    """Claim the images `dashboard_config` references; delete the ones it dropped.

    Raises DashboardImageReferenceError when a referenced id does not exist for
    this tenant, or belongs to another dashboard, including one that claimed it
    concurrently. The caller's transaction must then be rolled back.
    """
    wanted = referenced_image_ids(dashboard_config)
    owners = _current_owners(session, tenant_id, wanted) if wanted else {}
    invalid = sorted(
        i for i in wanted if i not in owners or owners[i] not in (None, dashboard_id)
    )
    if invalid:
        raise DashboardImageReferenceError(invalid)

    pending = sorted(i for i, owner in owners.items() if owner is None)
    if pending:
        claimed = session.execute(
            update(DashboardImage)
            .where(
                DashboardImage.tenant_id == tenant_id,
                col(DashboardImage.id).in_(pending),
                col(DashboardImage.dashboard_id).is_(None),
            )
            .values(dashboard_id=dashboard_id)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != len(pending):
            lost = (
                sorted(
                    i
                    for i, owner in _current_owners(
                        session, tenant_id, set(pending)
                    ).items()
                    if owner != dashboard_id
                )
                or pending
            )
            raise DashboardImageReferenceError(lost)

    released = delete(DashboardImage).where(
        DashboardImage.tenant_id == tenant_id,
        DashboardImage.dashboard_id == dashboard_id,
    )
    if wanted:
        released = released.where(col(DashboardImage.id).not_in(wanted))
    session.execute(released.execution_options(synchronize_session=False))


def delete_dashboard_images(
    session: Session, tenant_id: str, dashboard_id: str
) -> None:
    """Delete every image the dashboard owns (the FK cascade is only a backstop)."""
    session.execute(
        delete(DashboardImage)
        .where(
            DashboardImage.tenant_id == tenant_id,
            DashboardImage.dashboard_id == dashboard_id,
        )
        .execution_options(synchronize_session=False)
    )
