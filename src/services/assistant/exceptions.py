"""Every error a client can receive, with its status and its fixed message.

A response body never carries exception text: the message is looked up here by
code, so nothing internal can leak through it.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    message: str


ERRORS: dict[str, ErrorSpec] = {
    "unauthenticated": ErrorSpec(401, "Sign in again to use the assistant."),
    "forbidden": ErrorSpec(403, "You do not have access to the assistant."),
    "identity_unavailable": ErrorSpec(
        403, "Your account has no email address, so the assistant cannot be used."
    ),
    "conversation_not_found": ErrorSpec(404, "Conversation not found."),
    "draft_not_found": ErrorSpec(404, "Draft not found."),
    "not_found": ErrorSpec(404, "Not found."),
    "method_not_allowed": ErrorSpec(405, "Method not allowed."),
    "turn_in_progress": ErrorSpec(
        409, "The assistant is still answering in this conversation."
    ),
    "draft_not_pending": ErrorSpec(409, "This draft was already handled."),
    "draft_expired": ErrorSpec(409, "This draft has expired."),
    "draft_applying": ErrorSpec(409, "This draft is already being applied."),
    "draft_unsupported": ErrorSpec(
        409, "This draft cannot be applied by this version of Keep."
    ),
    "payload_too_large": ErrorSpec(413, "The request is too large."),
    "rate_limited": ErrorSpec(429, "Too many messages. Try again shortly."),
    "too_many_active_turns": ErrorSpec(
        429, "Too many answers are in progress. Try again shortly."
    ),
    "http_error": ErrorSpec(400, "The request could not be handled."),
    "internal": ErrorSpec(500, "Something went wrong."),
    "gateway_unavailable": ErrorSpec(502, "Keep could not be reached. Try again."),
    "gateway_incompatible": ErrorSpec(
        503, "The assistant is not available with this Keep version."
    ),
    "not_ready": ErrorSpec(503, "The service is not ready."),
    "llm_not_configured": ErrorSpec(503, "The assistant is not configured."),
}

# Field-level codes of a 400 validation response.
VALIDATION_MESSAGES: dict[str, str] = {
    "field_required": "This field is required.",
    "invalid_value": "This value is not valid.",
}


class AssistantError(Exception):
    """A failure with a client-visible code. Anything else becomes `internal`."""

    def __init__(
        self,
        code: str,
        *,
        field: str | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        spec = ERRORS[code]  # an unknown code is a programming error
        super().__init__(code)
        self.code = code
        self.status = spec.status
        self.message = spec.message
        self.field = field
        self.headers = headers
        # Extra top-level body keys, e.g. the current `draft` on a 409.
        self.extra = extra or {}


def error_body(code: str, message: str, field: str | None = None) -> dict[str, Any]:
    """The one error shape. `detail` repeats the message because keep-ui's
    ApiClient reads it and throws a TypeError on a 403 without it."""
    return {
        "errors": [{"field": field, "code": code, "message": message}],
        "detail": message,
    }
