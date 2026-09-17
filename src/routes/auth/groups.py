import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.models.user import Group
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateOrUpdateGroupRequest(BaseModel):
    name: str
    roles: list[str]
    members: list[str]

    class Config:
        allow_population_by_field_name = True


@router.get("", description="Get all groups (or search a subset with ?search=)")
def get_groups(
    search: Optional[str] = Query(
        default=None,
        description="Server-side filter -- returns only matching groups (fast at scale)",
    ),
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:settings"])
    ),
) -> list[Group]:
    identity_manager = IdentityManagerFactory.get_identity_manager(
        authenticated_entity.tenant_id
    )
    groups = identity_manager.get_groups(search=search)
    return groups


@router.post("", description="Create a group")
def create_group(
    group: CreateOrUpdateGroupRequest,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:settings"])
    ),
):
    identity_manager = IdentityManagerFactory.get_identity_manager(
        authenticated_entity.tenant_id
    )
    return identity_manager.create_group(group.name, group.members, group.roles)


@router.put("/{group_name}", description="Update a group")
def update_group(
    group_name: str,
    group: CreateOrUpdateGroupRequest,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:settings"])
    ),
):
    identity_manager = IdentityManagerFactory.get_identity_manager(
        authenticated_entity.tenant_id
    )
    return identity_manager.update_group(group.name, group.members, group.roles)


@router.delete("/{group_name}", description="Delete a group")
def delete_group(
    group_name: str,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:settings"])
    ),
):
    identity_manager = IdentityManagerFactory.get_identity_manager(
        authenticated_entity.tenant_id
    )
    return identity_manager.delete_group(group_name)


