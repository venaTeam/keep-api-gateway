"""The way from the assistant to the gateway's own routes, inside this process."""
import httpx
from starlette.types import ASGIApp

# Never resolved: the transport below answers for it.
IN_PROCESS_URL = "http://gateway.in-process"

# Request extension with the (host, port) the nested request comes from.
CLIENT_EXTENSION = "assistant_client"
UNKNOWN_CLIENT = ("127.0.0.1", 0)


class InProcessTransport(httpx.AsyncBaseTransport):
    """Runs the gateway's whole app for each request: the same middleware, verifier,
    scopes and route code as when the user calls it directly.

    The scope has no lifespan `state`, so a route called this way must not need it.
    """

    def __init__(self, app: ASGIApp):
        self._app = app

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # One per request: its `client` is fixed, and the rate limiter keys on it.
        transport = httpx.ASGITransport(
            app=self._app,
            raise_app_exceptions=False,
            client=request.extensions.get(CLIENT_EXTENSION, UNKNOWN_CLIENT),
        )
        return await transport.handle_async_request(request)
