import json
import os

import requests
from fastapi import HTTPException
from fastapi.routing import APIRoute
from keycloak.exceptions import KeycloakDeleteError, KeycloakGetError, KeycloakPostError
from keycloak.openid_connection import KeycloakOpenIDConnection
from starlette.routing import Route

from src.services.identity_manager.identity_managers.keycloak.keycloak_authverifier import (
    KeycloakAuthVerifier,
)
from src.config.core import config
from src.repositories.db import get_resource_ids_by_resource_type
from src.models.user import Group, PermissionEntity, ResourcePermission, Role, User
from src.services.context_manager import ContextManager
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.authverifierbase import AuthVerifierBase, get_all_scopes
from src.services.identity_manager.identitymanager import PREDEFINED_ROLES, BaseIdentityManager
from keycloak import KeycloakAdmin

# Some good sources on this topic:
# 1. https://stackoverflow.com/questions/42186537/resources-scopes-permissions-and-policies-in-keycloak
# 2. MUST READ - https://www.keycloak.org/docs/24.0.4/authorization_services/
# 3. ADMIN REST API - https://www.keycloak.org/docs-api/22.0.1/rest-api/index.html
# 4. (TODO) PROTECTION API - https://www.keycloak.org/docs/latest/authorization_services/index.html#_service_protection_api


class KeycloakIdentityManager(BaseIdentityManager):
    """
    RESOURCES = {
        "preset": {
            "table": "preset",
            "uid": "id",
        },
        "incident": {
            "table": "incident",
            "uid": "id",
        },
    }
    """

    RESOURCES = {}

    def __init__(self, tenant_id, context_manager: ContextManager, **kwargs):
        super().__init__(tenant_id, context_manager, **kwargs)
        self.server_url = os.environ.get("KEYCLOAK_URL")
        self.keycloak_verify_cert = (
            os.environ.get("KEYCLOAK_VERIFY_CERT", "true").lower() == "true"
        )
        try:
            self.keycloak_admin = KeycloakAdmin(
                server_url=os.environ["KEYCLOAK_URL"] + "/admin",
                username=os.environ.get("KEYCLOAK_ADMIN_USER"),
                password=os.environ.get("KEYCLOAK_ADMIN_PASSWORD"),
                realm_name=os.environ["KEYCLOAK_REALM"],
                verify=self.keycloak_verify_cert,
            )
            self.client_id = self.keycloak_admin.get_client_id(
                os.environ["KEYCLOAK_CLIENT_ID"]
            )
            self.keycloak_id_connection = KeycloakOpenIDConnection(
                server_url=os.environ["KEYCLOAK_URL"],
                client_id=os.environ["KEYCLOAK_CLIENT_ID"],
                realm_name=os.environ["KEYCLOAK_REALM"],
                client_secret_key=os.environ["KEYCLOAK_CLIENT_SECRET"],
                verify=self.keycloak_verify_cert,
            )

            self.admin_url = f"{os.environ['KEYCLOAK_URL']}/admin/realms/{os.environ['KEYCLOAK_REALM']}/clients/{self.client_id}"
            self.admin_url_without_client = f"{os.environ['KEYCLOAK_URL']}/admin/realms/{os.environ['KEYCLOAK_REALM']}"
            self.realm = os.environ["KEYCLOAK_REALM"]
            # if Keep controls the Keycloak server so it have event listener
            # for future use
            self.keep_controlled_keycloak = (
                os.environ.get("KEYCLOAK_KEEP_CONTROLLED", "false") == "true"
            )
            # Does ABAC is enabled
            self.abac_enabled = (
                os.environ.get("KEYCLOAK_ABAC_ENABLED", "true") == "true"
            )

            self.keycloak_multi_org = config(
                "KEYCLOAK_ROLES_FROM_GROUPS", default=False, cast=bool
            )

        except Exception as e:
            self.logger.error(
                "Failed to initialize Keycloak Identity Manager: %s", str(e)
            )
            raise
        self.logger.info("Keycloak Identity Manager initialized")

    def on_start(self, app) -> None:
        # if the on start process is disabled:
        if os.environ.get("SKIP_KEYCLOAK_ONSTART", "false") == "true":
            self.logger.info("Skipping keycloak on start")
            return
        # first, create all the scopes
        for scope in get_all_scopes():
            self.logger.info("Creating scope: %s", scope)
            self.create_scope(scope)
            self.logger.info("Scope created: %s", scope)
        # create resource for each route
        for route in app.routes:
            self.logger.info("Creating resource for route %s", route.path)
            # fetch the scopes for this route from the auth dependency
            if isinstance(route, Route) and not isinstance(route, APIRoute):
                self.logger.info("Skipping route: %s", route.path)
                continue
            if not route.dependant.dependencies:
                self.logger.warning("Skipping unprotected route: %s", route.path)
                continue

            scopes = []
            for dep in route.dependant.dependencies:
                # for routes that have other dependencies
                if not isinstance(dep.cache_key[0], KeycloakAuthVerifier):
                    continue
                scopes = dep.cache_key[0].scopes
                # this is the KeycloakAuthVerifier dependency :)
                methods = list(route.methods)
                if len(methods) > 1:
                    self.logger.warning(
                        "Keep does not support multiple methods for a single route",
                    )
                    continue
                protected_resource = methods[0] + " " + route.path
                dep.cache_key[0].protected_resource = protected_resource
                break

            # protected route but without scopes
            if not scopes:
                self.logger.warning("Route without scopes: %s", route.path)

            self.create_resource(
                protected_resource, scopes=scopes, resource_type="keep_route"
            )
            self.logger.info("Resource created for route: %s", route.path)

            # another thing we need to do is to add a /auth/user/orgs endpoint that will
            # return the orgs of the user for TenantSwitcher in the UI
            if self.keycloak_multi_org:
                self.logger.info("Creating /auth/user/orgs endpoint")
                from fastapi import Depends

                from src.services.identity_manager.identitymanagerfactory import (
                    IdentityManagerFactory,
                )

                # we want to add it only once to skip endless loop
                current_routes = [route.path for route in app.routes]
                if "/auth/user/orgs" not in current_routes:
                    self.logger.info("Adding /auth/user/orgs endpoint")

                    # add the endpoint
                    @app.get("/auth/user/orgs")
                    def tenant(
                        authenticated_entity: AuthenticatedEntity = Depends(
                            IdentityManagerFactory.get_auth_verifier([])
                        ),
                    ):
                        tenants = authenticated_entity.user_orgs
                        return tenants

        # create resource for each object
        if self.abac_enabled:
            for resource_type, resource_type_data in self.RESOURCES.items():
                self.logger.info("Creating resource for object %s", resource_type)
                resources = get_resource_ids_by_resource_type(
                    tenant_id=self.tenant_id,
                    table_name=resource_type_data["table"],
                    uid=resource_type_data["uid"],
                )
                for resource_id in resources:
                    resource_name = f"{resource_type}_{resource_id}"
                    resource_type_name = f"keep_{resource_type}"
                    self.create_resource(
                        resource_name=resource_name,
                        scopes=[],
                        resource_type=resource_type_name,
                    )
                self.logger.info("Resource created for object: %s", resource_type)
        for role in PREDEFINED_ROLES:
            self.logger.info("Creating role: %s", role)
            self.create_role(role, predefined=True)
            self.logger.info("Role created: %s", role)

    def _scope_name_to_id(self, all_scopes, scope_name: str) -> str:
        # if its ":*":
        if scope_name.split(":")[1] == "*":
            scope_verb = scope_name.split(":")[0]
            scope_ids = [
                scope["id"]
                for scope in all_scopes
                if scope["name"].startswith(scope_verb)
            ]
            return scope_ids
        else:
            scope = next(
                (scope for scope in all_scopes if scope["name"] == scope_name),
                None,
            )
            if not scope:
                self.logger.error(
                    "Scope %s not found in Keycloak",
                    scope_name,
                    extra={"scopes": all_scopes},
                )
                return []
            return [scope["id"]]

    def get_permission_by_name(self, permission_name):
        permissions = self.keycloak_admin.get_client_authz_permissions(self.client_id)
        permission = next(
            (
                permission
                for permission in permissions
                if permission["name"] == permission_name
            ),
            None,
        )
        return permission

    def create_scope_based_permission(self, role: Role, policy_id: str) -> None:
        try:
            scopes = role.scopes
            all_scopes = self.keycloak_admin.get_client_authz_scopes(self.client_id)
            scopes_ids = set()
            for scope in scopes:
                scope_ids = self._scope_name_to_id(all_scopes, scope)
                scopes_ids.update(scope_ids)
            resp = self.keycloak_admin.create_client_authz_scope_permission(
                client_id=self.client_id,
                payload={
                    "name": f"Permission for {role.name}",
                    "scopes": list(scopes_ids),
                    "policies": [policy_id],
                    "resources": [],
                    "decisionStrategy": "Affirmative".upper(),
                    "type": "scope",
                    "logic": "POSITIVE",
                },
            )
            return resp
        except KeycloakPostError as e:
            # if the permissions already exists, just update it
            if "already exists" in str(e):
                self.logger.info("Scope based permission already exists in Keycloak")
                # let's try to update
                try:
                    permission = self.get_permission_by_name(
                        f"Permission for {role.name}"
                    )
                    permission_id = permission.get("id")
                    resp = self.keycloak_admin.connection.raw_put(
                        path=f"{self.admin_url}/authz/resource-server/permission/scope/{permission_id}",
                        client_id=self.client_id,
                        data=json.dumps(
                            {
                                "name": f"Permission for {role.name}",
                                "scopes": list(scopes_ids),
                                "policies": [policy_id],
                                "resources": [],
                                "decisionStrategy": "Affirmative".upper(),
                                "type": "scope",
                                "logic": "POSITIVE",
                            }
                        ),
                    )
                except Exception:
                    pass
            else:
                self.logger.error(
                    "Failed to create scope based permission in Keycloak: %s", str(e)
                )
                raise HTTPException(
                    status_code=500, detail="Failed to create scope based permission"
                )

    def create_scope(self, scope: str) -> None:
        try:
            self.keycloak_admin.create_client_authz_scopes(
                self.client_id,
                {
                    "name": scope,
                    "displayName": f"Scope for {scope}",
                },
            )
        except KeycloakPostError as e:
            self.logger.error("Failed to create scopes in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to create scopes")

    def create_role(self, role: Role, predefined=False) -> str:
        try:
            role_name = self.keycloak_admin.create_client_role(
                self.client_id,
                {
                    "name": role.name,
                    "description": f"Role for {role.name}",
                    # we will use this to identify the role as predefined
                    "attributes": {
                        "predefined": [str(predefined).lower()],
                    },
                },
                skip_exists=True,
            )
            role_id = self.keycloak_admin.get_client_role_id(self.client_id, role_name)
            # create the role policy
            policy_id = self.create_role_policy(role_id, role.name, role.description)
            # create the scope based permission
            self.create_scope_based_permission(role, policy_id)
            return role_id
        except KeycloakPostError as e:
            if "already exists" in str(e):
                self.logger.info("Role already exists in Keycloak")
                # its ok!
                pass
            else:
                self.logger.error("Failed to create roles in Keycloak: %s", str(e))
                raise HTTPException(status_code=500, detail="Failed to create roles")

    def update_role(self, role_id: str, role: Role) -> str:
        # just update the policy
        role_id = self.keycloak_admin.get_client_role_id(self.client_id, role.name)
        scopes = role.scopes
        all_scopes = self.keycloak_admin.get_client_authz_scopes(self.client_id)
        scopes_ids = set()
        for scope in scopes:
            scope_ids = self._scope_name_to_id(all_scopes, scope)
            scopes_ids.update(scope_ids)
        # get the scope-based permission
        permissions = self.keycloak_admin.get_client_authz_permissions(self.client_id)
        permission = next(
            (
                permission
                for permission in permissions
                if permission["name"] == f"Permission for {role.name}"
            ),
            None,
        )
        if not permission:
            raise HTTPException(status_code=404, detail="Permission not found")
        permission_id = permission["id"]
        permission["scopes"] = list(scopes_ids)
        resp = self.keycloak_admin.connection.raw_put(
            f"{self.admin_url}/authz/resource-server/permission/scope/{permission_id}",
            data=json.dumps(permission),
        )
        resp.raise_for_status()
        return role_id

    def create_role_policy(self, role_id: str, role_name: str, role_description) -> str:
        try:
            resp = self.keycloak_admin.connection.raw_post(
                f"{self.admin_url}/authz/resource-server/policy/role",
                data=json.dumps(
                    {
                        "name": f"Allow {role_name} to {role_description}",
                        "description": f"Allow {role_name} to {role_description}",  # future use
                        "roles": [{"id": role_id, "required": False}],
                        "logic": "POSITIVE",
                        "fetchRoles": False,
                    }
                ),
            )
            resp.raise_for_status()
            resp = resp.json()
            return resp.get("id")
        except requests.exceptions.HTTPError as e:
            if "Conflict" in str(e):
                self.logger.info("Policy already exists in Keycloak")
                # get its id
                policies = self.get_policies()
                # find by name
                policy = next(
                    (
                        policy
                        for policy in policies
                        if policy["name"] == f"Allow {role_name} to {role_description}"
                    ),
                    None,
                )
                return policy["id"]
            else:
                self.logger.error("Failed to create policies in Keycloak: %s", str(e))
                raise HTTPException(status_code=500, detail="Failed to create policies")
        except Exception as e:
            self.logger.error("Failed to create policies in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to create policies")

    @property
    def support_sso(self) -> bool:
        return True

    def get_sso_providers(self) -> list[str]:
        return []

    def get_sso_wizard_url(self, authenticated_entity: AuthenticatedEntity) -> str:
        tenant_realm = authenticated_entity.org_realm
        org_id = authenticated_entity.org_id
        return f"{self.server_url}realms/{tenant_realm}/wizard/?org_id={org_id}/#iss={self.server_url}/realms/{tenant_realm}"

    def get_users(self) -> list[User]:
        try:
            # TODO: query only users that Keep created (so not show all LDAP users)
            users = self.keycloak_admin.get_users({})
            users = [user for user in users if "firstName" in user]

            users_dto = []
            for user in users:
                # todo: should be more efficient
                groups = self.keycloak_admin.get_user_groups(user["id"])
                groups = [
                    {
                        "id": group["id"],
                        "name": group["name"],
                    }
                    for group in groups
                ]
                role = self.get_user_current_role(user_id=user.get("id"))
                user_dto = User(
                    email=user.get("email", ""),
                    name=user.get("firstName", ""),
                    role=role,
                    created_at=user.get("createdTimestamp", ""),
                    ldap=(
                        True
                        if user.get("attributes", {}).get("LDAP_ID", False)
                        else False
                    ),
                    last_login=user.get("attributes", {}).get("last-login", [""])[0],
                    groups=groups,
                )
                users_dto.append(user_dto)
            return users_dto
        except KeycloakGetError as e:
            self.logger.error("Failed to fetch users from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to fetch users")

    def create_user(
        self,
        user_email: str,
        user_name: str,
        password: str,
        role: list[str],
        groups: list[str],
    ) -> dict:
        try:
            user_data = {
                "username": user_email,
                "email": user_email,
                "enabled": True,
                "firstName": user_name,
                "lastName": user_name,
                "emailVerified": True,
            }
            if password:
                user_data["credentials"] = [
                    {"type": "password", "value": password, "temporary": False}
                ]

            user_id = self.keycloak_admin.create_user(user_data)
            if role:
                role_id = self.keycloak_admin.get_client_role_id(self.client_id, role)
                self.keycloak_admin.assign_client_role(
                    client_id=self.client_id,
                    user_id=user_id,
                    roles=[{"id": role_id, "name": role}],
                )
            for group in groups:
                self.add_user_to_group(user_id=user_id, group=group)

            return {
                "status": "success",
                "message": "User created successfully",
                "user_id": user_id,
            }
        except KeycloakPostError as e:
            if "User exists" in str(e):
                self.logger.error(
                    "Failed to create user - user %s already exists", user_email
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Failed to create user - user {user_email} already exists",
                )
            self.logger.error("Failed to create user in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to create user")

    def get_user_id_by_email(self, user_email: str) -> str:
        user_id = self.keycloak_admin.get_users(query={"email": user_email})
        if not user_id:
            self.logger.error("User does not exists")
            raise HTTPException(status_code=404, detail="User does not exists")
        elif len(user_id) > 1:
            self.logger.error("Multiple users found")
            raise HTTPException(
                status_code=500, detail="Multiple users found, please contact admin"
            )
        user_id = user_id[0]["id"]
        return user_id

    def get_user_current_role(self, user_id: str) -> str:
        current_role = (
            self.keycloak_admin.connection.raw_get(
                self.admin_url_without_client + f"/users/{user_id}/role-mappings"
            )
            .json()
            .get("clientMappings", {})
            .get(self.realm, {})
            .get("mappings")
        )

        if current_role:
            # remove uma protection
            current_role = [
                role for role in current_role if role["name"] != "uma_protection"
            ]
            # if uma_protection is the only role, then the user has no role
            if current_role:
                return current_role[0]["name"]
            else:
                return None
        else:
            return None

    def add_user_to_group(self, user_id: str, group: str):
        resp = self.keycloak_admin.connection.raw_put(
            f"{self.admin_url_without_client}/users/{user_id}/groups/{group}",
            data=json.dumps({}),
        )
        resp.raise_for_status()

    def update_user(self, user_email: str, update_data: dict) -> dict:
        try:
            user_id = self.get_user_id_by_email(user_email)
            if "role" in update_data and update_data["role"]:
                role = update_data["role"]
                # get current role and understand if needs to be updated:
                current_role = self.get_user_current_role(user_id)
                # update the role only if its different than current
                # TODO: more than one role
                if current_role != role:
                    role_id = self.keycloak_admin.get_client_role_id(
                        self.client_id, role
                    )
                    if not role_id:
                        self.logger.error("Role does not exists")
                        raise HTTPException(
                            status_code=404, detail="Role does not exists"
                        )
                    self.keycloak_admin.assign_client_role(
                        client_id=self.client_id,
                        user_id=user_id,
                        roles=[{"id": role_id, "name": role}],
                    )
            if "groups" in update_data and update_data["groups"]:
                # get the current groups
                groups = self.keycloak_admin.get_user_groups(user_id)
                groups_ids = [g.get("id") for g in groups]
                # calc with groups needs to be removed and which to be added
                groups_to_remove = [
                    group_id
                    for group_id in groups_ids
                    if group_id not in update_data["groups"]
                ]

                groups_to_add = [
                    group for group in update_data["groups"] if group not in groups_ids
                ]
                # remove
                for group in groups_to_remove:
                    self.logger.info("Leaving group")
                    resp = self.keycloak_admin.connection.raw_delete(
                        f"{self.admin_url_without_client}/users/{user_id}/groups/{group}"
                    )
                    resp.raise_for_status()
                    self.logger.info("Left group")
                # add
                for group in groups_to_add:
                    self.logger.info("Joining group")
                    self.add_user_to_group(user_id=user_id, group=group)
                    self.logger.info("Joined group")
            return {"status": "success", "message": "User updated successfully"}
        except KeycloakPostError as e:
            self.logger.error("Failed to update user in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to update user")

    def delete_user(self, user_email: str) -> dict:
        try:
            user_id = self.get_user_id_by_email(user_email)
            self.keycloak_admin.delete_user(user_id)
            # delete the policy for the user (if not implicitly deleted?)
            return {"status": "success", "message": "User deleted successfully"}
        except KeycloakDeleteError as e:
            self.logger.error("Failed to delete user from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to delete user")

    def get_auth_verifier(self, scopes: list) -> AuthVerifierBase:
        return KeycloakAuthVerifier(scopes)

    def create_resource(
        self,
        resource_name: str,
        scopes: list[str] = [],
        resource_type="keep_generic",
        attributes={},
    ) -> None:
        resource = {
            "name": resource_name,
            "displayName": f"Resource for {resource_name}",
            "type": "urn:keep:resources:" + resource_type,
            "scopes": [{"name": scope} for scope in scopes],
            "attributes": attributes,
        }
        try:
            self.keycloak_admin.create_client_authz_resource(self.client_id, resource)
        except KeycloakPostError as e:
            if "already exists" in str(e):
                self.logger.info("Resource already exists in Keycloak")
                pass
            else:
                self.logger.error("Failed to create resource in Keycloak: %s", str(e))
                raise HTTPException(status_code=500, detail="Failed to create resource")

    def delete_resource(self, resource_id: str) -> None:
        try:
            resources = self.keycloak_admin.get_client_authz_resources(
                os.environ["KEYCLOAK_CLIENT_ID"]
            )
            for resource in resources:
                if resource["uris"] == ["/resource/" + resource_id]:
                    self.keycloak_admin.delete_client_authz_resource(
                        os.environ["KEYCLOAK_CLIENT_ID"], resource["id"]
                    )
        except KeycloakDeleteError as e:
            self.logger.error("Failed to delete resource from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to delete resource")

    def get_groups(self) -> list[dict]:
        try:
            groups = self.keycloak_admin.get_groups(
                query={"briefRepresentation": False}
            )
            result = []
            for group in groups:
                group_id = group["id"]
                group_name = group["name"]
                roles = group.get("clientRoles", {}).get("keep", [])

                # Fetch members for each group
                members = self.keycloak_admin.get_group_members(group_id)
                member_names = [member.get("email", "") for member in members]
                member_count = len(members)

                result.append(
                    Group(
                        id=group_id,
                        name=group_name,
                        roles=roles,
                        memberCount=member_count,
                        members=member_names,
                    )
                )
            return result
        except KeycloakGetError as e:
            self.logger.error("Failed to fetch groups from Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to fetch groups")

    def create_user_policy(self, perm, permission: ResourcePermission) -> None:
        # we need the user id from email:
        # TODO: this is not efficient, we should cache this
        users = self.keycloak_admin.get_users({})
        user = next(
            (user for user in users if user.get("email") == perm.id),
            None,
        )
        if not user:
            raise HTTPException(status_code=400, detail="User not found")
        resp = self.keycloak_admin.connection.raw_post(
            f"{self.admin_url}/authz/resource-server/policy/user",
            data=json.dumps(
                {
                    "name": f"Allow user {user.get('id')} to access resource type {permission.resource_type} with name {permission.resource_name}",
                    "description": json.dumps(
                        {
                            "user_id": user.get("id"),
                            "user_email": user.get("email"),
                            "resource_id": permission.resource_id,
                        }
                    ),
                    "logic": "POSITIVE",
                    "users": [user.get("id")],
                }
            ),
        )
        try:
            resp.raise_for_status()
        # 409 is ok, it means the policy already exists
        except Exception as e:
            if resp.status_code != 409:
                raise e
            # just continue to next policy
            else:
                return None
        policy_id = resp.json().get("id")
        return policy_id

    def create_group_policy(self, perm, permission: ResourcePermission) -> None:
        group_name = perm.id
        group = self.keycloak_admin.get_groups(query={"search": perm.id})
        if not group or len(group) > 1:
            self.logger.error("Problem with group - should be 1 but got %s", len(group))
            raise HTTPException(status_code=400, detail="Problem with group")
        group = group[0]
        group_id = group["id"]
        resp = self.keycloak_admin.connection.raw_post(
            f"{self.admin_url}/authz/resource-server/policy/group",
            data=json.dumps(
                {
                    "name": f"Allow group {perm.id} to access resource type {permission.resource_type} with name {permission.resource_name}",
                    "description": json.dumps(
                        {
                            "group_name": group_name,
                            "group_id": group_id,
                            "resource_id": permission.resource_id,
                        }
                    ),
                    "logic": "POSITIVE",
                    "groups": [{"id": group_id, "extendChildren": False}],
                    "groupsClaim": "",
                }
            ),
        )
        try:
            resp.raise_for_status()
        # 409 is ok, it means the policy already exists
        except Exception as e:
            if resp.status_code != 409:
                raise e
            # just continue to next policy
            else:
                return None
        policy_id = resp.json().get("id")
        return policy_id

    def create_permissions(self, permissions: list[ResourcePermission]) -> None:
        # create or update
        try:
            existing_permissions = self.keycloak_admin.get_client_authz_permissions(
                self.client_id,
            )
            existing_permission_names_to_permissions = {
                permission["name"]: permission for permission in existing_permissions
            }
            for permission in permissions:
                # 1. first, create the resource if its not already created
                resp = self.keycloak_admin.create_client_authz_resource(
                    self.client_id,
                    {
                        "name": permission.resource_id,
                        "displayName": permission.resource_name,
                        "type": "urn:keep:resources:keep_" + permission.resource_type,
                        "scopes": [],
                    },
                    skip_exists=True,
                )
                # 2. create the policy if it doesn't exist:
                policies = []
                for perm in permission.permissions:
                    try:
                        if perm.type == "user":
                            policy_id = self.create_user_policy(perm, permission)
                            if policy_id:
                                policies.append(policy_id)
                            else:
                                self.logger.info("Policy already exists in Keycloak")
                        else:
                            policy_id = self.create_group_policy(perm, permission)
                            if policy_id:
                                policies.append(policy_id)
                            else:
                                self.logger.info("Policy already exists in Keycloak")

                    except KeycloakPostError as e:
                        if "already exists" in str(e):
                            self.logger.info("Policy already exists in Keycloak")
                            # its ok!
                            pass
                        else:
                            self.logger.error(
                                "Failed to create policy in Keycloak: %s", str(e)
                            )
                            raise HTTPException(
                                status_code=500, detail="Failed to create policy"
                            )
                    except Exception as e:
                        self.logger.error(
                            "Failed to create policy in Keycloak: %s", str(e)
                        )
                        raise HTTPException(
                            status_code=500, detail="Failed to create policy"
                        )
                # 3. create the permission
                permission_name = f"Permission for resource {permission.resource_name} ({permission.resource_id})"
                # if the permission already exists, update it
                if permission_name in existing_permission_names_to_permissions:
                    permission_id = existing_permission_names_to_permissions[
                        permission_name
                    ]["id"]
                    resp = self.keycloak_admin.connection.raw_put(
                        f"{self.admin_url}/authz/resource-server/permission/resource/{permission_id}",
                        data=json.dumps(
                            {
                                "name": permission_name,
                                "type": "resource",
                                "logic": "POSITIVE",
                                "decisionStrategy": "AFFIRMATIVE",
                                "config": {
                                    "default": "true",
                                    "applyPolicies": json.dumps(policies),
                                },
                                "resources": [permission.resource_id],
                                "policies": policies,
                            }
                        ),
                    )
                    resp.raise_for_status()
                # create new permission
                else:
                    self.keycloak_admin.create_client_authz_resource_permission(
                        self.client_id,
                        {
                            "name": permission_name,
                            "type": "resource",
                            "logic": "POSITIVE",
                            "decisionStrategy": "AFFIRMATIVE",
                            "config": {
                                "default": "true",
                                "applyPolicies": json.dumps(policies),
                            },
                            "resources": [permission.resource_id],
                            "policies": policies,
                        },
                    )

        except KeycloakPostError as e:
            self.logger.error("Failed to create permissions in Keycloak: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to create permissions")

    def get_permissions(self) -> list[ResourcePermission]:
        return []

    def get_user_permission_on_resource_type(
        self, resource_type: str, authenticated_entity: AuthenticatedEntity
    ) -> list[ResourcePermission]:
        # TODO: implement
        return []
