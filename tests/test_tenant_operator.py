"""VENA-5596 tenant + operator management -- repository and authorization tests.

These exercise the real logic (atomic create, unique constraints, role
resolution, grant management, operator one-per-group, validation, self-demotion)
against an in-memory SQLite via the `db_session` fixture, which patches
`src.repositories.db.engine`.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.repositories import db
from src.repositories.dependencies import GENERIC_TENANT_UUID

# Import the new tables so SQLModel.metadata.create_all builds them for the test DB.
from src.models.db.operator import Operator  # noqa: F401
from src.models.db.tenant_role_grant import TenantRoleGrant  # noqa: F401
from src.services.identity_manager import tenant_access
from src.routes import tenants as tenants_route

from tests.fixtures.client import client, setup_api_key, test_app  # noqa


def _grant(subject, subject_type, role):
    return SimpleNamespace(subject=subject, subject_type=subject_type, role=role)


def _entity(email=None, groups=None, role="editor"):
    return SimpleNamespace(email=email, groups=groups or [], role=role)


# --- create_tenant_atomic -------------------------------------------------


def test_create_tenant_persists_tenant_and_grants(db_session):
    tenant = db.create_tenant_atomic(
        name="org-a",
        role_mappings=[
            _grant("alice@corp.com", "user", "admin"),
            _grant("/org-a", "group", "editor"),
        ],
        created_by="root",
    )
    assert tenant.name == "org-a"
    grants = db.list_grants(tenant.id)
    assert {(g.subject, g.role) for g in grants} == {
        ("alice@corp.com", "admin"),
        ("/org-a", "editor"),
    }


def test_create_tenant_duplicate_name_conflicts(db_session):
    db.create_tenant_atomic(name="dup", role_mappings=[_grant("a@b", "user", "admin")])
    with pytest.raises(db.TenantNameConflict):
        db.create_tenant_atomic(
            name="dup", role_mappings=[_grant("c@d", "user", "admin")]
        )
    # the prepopulated tenant name is also protected
    with pytest.raises(db.TenantNameConflict):
        db.create_tenant_atomic(
            name="test-tenant", role_mappings=[_grant("c@d", "user", "admin")]
        )


# --- update_tenant --------------------------------------------------------


def test_update_tenant_name(db_session):
    tenant = db.create_tenant_atomic(
        name="org-u", role_mappings=[_grant("a@b", "user", "admin")]
    )
    updated = db.update_tenant(tenant.id, name="org-u2")
    assert updated.name == "org-u2"


def test_update_tenant_name_conflict(db_session):
    db.create_tenant_atomic(name="taken", role_mappings=[_grant("a@b", "user", "admin")])
    tenant = db.create_tenant_atomic(
        name="org-move", role_mappings=[_grant("c@d", "user", "admin")]
    )
    with pytest.raises(db.TenantNameConflict):
        db.update_tenant(tenant.id, name="taken")


def test_update_tenant_not_found(db_session):
    with pytest.raises(db.TenantNotFound):
        db.update_tenant("nope", name="x")


# --- grants + role resolution --------------------------------------------


def test_role_resolution_strongest_wins(db_session):
    tenant = db.create_tenant_atomic(
        name="org-r", role_mappings=[_grant("boss@corp", "user", "admin")]
    )
    db.add_grant(tenant.id, "/sre", "group", "editor")
    db.add_grant(tenant.id, "/all", "group", "viewer")
    assert db.get_tenant_role_for_subjects(tenant.id, ["boss@corp"]) == "admin"
    assert db.get_tenant_role_for_subjects(tenant.id, ["/sre"]) == "editor"
    # a subject in both an editor and viewer group -> strongest (editor)
    assert db.get_tenant_role_for_subjects(tenant.id, ["/sre", "/all"]) == "editor"
    assert db.get_tenant_role_for_subjects(tenant.id, ["nobody"]) is None


def test_add_grant_is_idempotent_and_upgrades(db_session):
    tenant = db.create_tenant_atomic(
        name="org-g", role_mappings=[_grant("a@b", "user", "admin")]
    )
    db.add_grant(tenant.id, "carol@corp", "user", "viewer")
    db.add_grant(tenant.id, "carol@corp", "user", "editor")  # upgrade
    assert db.get_tenant_role_for_subjects(tenant.id, ["carol@corp"]) == "editor"
    assert db.remove_grant(tenant.id, "carol@corp") is True
    assert db.get_tenant_role_for_subjects(tenant.id, ["carol@corp"]) is None
    assert db.remove_grant(tenant.id, "carol@corp") is False


def test_get_tenants_for_subjects(db_session):
    t1 = db.create_tenant_atomic(
        name="org-1", role_mappings=[_grant("dev@corp", "user", "admin")]
    )
    t2 = db.create_tenant_atomic(
        name="org-2", role_mappings=[_grant("/team", "group", "editor")]
    )
    db.create_tenant_atomic(name="org-3", role_mappings=[_grant("x@y", "user", "admin")])
    ids = {t.id for t in db.get_tenants_for_subjects(["dev@corp", "/team"])}
    assert ids == {t1.id, t2.id}
    assert db.get_tenants_for_subjects([]) == []


# --- operators ------------------------------------------------------------


def test_create_operator_unique_group_per_tenant(db_session):
    op = db.create_operator(group="grp-solo", tenant_id=GENERIC_TENANT_UUID)
    assert op.group == "grp-solo"
    with pytest.raises((db.OperatorGroupTaken, db.OperatorNameTaken)):
        db.create_operator(group="grp-solo", tenant_id=GENERIC_TENANT_UUID)

    # Different group on the same tenant works
    db.create_operator(group="/g1", tenant_id=GENERIC_TENANT_UUID)
    db.create_operator(group="/g2", tenant_id=GENERIC_TENANT_UUID)
    in_use = db.operator_groups_in_use()
    assert {"/g1", "/g2"} <= in_use
    # available = a caller's groups minus in-use
    caller_groups = ["/g1", "/g2", "/g3"]
    available = sorted(g for g in caller_groups if g not in in_use)
    assert available == ["/g3"]


# --- authorization helper -------------------------------------------------


def test_resolve_tenant_role_superadmin_is_admin_anywhere(db_session):
    entity = _entity(email="su@corp", role="superadmin")
    assert tenant_access.resolve_tenant_role(entity, "any-tenant") == "admin"


def test_resolve_tenant_role_from_grant(db_session):
    tenant = db.create_tenant_atomic(
        name="org-auth", role_mappings=[_grant("admin@corp", "user", "admin")]
    )
    db.add_grant(tenant.id, "/viewers", "group", "viewer")
    admin_entity = _entity(email="admin@corp", role="editor")
    viewer_entity = _entity(email="x@corp", groups=["/viewers"], role="editor")
    stranger = _entity(email="nobody@corp", role="editor")
    assert tenant_access.resolve_tenant_role(admin_entity, tenant.id) == "admin"
    assert tenant_access.resolve_tenant_role(viewer_entity, tenant.id) == "viewer"
    assert tenant_access.resolve_tenant_role(stranger, tenant.id) is None


def test_assert_tenant_role_raises_403(db_session):
    tenant = db.create_tenant_atomic(
        name="org-403", role_mappings=[_grant("admin@corp", "user", "admin")]
    )
    stranger = _entity(email="nobody@corp", role="editor")
    with pytest.raises(HTTPException) as exc:
        tenant_access.assert_tenant_role(stranger, tenant.id, "viewer")
    assert exc.value.status_code == 403


# --- route validation helpers ---------------------------------------------


def test_validate_role_mappings_rejects_bad_input():
    with pytest.raises(HTTPException) as e1:
        tenants_route._validate_role_mappings(
            [tenants_route.RoleAssignment(subject="a", subject_type="team", role="admin")]
        )
    assert e1.value.status_code == 400

    with pytest.raises(HTTPException) as e2:
        tenants_route._validate_role_mappings(
            [tenants_route.RoleAssignment(subject="a", subject_type="user", role="owner")]
        )
    assert e2.value.status_code == 400

    with pytest.raises(HTTPException) as e3:
        tenants_route._validate_role_mappings(
            [
                tenants_route.RoleAssignment(subject="a", subject_type="user", role="admin"),
                tenants_route.RoleAssignment(subject="a", subject_type="user", role="viewer"),
            ]
        )
    assert e3.value.status_code == 400


def test_create_tenant_endpoint_rejects_non_superadmin(db_session):
    body = tenants_route.CreateTenantRequest(
        name="org-gate",
        role_mappings=[tenants_route.RoleAssignment(
            subject="a@b", subject_type="user", role="admin"
        )],
    )
    with pytest.raises(HTTPException) as exc:
        tenants_route.create_tenant_endpoint(body=body, authenticated_entity=_entity(role="admin"))
    assert exc.value.status_code == 403


def test_create_tenant_endpoint_allows_superadmin(db_session):
    body = tenants_route.CreateTenantRequest(
        name="org-gate-ok",
        role_mappings=[tenants_route.RoleAssignment(
            subject="a@b", subject_type="user", role="admin"
        )],
    )
    result = tenants_route.create_tenant_endpoint(
        body=body, authenticated_entity=_entity(email="su@corp", role="superadmin")
    )
    assert result["name"] == "org-gate-ok"
    assert db.get_tenant(result["id"]) is not None


def test_guard_self_modification():
    admin_entity = _entity(email="me@corp", groups=["/mine"], role="admin")
    # own email -> blocked
    with pytest.raises(HTTPException) as e1:
        tenants_route._guard_self_modification(admin_entity, "me@corp")
    assert e1.value.status_code == 400
    # own group -> blocked
    with pytest.raises(HTTPException):
        tenants_route._guard_self_modification(admin_entity, "/mine")
    # someone else -> allowed
    tenants_route._guard_self_modification(admin_entity, "other@corp")
    # superadmin is exempt
    su = _entity(email="su@corp", role="superadmin")
    tenants_route._guard_self_modification(su, "su@corp")
