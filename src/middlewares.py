import logging
import os
import time

import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


def _extract_identity(request: Request, attribute="email") -> str:
    try:
        authorization = request.headers.get("Authorization")
        if not authorization:
            return "anonymous"

        token = authorization.split(" ")[1]
        decoded_token = jwt.decode(token, options={"verify_signature": False})
        return decoded_token.get(attribute)
    except Exception:
        return "anonymous"


PROBE_PATHS = frozenset({"/readyz", "/healthcheck"})


class LoggingMiddleware(BaseHTTPMiddleware):
    """Logs the start and end of every request, except the probes.

    Kubelet hits `PROBE_PATHS` every few seconds on every pod, at two log lines
    each, and they say nothing worth keeping. Only the logging is skipped for
    them — the rest of the middleware still runs, because `request.state.tenant_id`
    is set here and the catch-all exception handler in `main.py` reads it.
    """

    async def dispatch(self, request: Request, call_next):
        identity = _extract_identity(request, attribute="keep_tenant_id")
        is_probe = request.url.path in PROBE_PATHS
        if not is_probe:
            logger.info(
                f"Request started: {request.method} {request.url.path}",
                extra={"tenant_id": identity},
            )

        # for debugging purposes, log the payload
        if os.environ.get("LOG_AUTH_PAYLOAD", "false") == "true":
            logger.info(f"Request headers: {request.headers}")

        # Record the product-metric source (ui|api) for this request so deep
        # business-logic chokepoints can label keep_user_action_total without
        # threading the request object through. keep-ui's ApiClient sends
        # X-Keep-Source: ui; everything else defaults to api.
        try:
            from src.services.product_metrics import set_request_source

            set_request_source(request.headers.get("X-Keep-Source"))
        except Exception:
            pass

        start_time = time.time()
        request.state.tenant_id = identity
        response = await call_next(request)

        end_time = time.time()
        identity = getattr(request.state, "tenant_id", identity)
        if not is_probe:
            logger.info(
                f"Request finished: {request.method} {request.url.path} {response.status_code} in {end_time - start_time:.2f}s",
                extra={
                    "tenant_id": identity,
                    "status_code": response.status_code,
                },
            )
        return response
