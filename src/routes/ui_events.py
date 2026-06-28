"""
Product-BI beacon for client-only UI events (Phase 2).

keep-ui posts fire-and-forget events here for actions that do NOT otherwise hit
the gateway (page views, workflow builder, search, Copilot, etc.). Actions that
already reach the gateway are counted server-side at their business-logic
chokepoints with source from the X-Keep-Source header — they must NOT also be
sent here, to avoid double counting.

The endpoint is authenticated (tenant_id from the identity) and validates
feature/action against the server-side allow-list (cardinality firewall):
unknown labels are rejected with 400 and no series is created.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.services.product_metrics import record_page_load, record_user_action

logger = logging.getLogger(__name__)
router = APIRouter()


class UiEvent(BaseModel):
    feature: str
    action: str
    result: str = "success"


class PageView(BaseModel):
    route: str


@router.post("/events", status_code=202)
def record_ui_event(
    event: UiEvent,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:settings"])
    ),
) -> dict:
    """Record a client-only product event. Always source=ui."""
    recorded = record_user_action(
        tenant_id=authenticated_entity.tenant_id,
        feature=event.feature,
        action=event.action,
        result=event.result,
        source="ui",
    )
    if not recorded:
        # Allow-list rejection: unknown feature/action.
        raise HTTPException(
            status_code=400, detail="Unknown feature/action for ui event"
        )
    return {"status": "ok"}


@router.post("/page-view", status_code=202)
def record_page_view(
    event: PageView,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:settings"])
    ),
) -> dict:
    """Record a keep-ui page view (moved server-side). route is allow-listed."""
    recorded = record_page_load(
        tenant_id=authenticated_entity.tenant_id, route=event.route
    )
    if not recorded:
        raise HTTPException(status_code=400, detail="Unknown route for page view")
    return {"status": "ok"}
