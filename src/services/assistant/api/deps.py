"""Request dependencies and path helpers shared by the routes."""
import logging
import time
import uuid

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from src.services.assistant.api.guard import single_bearer
from src.services.assistant.api.route import describe_exception
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.gateway.client import Caller, GatewayClient
from src.services.assistant.gateway.errors import (
    GatewayForbidden,
    GatewayIncompatible,
    GatewayUnauthorized,
    GatewayUnavailable,
)
from src.services.assistant.gateway.transport import UNKNOWN_CLIENT
from src.services.assistant.models.identity import Identity
from src.utils.request_id import current_request_id

logger = logging.getLogger(__name__)


async def request_started() -> float:
    """time.monotonic() before any other dependency ran."""
    return time.monotonic()


async def require_ready() -> None:
    """Before anything that touches them: the tables come from another release job."""
    if db_core.schema_verified():
        return
    try:
        missing = await run_in_threadpool(db_core.check_schema)
    except Exception as exc:
        metrics.internal_errors_total.labels(site="readiness_database").inc()
        logger.error("assistant: database check failed", extra=describe_exception(exc))
        raise AssistantError("not_ready") from None
    if missing:
        metrics.internal_errors_total.labels(site="readiness_schema").inc()
        logger.error("assistant: schema incomplete", extra={"missing": missing})
        raise AssistantError("not_ready")


def require_bearer(request: Request) -> str:
    """What the guard already checked; no route may depend on that alone."""
    value = single_bearer(request.headers.getlist("authorization"))
    if value is None:
        raise AssistantError("unauthenticated", headers={"WWW-Authenticate": "Bearer"})
    return value


def request_id() -> str:
    """A UUID either way: the gateway's routes refuse any other X-Request-ID."""
    return current_request_id() or str(uuid.uuid4())


async def get_identity(request: Request) -> Identity:
    """Asks the gateway on every request: a cached answer would outlive a revoked
    token, a changed role or a switched tenant."""
    authorization = require_bearer(request)
    gateway: GatewayClient = request.app.state.assistant_gateway
    client = tuple(request.client) if request.client else UNKNOWN_CLIENT
    caller = Caller(authorization, request_id(), client)
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
        client=client,
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
