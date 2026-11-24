"""Request dependencies and path helpers shared by the routes."""
import logging
import uuid

from fastapi import Request

from src.services.assistant.core import metrics
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.gateway.client import Caller, GatewayClient
from src.services.assistant.gateway.errors import (
    GatewayForbidden,
    GatewayIncompatible,
    GatewayUnauthorized,
    GatewayUnavailable,
)
from src.services.assistant.models.identity import Identity
from src.utils.request_id import current_request_id

logger = logging.getLogger(__name__)


def require_bearer(request: Request) -> str:
    """Exactly one Authorization header, Bearer scheme.

    The gateway also takes Basic and Digest, as API keys that act under the email
    of whoever created them: a key holder would own that user's chats here.
    """
    values = request.headers.getlist("authorization")
    if len(values) == 1:
        scheme, _, credentials = values[0].partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return values[0]
    raise AssistantError("unauthenticated", headers={"WWW-Authenticate": "Bearer"})


def request_id() -> str:
    """A UUID either way: the gateway's routes refuse any other X-Request-ID."""
    return current_request_id() or str(uuid.uuid4())


async def get_identity(request: Request) -> Identity:
    """Asks the gateway on every request: a cached answer would outlive a revoked
    token, a changed role or a switched tenant."""
    authorization = require_bearer(request)
    gateway: GatewayClient = request.app.state.assistant_gateway
    caller = Caller(authorization=authorization, request_id=request_id())
    try:
        body = await gateway.whoami(caller)
    except GatewayUnauthorized:
        raise AssistantError("unauthenticated") from None
    except GatewayForbidden:
        raise AssistantError("forbidden") from None
    except GatewayUnavailable:
        raise AssistantError("gateway_unavailable") from None
    except GatewayIncompatible:
        raise _incompatible("not an object") from None

    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise _incompatible("no tenant_id")
    # A gateway from before this feature does not send it. Without an owner the
    # rows would belong to nobody, so this must not go on.
    if "email" not in body:
        raise _incompatible("no email")
    email = body["email"]
    if not isinstance(email, str) or not email.strip():
        metrics.internal_errors_total.labels(site="identity_unavailable").inc()
        logger.warning(
            "identity: the gateway knows no email for this user",
            extra={"tenant_id": tenant_id},
        )
        raise AssistantError("identity_unavailable")
    scopes = body.get("scopes")
    return Identity(
        tenant_id=tenant_id,
        email=email,
        scopes=(
            frozenset(scope for scope in scopes if isinstance(scope, str))
            if isinstance(scopes, list)
            else None
        ),
        authorization=authorization,
    )


def _incompatible(reason: str) -> AssistantError:
    metrics.internal_errors_total.labels(site="gateway_incompatible").inc()
    logger.error("identity: unusable whoami answer", extra={"reason": reason})
    return AssistantError("gateway_incompatible")


def parse_resource_id(value: str, not_found_code: str) -> uuid.UUID:
    """A path id. A malformed one is a 404 of that resource, not a 400: it
    cannot name an existing row, and the answer stays the same for both."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AssistantError(not_found_code) from None
