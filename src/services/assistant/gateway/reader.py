"""The gateway as kinds and tools see it: reads only, on behalf of one caller.

It holds the client but offers none of its writes, and it has no way to reach the
client by name: fixed slots and no `__getattr__`. Confirm alone keeps the client.

A new read is a method here that calls one read method of the client; its (method,
path template) joins the pin in tests/assistant/test_reader_facade.py.
"""
import dataclasses
import uuid
from collections.abc import Collection

from src.services.assistant.gateway.client import (
    Caller,
    FetchedAlert,
    FetchResult,
    GatewayClient,
)


class ReadGateway:
    __slots__ = ("_client", "_caller", "_deadline_seconds")

    def __init__(
        self,
        client: GatewayClient,
        caller: Caller,
        *,
        deadline_seconds: float | None = None,
    ):
        """`deadline_seconds`: of a read that names none. None: the client's own."""
        self._client = client
        self._caller = caller
        self._deadline_seconds = deadline_seconds

    def for_request(
        self, request_id: uuid.UUID | str, *, deadline_seconds: float | None = None
    ) -> "ReadGateway":
        """The same user's reads under another X-Request-ID (a turn's or a draft's id,
        which the gateway's audit trail then names). `deadline_seconds` as above."""
        return ReadGateway(
            self._client,
            dataclasses.replace(self._caller, request_id=str(request_id)),
            deadline_seconds=deadline_seconds,
        )

    async def query_alerts(self, cel: str, limit: int) -> list[FetchedAlert]:
        return await self._client.query_alerts(self._caller, cel, limit)

    async def count_alerts(self, cel: str) -> int | None:
        return await self._client.count_alerts(self._caller, cel)

    async def alert_fields(self) -> list[str]:
        return await self._client.alert_fields(self._caller)

    async def fetch_alerts_by_fingerprints(
        self,
        fingerprints: Collection[str],
        *,
        deadline_seconds: float | None = None,
        known_missing: Collection[str] = (),
    ) -> FetchResult:
        return await self._client.fetch_alerts_by_fingerprints(
            self._caller,
            fingerprints,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            known_missing=known_missing,
        )
