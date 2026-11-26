"""What the user is looking at, read from the gateway when a turn starts.

The client sends ids only. A failure here never ends a turn: the model is told what
could not be read.
"""
import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.services.assistant.bl.projection import project_alert
from src.services.assistant.config import get_settings
from src.services.assistant.gateway.client import Caller, GatewayClient
from src.services.assistant.gateway.errors import GatewayError
from src.services.assistant.models.api.chat import ChatContext
from src.services.assistant.models.api.draft import MAX_ALERTS_PER_DRAFT
from src.services.assistant.models.identity import Identity

MAX_FIELDS = 300
MAX_FIELDS_CHARS = 6000
_FIELD_NAME = re.compile(r"[A-Za-z0-9_.\-@]{1,100}")

# keep-ui's routes. Any other segment is an id or a name the user gave something.
_PAGE_SEGMENTS = frozenset(
    "activity ai alerts builder chat dashboard deduplication executions extraction "
    "feed incidents maintenance mapping notifications-hub preview providers rules "
    "settings timeline topology workflows".split()
)
_MAX_PAGE_SEGMENTS = 6

_MAX_CACHED_TENANTS = 1000
# tenant id -> (monotonic time it ends, field names). Names only, no values.
_fields_cache: dict[str, tuple[float, list[str]]] = {}


@dataclass(frozen=True)
class TurnContext:
    now: str
    page: str | None = None
    selected_count: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    # What is missing above.
    notes: list[str] = field(default_factory=list)


async def gather(
    identity: Identity,
    gateway: GatewayClient,
    request: ChatContext,
    request_id: uuid.UUID,
) -> TurnContext:
    caller = identity.caller(request_id)
    notes: list[str] = []
    alerts, fields = await asyncio.gather(
        _selected_alerts(gateway, caller, request.selected_fingerprints, notes),
        _fields(gateway, caller, identity.tenant_id, notes),
    )
    return TurnContext(
        now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        page=page_pattern(request.page),
        selected_count=max(request.selected_count, len(request.selected_fingerprints)),
        alerts=alerts,
        fields=fields,
        notes=notes,
    )


def page_pattern(page: str) -> str | None:
    """The route, without ids, names, query or fragment."""
    segments = [s for s in re.split(r"[?#]", page)[0].split("/") if s]
    if not segments:
        return None
    return "/" + "/".join(
        segment if segment in _PAGE_SEGMENTS else ":id"
        for segment in segments[:_MAX_PAGE_SEGMENTS]
    )


async def _selected_alerts(
    gateway: GatewayClient, caller: Caller, fingerprints: list[str], notes: list[str]
) -> list[dict[str, Any]]:
    # More than a draft can hold are no use to the model.
    wanted = fingerprints[:MAX_ALERTS_PER_DRAFT]
    if not wanted:
        return []
    try:
        result = await gateway.fetch_alerts_by_fingerprints(caller, wanted)
    except GatewayError:
        notes.append("The selected alerts could not be read.")
        return []
    missing = len(wanted) - len(result.found)
    if missing:
        notes.append(f"{missing} of the selected alerts could not be found.")
    return [project_alert(alert) for alert in result.found.values()]


async def _fields(
    gateway: GatewayClient, caller: Caller, tenant_id: str, notes: list[str]
) -> list[str]:
    # By the tenant the gateway named for this token, never by one a client sent.
    until, names = _fields_cache.get(tenant_id, (0.0, []))
    if time.monotonic() < until:
        return names
    try:
        names = usable_fields(await gateway.alert_fields(caller))
    except GatewayError:
        notes.append("The other fields could not be read: use the core fields only.")
        return []
    if len(_fields_cache) >= _MAX_CACHED_TENANTS:
        _fields_cache.clear()
    _fields_cache[tenant_id] = (
        time.monotonic() + get_settings().fields_cache_ttl_seconds,
        names,
    )
    return names


def usable_fields(names: list[str]) -> list[str]:
    """Field names come from alert payloads: only ones that are plainly names."""
    usable: list[str] = []
    chars = 0
    for name in dict.fromkeys(names):
        if not _FIELD_NAME.fullmatch(name):
            continue
        chars += len(name) + 3  # its quotes and comma
        if len(usable) == MAX_FIELDS or chars > MAX_FIELDS_CHARS:
            break
        usable.append(name)
    return usable


def reset_fields_cache() -> None:
    """Forget everything (tests)."""
    _fields_cache.clear()
