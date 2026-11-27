"""The gateway as kinds and tools see it: reads only, on behalf of one caller.

It holds the client but offers none of its writes, and it has no way to reach the
client by name: fixed slots and no `__getattr__`. Confirm alone keeps the client.

A new read is a method here that calls one read method of the client, passing the
reader's `critical` on; its (method, path template) joins the pin in
tests/assistant/test_reader_facade.py.
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
    __slots__ = ("_client", "_caller", "_deadline_seconds", "_critical")

    def __init__(
        self,
        client: GatewayClient,
        caller: Caller,
        *,
        deadline_seconds: float | None = None,
        critical: bool = False,
    ):
        """`deadline_seconds`: of a read that names none. None: the client's own.
        `critical`: Confirm's and reconcile's reads, which no chat call may keep from
        a place (see the client's CALL_CLASSES)."""
        self._client = client
        self._caller = caller
        self._deadline_seconds = deadline_seconds
        self._critical = critical

    def for_request(
        self,
        request_id: uuid.UUID | str,
        *,
        deadline_seconds: float | None = None,
        critical: bool = False,
        deadline_at: float | None = None,
    ) -> "ReadGateway":
        """The same user's reads under another X-Request-ID (a turn's or a draft's id,
        which the gateway's audit trail then names). `deadline_seconds` and `critical`
        as above: neither is carried over. `deadline_at`: a time.monotonic() instant
        no read of the new reader runs past (the Caller's); one this reader has is
        carried over, and can only come sooner."""
        caller = dataclasses.replace(self._caller, request_id=str(request_id))
        if deadline_at is not None:
            if caller.deadline_at is not None:
                deadline_at = min(deadline_at, caller.deadline_at)
            caller = dataclasses.replace(caller, deadline_at=deadline_at)
        return ReadGateway(
            self._client,
            caller,
            deadline_seconds=deadline_seconds,
            critical=critical,
        )

    async def query_alerts(
        self, cel: str, limit: int, *, deadline_seconds: float | None = None
    ) -> list[FetchedAlert]:
        return await self._client.query_alerts(
            self._caller,
            cel,
            limit,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )

    async def count_alerts(
        self, cel: str, *, deadline_seconds: float | None = None
    ) -> int | None:
        return await self._client.count_alerts(
            self._caller,
            cel,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )

    async def alert_fields(
        self, *, deadline_seconds: float | None = None
    ) -> list[str]:
        return await self._client.alert_fields(
            self._caller,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )

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
            critical=self._critical,
        )
