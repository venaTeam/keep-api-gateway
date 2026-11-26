"""Which role may do what, for the scopes the alert status change relies on."""

import pytest

from src.services.identity_manager.rbac import get_role_by_role_name

# role -> may it use the scope
EXPECTED = {
    "write:alert": {
        "noc": True,
        "viewer": False,
        "webhook": True,
        "editor": True,
        "admin": True,
        "superadmin": True,
    },
    "delete:alert": {
        "noc": False,
        "viewer": False,
        "webhook": False,
        "editor": True,
        "admin": True,
        "superadmin": True,
    },
    "write:settings": {
        "noc": False,
        "viewer": False,
        "webhook": False,
        "editor": True,
        "admin": True,
        "superadmin": True,
    },
    "write:providers": {
        "noc": False,
        "viewer": False,
        "webhook": False,
        "editor": True,
        "admin": True,
        "superadmin": True,
    },
    "read:alert": {
        "noc": True,
        "viewer": True,
        "webhook": False,
        "editor": True,
        "admin": True,
        "superadmin": True,
    },
}

CASES = [
    (scope, role, allowed)
    for scope, roles in EXPECTED.items()
    for role, allowed in roles.items()
]


@pytest.mark.parametrize("scope,role,allowed", CASES)
def test_role_scopes(scope, role, allowed):
    assert get_role_by_role_name(role).has_scopes([scope]) is allowed


@pytest.mark.parametrize(
    "scope",
    [
        "write:incident",
        "write:preset",
        "write:rules",
        "write:workflows",
        "update:alert",
        "execute:workflows",
        "create:tenant",
    ],
)
def test_noc_writes_nothing_but_alerts(scope):
    assert not get_role_by_role_name("noc").has_scopes([scope])
