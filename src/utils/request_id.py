from starlette_context import context
from starlette_context.header_keys import HeaderKeys


def current_request_id() -> str | None:
    """X-Request-ID of the request being handled, None outside a request.

    The request-context middleware (main.py) takes it from the caller's header, which
    must be a UUID, or generates one.
    """
    if not context.exists():
        return None
    request_id = context.get(HeaderKeys.request_id)
    return None if request_id is None else str(request_id)
