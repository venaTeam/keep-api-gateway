"""The gateway as kinds and tools see it: reads only, on behalf of one caller.

It holds the client but offers none of its writes, and it has no way to reach the
client by name: fixed slots and no `__getattr__`. Confirm alone keeps the client.

A new read is a method here that calls one read method of the client, passing the
reader's `critical` on; its (method, path template) joins the pin in
tests/assistant/test_reader_facade.py.

What it hands on of the tenant's lists goes through `views`: no emails, no tenant.
"""
import dataclasses
import uuid
from collections.abc import Collection
from typing import Any

from src.services.assistant.gateway.client import (
    Caller,
    FetchedAlert,
    FetchResult,
    GatewayClient,
)
from src.services.assistant.gateway.views import (
    RuleRead,
    preset_view,
    rule_read,
    without_people,
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

    async def search_alerts(
        self,
        cel: str,
        limit: int,
        sort_field: str,
        sort_direction: str,
        *,
        deadline_seconds: float | None = None,
    ) -> list[FetchedAlert]:
        return await self._client.search_alerts(
            self._caller,
            cel,
            limit,
            sort_field,
            sort_direction,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )

    async def count_matching(
        self, cel: str, *, deadline_seconds: float | None = None
    ) -> int | None:
        return await self._client.count_matching(
            self._caller,
            cel,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )

    # --- the tenant's lists, and checks ----------------------------------------

    async def presets(
        self, *, deadline_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        """Each preset with `mine`; the static feed preset is one of them."""
        rows = await self._client.presets(
            self._caller,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )
        return [preset_view(row, self._caller.email) for row in rows or ()]

    async def tags(
        self, *, deadline_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._client.tags(
            self._caller,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )
        return [without_people(row) for row in rows or ()]

    async def rules(self, *, deadline_seconds: float | None = None) -> list[RuleRead]:
        """Each rule's fields with `mine`, and what an update carries. RulesUnreadable
        when the gateway cannot read one of them."""
        rows = await self._client.rules(
            self._caller,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )
        return [rule_read(row, self._caller.email) for row in rows or ()]

    async def dashboards(
        self, *, deadline_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        """Each dashboard; the presets its widgets embed lose their people too. A
        write that sends a config back sends it without them."""
        rows = await self._client.dashboards(
            self._caller,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )
        return [without_people(row) for row in rows or ()]

    async def facets(
        self, *, deadline_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._client.facets(
            self._caller,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )
        return [without_people(row) for row in rows or ()]

    async def check_cel(
        self, cel: str, cel_context: str, *, deadline_seconds: float | None = None
    ) -> dict[str, Any]:
        return await self._client.check_cel(
            self._caller,
            cel,
            cel_context,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )

    async def facet_options(
        self, cel: str, facet_id: str, *, deadline_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        return await self._client.facet_options(
            self._caller,
            cel,
            facet_id,
            deadline_seconds=(
                self._deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            critical=self._critical,
        )
