import os
from unittest.mock import patch

import pytest

from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

MOCK_TOKEN = "MOCKTOKEN"


class MockSigningKey:
    def __init__(self, key):
        self.key = key


class MockJWKClient:
    def get_signing_key_from_jwt(self, token):
        # Return a mock key. Adjust the value as needed for your tests.
        return MockSigningKey(key="mock_key")


# Function to return the mock signing key
def mock_get_signing_key_from_jwt(token):
    # Return a mock key. Adjust the value as needed for your tests.
    return MockSigningKey(key="mock_key")


def get_mock_jwt_payload(token, *args, **kwargs):
    auth_type = os.getenv("AUTH_TYPE")
    if token != MOCK_TOKEN:
        raise Exception("Invalid token")
    if auth_type == "SINGLE_TENANT":
        return {
            "tenant_id": SINGLE_TENANT_UUID,
            "keep_role": "admin",
            "email": "admin@single-tenant.com",
        }
    elif auth_type == "MULTI_TENANT":
        return {
            "keep_tenant_id": "multi-tenant-id",
            "role": "admin",
            "email": "admin@multi-tenant.com",
        }
    elif auth_type == "NO_AUTH":
        # Return a payload that represents an unauthenticated or any other state
        return {}
    else:
        # Default payload or raise an exception if needed
        return {}


@pytest.mark.parametrize(
    "test_app", ["SINGLE_TENANT", "NO_AUTH"], indirect=True
)
def test_api_key_with_header(db_session, client, test_app):
    """Tests the API key authentication with the x-api-key/digest"""
    auth_type = os.getenv("AUTH_TYPE")
    valid_api_key = "valid_api_key"
    setup_api_key(db_session, valid_api_key)

    # Test with valid API key
    response = client.get("/providers", headers={"x-api-key": valid_api_key})
    assert response.status_code == 200

    # Test with invalid API key
    response = client.get("/providers", headers={"x-api-key": "invalid_api_key"})
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 200

    # Test with digest (valid)
    response = client.get(
        "/providers", headers={"Authorization": f"Digest {valid_api_key}"}
    )
    assert response.status_code == 200

    # Test with digest (invalid)
    response = client.get(
        "/providers", headers={"Authorization": "Digest invalid_api_key"}
    )
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 200

    # Test with digest lower
    response = client.get(
        "/providers", headers={"authorization": f"digest {valid_api_key}"}
    )
    assert response.status_code == 200

    # Test with digest lower
    response = client.get(
        "/providers", headers={"authorization": "digest invalid_api_key"}
    )
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 200


@pytest.mark.parametrize(
    "test_app", ["SINGLE_TENANT", "NO_AUTH"], indirect=True
)
def test_bearer_token(db_session, client, test_app):
    """Tests the bearer token authentication"""
    auth_type = os.getenv("AUTH_TYPE")
    # Test bearer tokens
    from src.repositories import dependencies

    # Patch the jwks client (otherwise it will be None)
    dependencies.jwks_client = MockJWKClient()
    with (
        patch("jwt.decode", side_effect=get_mock_jwt_payload),
        patch(
            "jwt.PyJWKClient.get_signing_key_from_jwt",
            side_effect=mock_get_signing_key_from_jwt,
        ),
    ):
        response = client.get(
            "/providers", headers={"Authorization": f"Bearer {MOCK_TOKEN}"}
        )
        assert response.status_code == 200

        response = client.get(
            "/providers", headers={"Authorization": "Bearer invalid_token"}
        )
        assert response.status_code == 401 if auth_type != "NO_AUTH" else 200


@pytest.mark.parametrize(
    "test_app", ["SINGLE_TENANT", "NO_AUTH"], indirect=True
)
def test_webhook_api_key(db_session, client, test_app):
    """Tests the webhook API key authentication"""
    auth_type = os.getenv("AUTH_TYPE")
    valid_api_key = "valid_api_key"
    setup_api_key(db_session, valid_api_key, role="webhook")
    response = client.post(
        "/alerts/event/grafana", json={}, headers={"x-api-key": valid_api_key}
    )
    assert response.status_code == 202

    response = client.post(
        "/alerts/event/grafana", json={}, headers={"x-api-key": "invalid_api_key"}
    )
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 200

    response = client.post(
        "/alerts/event/grafana",
        json={},
        headers={"Authorization": f"Digest {valid_api_key}"},
    )
    assert response.status_code == 202

    response = client.post(
        "/alerts/event/grafana",
        json={},
        headers={"authorization": f"digest {valid_api_key}"},
    )
    assert response.status_code == 202

    response = client.post(
        "/alerts/event/grafana",
        json={},
        headers={"authorization": "digest invalid_api_key"},
    )
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 202

    response = client.post(
        "/alerts/event/grafana",
        json={},
        headers={"Authorization": "digest invalid_api_key"},
    )
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 202





@pytest.mark.parametrize(
    "test_app",
    [
        {"AUTH_TYPE": "SINGLE_TENANT", "KEEP_IMPERSONATION_ENABLED": "true"},
    ],
    indirect=True,
)
def test_api_key_impersonation_without_admin(db_session, client, test_app):
    """Tests the API key impersonation with different environment settings"""

    valid_api_key = "valid_admin_api_key"
    setup_api_key(db_session, valid_api_key, role="noc")
    response = client.get(
        "/providers",
        headers={
            "x-api-key": valid_api_key,
            "X-KEEP-USER": "testuser",
            "X-KEEP-ROLE": "noc",
        },
    )
    assert response.status_code == 401
    # check the message in the response
    assert response.json()["detail"] == "Impersonation not allowed for non-admin users"


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "SINGLE_TENANT",
            "KEEP_IMPERSONATION_ENABLED": "true",
            "KEEP_IMPERSONATION_AUTO_PROVISION": "false",
        },
    ],
    indirect=True,
)
def test_api_key_impersonation_without_user_provision(db_session, client, test_app):
    """Tests the API key impersonation with different environment settings"""

    valid_api_key = "valid_admin_api_key"
    setup_api_key(db_session, valid_api_key, role="admin")
    response = client.get(
        "/providers",
        headers={
            "x-api-key": valid_api_key,
            "X-KEEP-USER": "testuser",
            "X-KEEP-ROLE": "admin",
        },
    )
    assert response.status_code == 200

    # user should not be provisioned
    response = client.get("/auth/users", headers={"x-api-key": valid_api_key})
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "SINGLE_TENANT",
            "KEEP_IMPERSONATION_ENABLED": "true",
            "KEEP_IMPERSONATION_AUTO_PROVISION": "true",
        },
    ],
    indirect=True,
)
def test_api_key_impersonation_with_user_provision(db_session, client, test_app):
    """Tests the API key impersonation with different environment settings"""

    valid_api_key = "valid_admin_api_key"
    setup_api_key(db_session, valid_api_key, role="admin")
    response = client.get(
        "/providers",
        headers={
            "x-api-key": valid_api_key,
            "X-KEEP-USER": "testuser",
            "X-KEEP-ROLE": "admin",
        },
    )
    assert response.status_code == 200

    # check that the user exists now
    response = client.get("/auth/users", headers={"x-api-key": valid_api_key})
    assert response.status_code == 200
    assert response.json()[0].get("email") == "testuser"


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "SINGLE_TENANT",
            "KEEP_IMPERSONATION_ENABLED": "true",
            "KEEP_IMPERSONATION_AUTO_PROVISION": "true",
        },
    ],
    indirect=True,
)
def test_api_key_impersonation_provisioned_user_cant_login(
    db_session, client, test_app
):
    """Tests the API key impersonation with different environment settings"""

    valid_api_key = "valid_admin_api_key"
    setup_api_key(db_session, valid_api_key, role="admin")
    response = client.get(
        "/providers",
        headers={
            "x-api-key": valid_api_key,
            "X-KEEP-USER": "testuser",
            "X-KEEP-ROLE": "admin",
        },
    )
    assert response.status_code == 200

    # check that the user exists now
    response = client.get("/auth/users", headers={"x-api-key": valid_api_key})
    assert response.status_code == 200
    assert response.json()[0].get("email") == "testuser"

    # try to login with the user
    response = client.post(
        "/signin",
        json={"username": "testuser", "password": ""},
    )
    assert response.status_code == 401
    assert response.json()["message"] == "Empty password"


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "OAUTH2PROXY",
            "KEEP_OAUTH2_PROXY_USER_HEADER": "x-forwarded-email",
            "KEEP_OAUTH2_PROXY_USER_ROLE": "x-forwarded-groups",
        },
    ],
    indirect=True,
)
def test_oauth_proxy(db_session, client, test_app):
    """Tests the API key impersonation with different environment settings"""
    response = client.post(
        "/auth/users",
        headers={
            "x-forwarded-email": "shahar",
            "x-forwarded-groups": "noc,admin",
        },
        json={"email": "shahar", "role": "admin"},
    )
    # admin role should be able to create users
    assert response.status_code == 200

    response = client.post(
        "/auth/users",
        headers={
            "x-forwarded-email": "shahar",
            "x-forwarded-groups": "noc",
        },
        json={"email": "shahar", "role": "admin"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "OAUTH2PROXY",
            "KEEP_OAUTH2_PROXY_USER_HEADER": "x-forwarded-email",
            "KEEP_OAUTH2_PROXY_USER_ROLE": "X-Forwarded-Groups",
            "KEEP_OAUTH2_PROXY_ADMIN_ROLE": "team-platform@example.com",
            "KEEP_OAUTH2_PROXY_NOC_ROLE": "dept-engineering-product@example.com",
            "KEEP_OAUTH2_PROXY_WEBHOOK_ROLE": "foo@example.com",
            "KEEP_OAUTH2_PROXY_AUTO_CREATE_USER": "true",
        },
    ],
    indirect=True,
)
def test_oauth_proxy2(db_session, client, test_app):
    """Tests the oauth2proxy impersonation with different environment settings"""
    response = client.post(
        "/auth/users",
        headers={
            "x-forwarded-email": "shahar",
            "x-forwarded-groups": "all@example.com,aws@example.com,dept-engineering-product@example.com,team-platform@example.com",
        },
        json={"email": "shahar", "role": "admin"},
    )
    # admin role should be able to create users, noc would fail
    assert response.status_code == 200

    response = client.post(
        "/auth/users",
        headers={
            "x-forwarded-email": "shahar",
            "x-forwarded-groups": "dept-engineering-product@example.com,foo@example.com",
        },
        json={"email": "shahar", "role": "admin"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "test_app", ["SINGLE_TENANT", "NO_AUTH"], indirect=True
)
def test_deleted_api_key_authentication(db_session, client, test_app):
    """Tests that deleted API keys cannot be used for authentication"""
    import hashlib

    from src.repositories.db import get_api_key
    from src.repositories.dependencies import SINGLE_TENANT_UUID
    from src.models.db.tenant import TenantApiKey

    auth_type = os.getenv("AUTH_TYPE")
    valid_api_key = "test_deleted_key"

    # Create API key in database directly
    hash_api_key = hashlib.sha256(valid_api_key.encode()).hexdigest()
    api_key_entry = TenantApiKey(
        tenant_id=SINGLE_TENANT_UUID,
        reference_id="test_deleted",
        key_hash=hash_api_key,
        created_by="test@example.com",
        role="admin",
        is_deleted=False,
    )
    db_session.add(api_key_entry)
    db_session.commit()

    # Test that non-deleted API key works
    response = client.get("/providers", headers={"x-api-key": valid_api_key})
    assert response.status_code == 200

    # Test get_api_key function directly - should find non-deleted key
    found_key = get_api_key(valid_api_key)
    assert found_key is not None
    assert found_key.is_deleted == False

    # Mark API key as deleted
    api_key_entry.is_deleted = True
    db_session.commit()

    # Test that deleted API key is rejected
    response = client.get("/providers", headers={"x-api-key": valid_api_key})
    assert response.status_code == 401 if auth_type != "NO_AUTH" else 200

    # Test get_api_key function directly - should NOT find deleted key by default
    found_key = get_api_key(valid_api_key)
    assert found_key is None

    # Test get_api_key function with include_deleted=True - should find deleted key
    found_key = get_api_key(valid_api_key, include_deleted=True)
    assert found_key is not None
    assert found_key.is_deleted == True


# ---------------------------------------------------------------------------
# CORS middleware tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_cors_allows_trusted_origin(monkeypatch, test_app):
    """Trusted origin should receive Access-Control-Allow-Origin echoed back."""
    trusted = "https://platform.keephq.dev"
    monkeypatch.setenv("KEEP_PLATFORM_URL", trusted)

    # Re-evaluate the config so the new env var is picked up.
    import importlib, src.config.config as cfg
    importlib.reload(cfg)
    test_app.middleware_stack = None  # force middleware rebuild via TestClient

    from fastapi.testclient import TestClient
    with TestClient(test_app, raise_server_exceptions=False) as c:
        response = c.get("/", headers={"Origin": trusted})
    assert response.headers.get("access-control-allow-origin") == trusted


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_cors_blocks_untrusted_origin(monkeypatch, test_app):
    """An origin not in KEEP_CORS_TRUSTED_ORIGINS must not be echoed back."""
    monkeypatch.setenv("KEEP_PLATFORM_URL", "https://platform.keephq.dev")
    monkeypatch.setenv("KEEP_CORS_TRUSTED_ORIGINS", "https://platform.keephq.dev")

    import importlib, src.config.config as cfg
    importlib.reload(cfg)

    from fastapi.testclient import TestClient
    with TestClient(test_app, raise_server_exceptions=False) as c:
        response = c.get("/", headers={"Origin": "https://evil.example.com"})
    # CORSMiddleware must NOT echo back an untrusted origin
    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_multi_origin():
    """
    All origins in a comma-separated KEEP_CORS_TRUSTED_ORIGINS must be accepted.
    Tests the parsing logic and CORSMiddleware behaviour in isolation.
    """
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.cors import CORSMiddleware

    raw = "https://app.keep.dev,https://staging.keep.dev"
    # Replicate the same parsing logic from config.py
    trusted_origins = [o.strip() for o in raw.split(",") if o.strip()]

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=trusted_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    with TestClient(app) as c:
        for origin in trusted_origins:
            response = c.get("/ping", headers={"Origin": origin})
            assert response.headers.get("access-control-allow-origin") == origin, (
                f"Expected origin '{origin}' to be allowed"
            )

        # Also verify an untrusted origin is not echoed back
        response = c.get("/ping", headers={"Origin": "https://evil.example.com"})
        assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"
