"""The error boundary of the assistant's routes.

Every error becomes the contract's `{errors, detail}` with fixed text right here.
The gateway's own handlers never see one: its catch-all logs and returns `str(exc)`,
and exception text can quote alert values, SQL literals or the user's message.
"""
import logging
import traceback
from collections.abc import Callable, Coroutine
from pathlib import PurePath
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.services.assistant.core import metrics
from src.services.assistant.exceptions import (
    ERRORS,
    VALIDATION_MESSAGES,
    AssistantError,
    error_body,
)
from src.utils.request_id import current_request_id

logger = logging.getLogger(__name__)

MAX_LOGGED_FRAMES = 20


class AssistantRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            try:
                return await handle(request)
            except AssistantError as exc:
                return assistant_error_response(exc)
            except RequestValidationError as exc:
                return _validation_error_response(exc)
            except StarletteHTTPException as exc:
                return _http_error_response(exc)
            except Exception as exc:
                return _internal_error_response(exc, request.method)

        return bounded


def assistant_error_response(exc: AssistantError) -> JSONResponse:
    body = error_body(exc.code, exc.message, exc.field)
    body.update(exc.extra)
    return JSONResponse(body, exc.status, exc.headers)


def _validation_error_response(exc: RequestValidationError) -> JSONResponse:
    # 400 with fixed messages: pydantic's own text can quote the input.
    errors = []
    for error in exc.errors():
        location = [str(part) for part in error["loc"] if part != "body"]
        code = (
            "field_required" if error["type"].endswith("missing") else "invalid_value"
        )
        errors.append(
            {
                "field": ".".join(location) or "body",
                "code": code,
                "message": VALIDATION_MESSAGES[code],
            }
        )
    return JSONResponse({"errors": errors, "detail": errors[0]["message"]}, 400)


def _http_error_response(exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(
        exc.status_code, "http_error"
    )
    return JSONResponse(
        error_body(code, ERRORS[code].message), exc.status_code, exc.headers
    )


def _internal_error_response(exc: Exception, method: str) -> JSONResponse:
    metrics.internal_errors_total.labels(site="request").inc()
    logger.error(
        "assistant: unhandled request error",
        extra={
            "code": "internal",
            "method": method,
            "request_id": current_request_id(),
            **describe_exception(exc),
        },
    )
    return assistant_error_response(AssistantError("internal"))


def describe_exception(exc: BaseException) -> dict[str, Any]:
    """What may be logged about an exception: where it happened and its type, never
    its text, its arguments or the source lines."""
    frames = traceback.extract_tb(exc.__traceback__, limit=-MAX_LOGGED_FRAMES)
    return {
        "error_type": type(exc).__name__,
        "frames": [
            f"{PurePath(frame.filename).name}:{frame.lineno} {frame.name}"
            for frame in frames
        ],
    }
