from fastapi import APIRouter, Depends

from src.repositories.db import get_tags as get_tags_db
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()


@router.get("", description="get tags")
def get_tags(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:presets"])
    ),
) -> list[dict]:
    tags = get_tags_db(authenticated_entity.tenant_id)
    return tags

