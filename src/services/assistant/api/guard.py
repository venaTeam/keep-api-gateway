"""What is refused before any other code of the gateway sees an assistant request."""
import json
import uuid

from starlette._utils import get_route_path  # what the router itself matches on
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.services.assistant.exceptions import ERRORS, error_body

PATH_PREFIX = "/assistant"
MAX_BODY_BYTES = 256 * 1024


def single_bearer(values: list[str]) -> str | None:
    """The value of exactly one Authorization header with the Bearer scheme.

    The gateway also takes Basic and Digest, as API keys that act under the email
    of whoever created them: a key holder would own that user's chats here.
    """
    if len(values) == 1:
        scheme, _, credentials = values[0].partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return values[0]
    return None


class AssistantGuardMiddleware:
    """Pure ASGI (BaseHTTPMiddleware would buffer the chat stream), for /assistant
    only: one Bearer header, a bounded body, and a request id of our own."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_assistant_path(get_route_path(scope)):
            await self.app(scope, receive, send)
            return

        # The caller's own X-Request-ID is ignored: the request context would refuse
        # one that is not a UUID, with a body outside the contract.
        request_id = str(uuid.uuid4())
        scope["headers"] = [
            (name, value) for name, value in scope["headers"] if name != b"x-request-id"
        ] + [(b"x-request-id", request_id.encode())]

        authorizations = [
            value.decode("latin-1")
            for name, value in scope["headers"]
            if name == b"authorization"
        ]
        if single_bearer(authorizations) is None:
            await _send_error(send, "unauthenticated", request_id)
            return

        for name, value in scope["headers"]:
            if name == b"content-length" and (
                not value.isdigit() or int(value) > self.max_bytes
            ):
                await _send_error(send, "payload_too_large", request_id)
                return

        received = 0
        rejected = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    if not started:
                        await _send_error(send, "payload_too_large", request_id)
                    # The app sees a disconnect and stops reading.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if rejected:
                return  # the 413 is already on the wire
            started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)


def _is_assistant_path(path: str) -> bool:
    return path == PATH_PREFIX or path.startswith(f"{PATH_PREFIX}/")


async def _send_error(send: Send, code: str, request_id: str) -> None:
    spec = ERRORS[code]
    body = json.dumps(error_body(code, spec.message)).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"x-request-id", request_id.encode()),
    ]
    if spec.status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send(
        {"type": "http.response.start", "status": spec.status, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body})
