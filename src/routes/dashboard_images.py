"""Upload and serve images for dashboard IMAGE widgets.

Uploads are the raw request body with the image's Content-Type, not multipart:
Starlette spools a multipart file part of any size before a handler runs, so
the only way to enforce the size cap on the transfer itself is to read the
stream here. An upload is pending until a dashboard save claims it
(src/repositories/dashboard_images.py); pending uploads older than
PENDING_GRACE are swept on the tenant's next upload.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, func
from sqlmodel import Session, col, select

from src.config.core import config
from src.models.db.dashboard_image import DashboardImage
from src.repositories.db import get_session
from src.services.dashboard_image_validation import (
    ALLOWED_CONTENT_TYPES,
    ImageContentError,
    validate_image_content,
)
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()
logger = logging.getLogger(__name__)

PENDING_GRACE = timedelta(hours=24)
DEFAULT_MAX_BYTES = 5_242_880
DEFAULT_MAX_PENDING = 20
IMAGE_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Cache-Control": "private, max-age=31536000, immutable",
}


def max_bytes() -> int:
    """Upload size cap, read per request so it follows the environment."""
    return config("KEEP_DASHBOARD_IMAGE_MAX_BYTES", cast=int, default=DEFAULT_MAX_BYTES)


def max_pending() -> int:
    """Per-tenant cap on unclaimed uploads, read per request."""
    return config(
        "KEEP_DASHBOARD_IMAGE_MAX_PENDING", cast=int, default=DEFAULT_MAX_PENDING
    )


def _too_large(limit: int) -> HTTPException:
    return HTTPException(413, f"Image exceeds the {limit} byte limit")


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the body, failing with 413 as soon as it passes `limit` bytes."""
    chunks = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _pending_filter(tenant_id: str):
    return (
        DashboardImage.tenant_id == tenant_id,
        col(DashboardImage.dashboard_id).is_(None),
    )


@router.post("", status_code=201)
async def upload_dashboard_image(
    request: Request,
    name: str = Query(..., min_length=1, max_length=255),
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:dashboards"])
    ),
    session: Session = Depends(get_session),
):
    """Store an uploaded image as pending until a dashboard save claims it."""
    tenant_id = authenticated_entity.tenant_id
    limit = max_bytes()

    declared_length = request.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > limit:
        raise _too_large(limit)

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            415, "Unsupported image type; allowed: PNG, JPEG, GIF, WEBP, SVG"
        )

    data = await _read_capped(request, limit)
    if not data:
        raise HTTPException(400, "Image is empty")
    try:
        validate_image_content(content_type, data)
    except ImageContentError as e:
        raise HTTPException(415, f"Unsupported or corrupt image: {e}")

    session.execute(
        delete(DashboardImage).where(
            *_pending_filter(tenant_id),
            DashboardImage.created_at < datetime.utcnow() - PENDING_GRACE,
        )
    )
    pending = session.exec(
        select(func.count(DashboardImage.id)).where(*_pending_filter(tenant_id))
    ).one()
    if pending >= max_pending():
        session.commit()
        raise HTTPException(
            429, "Too many unsaved images; save or discard the dashboard first"
        )

    image = DashboardImage(
        tenant_id=tenant_id,
        name=name,
        content_type=content_type,
        size_bytes=len(data),
        image_blob=data,
        created_by=authenticated_entity.email,
    )
    session.add(image)
    session.commit()
    logger.info(
        "Dashboard image uploaded",
        extra={"tenant_id": tenant_id, "image_id": image.id, "size": len(data)},
    )
    return {
        "id": image.id,
        "name": image.name,
        "content_type": image.content_type,
        "size_bytes": image.size_bytes,
    }


@router.get("/{image_id}")
def get_dashboard_image(
    image_id: str,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:dashboards"])
    ),
    session: Session = Depends(get_session),
):
    """Serve an image of the caller's tenant; 404 when it does not exist there."""
    image = session.exec(
        select(DashboardImage).where(
            DashboardImage.tenant_id == authenticated_entity.tenant_id,
            DashboardImage.id == image_id,
        )
    ).first()
    if not image:
        raise HTTPException(404, "Image not found")
    return Response(
        content=image.image_blob,
        media_type=image.content_type,
        headers=IMAGE_RESPONSE_HEADERS,
    )
