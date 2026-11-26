"""Human identities for route tests.

`test_app="SINGLE_TENANT"` runs the DB verifier, which accepts an HS256 JWT whose
`role` claim is enforced against rbac. It is the only test auth mode in which a
request can carry any role and a real email (noauth is always admin, and API keys
have an `api_key_name`, so they take the "by API" audit branch).
"""

import jwt

from src.repositories.dependencies import GENERIC_TENANT_UUID

# Set by the `test_app` fixture for string params.
TEST_JWT_SECRET = "somesecret"


def user_email(role: str) -> str:
    return f"{role}@example.com"


def bearer_headers(
    role: str, tenant_id: str = GENERIC_TENANT_UUID, email: str | None = None, **extra
) -> dict[str, str]:
    """Authorization header of a user holding `role` (plus any extra headers)."""
    token = jwt.encode(
        {"tenant_id": tenant_id, "email": email or user_email(role), "role": role},
        TEST_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", **extra}
