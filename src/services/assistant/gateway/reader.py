"""The gateway as kinds see it: reads only, on behalf of one caller.

It holds the client but offers none of its writes. Confirm alone keeps the client.
"""
from collections.abc import Collection

from src.services.assistant.gateway.client import Caller, FetchResult, GatewayClient


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
