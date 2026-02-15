import logging

from fastapi import APIRouter, Depends

from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get(
    "",
    description="Get tenant id",
)
def get_tenant_id(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:settings"])
    ),
) -> dict:
    tenant_id = authenticated_entity.tenant_id
    return {
        "tenant_id": tenant_id,
    }

