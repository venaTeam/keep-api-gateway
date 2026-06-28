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


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        identity = _extract_identity(request, attribute="keep_tenant_id")
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
        logger.info(
            f"Request finished: {request.method} {request.url.path} {response.status_code} in {end_time - start_time:.2f}s",
            extra={
                "tenant_id": identity,
                "status_code": response.status_code,
            },
        )
        return response
