import logging
from typing import List

from fastapi import APIRouter, Depends

from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.services.identity_manager.tenant_access import get_groups

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
    # `role` is the backend-resolved role (includes the env-based `superadmin`),
    # which the Keycloak token claim does not carry -- the UI gates buttons on it.
    return {
        "tenant_id": authenticated_entity.tenant_id,
        "role": authenticated_entity.role,
    }


@router.get(
    "/groups",
    description="Get all Keycloak groups the current user belongs to",
)
def get_my_groups(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:settings"])
    ),
) -> List[str]:
    return sorted(get_groups(authenticated_entity))

