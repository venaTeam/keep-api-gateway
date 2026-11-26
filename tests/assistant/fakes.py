"""Test doubles shared by the suites."""
import json
from collections.abc import Callable

import httpx

from src.services.assistant.bl.confirm_bl import status_change
from src.services.assistant.gateway.client import Caller, GatewayClient
from src.services.assistant.models.api.draft import GatewayResult, TargetStatus
from src.services.assistant.models.identity import Identity

TOKEN = "Bearer keepActiveTenant=keep&header.payload.signature"
REQUEST_ID = "00000000-0000-4000-8000-000000000042"


def caller(request_id: str = REQUEST_ID) -> Caller:
    return Caller(authorization=TOKEN, request_id=request_id)


async def change_status(
    client: GatewayClient,
    who: Caller,
    fingerprints: list[str],
    status: TargetStatus,
    note: str | None = None,
    dispose_on_new_alert: bool = False,
    *,
    timeout: float | None = None,
) -> GatewayResult:
    """The write of a Confirm, as Confirm builds it."""
    body, query = status_change(fingerprints, status, note, dispose_on_new_alert)
    answer = await client.write(
        who, "batch_enrich", body=body, query=query, timeout=timeout
    )
    return answer.result


def identity(**fields) -> Identity:
    return Identity(
        **{
            "tenant_id": "keep",
            "email": "ops@example.com",
            "scopes": frozenset({"read:*", "write:alert"}),
            "authorization": TOKEN,
            **fields,
        }
    )


def whoami(**fields) -> dict:
    return {
        "tenant_id": "keep",
        "role": "noc",
        "email": "ops@example.com",
        "scopes": ["read:*", "write:alert"],
        **fields,
    }


def alert(fingerprint: str, **fields) -> dict:
    """An alert as the gateway renders it."""
    return {
        "id": "a-1",
        "fingerprint": fingerprint,
        "name": f"name of {fingerprint}",
        "status": "firing",
        "severity": "critical",
        "lastReceived": "2026-11-24T09:58:00.000Z",
        "source": ["prometheus"],
        "note": None,
        "incident": None,
        **fields,
    }


def query_result(*alerts: dict) -> dict:
    return {"limit": 1000, "offset": 0, "count": len(alerts), "results": list(alerts)}


class FakeGateway:
    """Records every request; answers from a handler or from set responses.

    Responses are keyed by (method, path as sent on the wire). A value is an
    httpx.Response, an exception to raise, or a callable taking the request.
    """

    def __init__(self, handler: Callable | None = None):
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple[str, str], object] = {}
        self._handler = handler

    def on(self, method: str, path: str, response) -> "FakeGateway":
        self.responses[(method, path)] = response
        return self

    def on_json(self, method: str, path: str, body, status: int = 200) -> "FakeGateway":
        return self.on(method, path, httpx.Response(status, json=body))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._handler is not None:
            return await self._handler(request)
        path = request.url.raw_path.decode().split("?")[0]
        answer = self.responses.get((request.method, path))
        if answer is None:
            return httpx.Response(404, json={"detail": "Alert not found"})
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return await answer(request)
        # A Response object can be sent once; tests may ask again.
        return httpx.Response(
            answer.status_code, headers=answer.headers, content=answer.content
        )

    def client(self) -> GatewayClient:
        return GatewayClient("http://gateway.test", httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.raw_path.decode()}" for r in self.requests]

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)
