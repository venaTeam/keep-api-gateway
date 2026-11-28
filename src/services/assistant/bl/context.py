"""What the user is looking at, read from the gateway when a turn starts.

The client sends ids only. A failure here never ends a turn: the model is told what
could not be read.
"""
import asyncio
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from src.services.assistant import navigation
from src.services.assistant.bl.projection import project_alert, readable_text
from src.services.assistant.bl.scopes import has_scope
from src.services.assistant.config import get_settings
from src.services.assistant.gateway.errors import GatewayError
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.kinds.registry import KINDS, disabled_kinds
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
# The pages under this segment have the search box `search_cel` comes from.
_ALERTS_SEGMENT = "alerts"
# Of the filter of the preset on the user's page.
MAX_ACTIVE_PRESET_CEL_CHARS = 500
MAX_ACTIVE_PRESET_NAME_CHARS = 256

# A read of the turn, made at most once: ToolContext.read_once.
ReadOnce = Callable[..., Awaitable[Any]]

_MAX_CACHED_TENANTS = 1000
# tenant id -> (monotonic time it ends, field names). Names only, no values.
_fields_cache: dict[str, tuple[float, list[str]]] = {}
# tenant id -> (monotonic time it ends, facet id by field name). Ids only, no values.
_facets_cache: dict[str, tuple[float, dict[str, str]]] = {}


UTC_NAME = "UTC"


@dataclass(frozen=True)
class TurnContext:
    now: str
    # The user's zone, by its IANA name: UTC, and not known, when their browser named
    # none this server can use.
    timezone: str = UTC_NAME
    timezone_known: bool = False
    page: str | None = None
    selected_count: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    # The preset of the alerts page the user is on: {id, name, cel}.
    active_preset: dict[str, Any] | None = None
    # The search box's CEL of the alerts page the user is on.
    search_cel: str | None = None
    # The incident whose page the user is on: a canonical UUID, never checked.
    current_incident_id: str | None = None
    # The draft kinds the user's scopes allow in any of their modes, but those
    # switched off: a hint, the gateway decides on Confirm.
    allowed_drafts: list[str] = field(default_factory=list)
    # What is missing above.
    notes: list[str] = field(default_factory=list)


async def gather(
    identity: Identity,
    gateway: ReadGateway,
    request: ChatContext,
    request_id: uuid.UUID,
    *,
    now: datetime | None = None,
    read_once: ReadOnce | None = None,
) -> TurnContext:
    """`now`: the turn's instant, which its tools get too (ToolContext.now); without
    it, the current second. `read_once`: the turn's memo (ToolContext.read_once), so
    that a tool listing the same objects later gets them for nothing; without it, each
    read is made here."""
    reader = gateway.for_request(request_id)
    notes: list[str] = []
    shown = navigation.page_objects(request.page)
    alerts, fields, active_preset = await asyncio.gather(
        _selected_alerts(reader, request.selected_fingerprints, notes),
        _fields(reader, identity.tenant_id, notes),
        _active_preset(reader, shown.preset_name, read_once, notes),
    )
    if now is None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
    zone, zone_name = user_zone(request.timezone)
    page = page_pattern(request.page)
    return TurnContext(
        now=now.isoformat(timespec="seconds"),
        timezone=zone_name,
        timezone_known=isinstance(zone, ZoneInfo),
        page=page,
        selected_count=max(request.selected_count, len(request.selected_fingerprints)),
        alerts=alerts,
        fields=fields,
        active_preset=active_preset,
        search_cel=_search_cel(request.search_cel, page, notes),
        current_incident_id=shown.incident_id,
        allowed_drafts=allowed_drafts(identity.scopes),
        notes=notes,
    )


def allowed_drafts(scopes: Collection[str] | None) -> list[str]:
    """The draft kinds of which the scopes allow some write (C46), but those switched
    off. None as scopes: the gateway did not name them, which refuses nothing."""
    off = disabled_kinds()
    return [
        name
        for name, kind in KINDS.items()
        if name not in off
        and any(has_scope(scopes, scope) for scope in sorted(kind.scope_hint))
    ]


def _search_cel(value: str, page: str | None, notes: list[str]) -> str | None:
    """The search box's CEL, on an alerts page only. One with characters that show
    nothing or reorder the text around them is dropped, and the model is told: it never
    gets a filter that reads otherwise than the user sees it."""
    if not value.strip() or page is None or page.split("/")[1] != _ALERTS_SEGMENT:
        return None
    if any(unicodedata.category(char) == "Cf" for char in value):
        notes.append(
            "The search box holds a filter with invisible characters: it is left out."
        )
        return None
    return value


async def _active_preset(
    reader: ReadGateway,
    name: str | None,
    read_once: ReadOnce | None,
    notes: list[str],
) -> dict[str, Any] | None:
    """The preset of the page, found by its lowercased name as keep-ui finds it."""
    if name is None:
        return None
    try:
        presets = await (read_once(reader.presets) if read_once else reader.presets())
    except GatewayError:
        notes.append("The preset of the page could not be read.")
        return None
    for preset in presets:
        preset_name = preset.get("name")
        if isinstance(preset_name, str) and preset_name.lower() == name:
            return {
                "id": preset.get("id"),
                "name": readable_text(preset_name, MAX_ACTIVE_PRESET_NAME_CHARS),
                "cel": _preset_cel(preset),
            }
    notes.append("The preset of the page was not found.")
    return None


def _preset_cel(preset: dict[str, Any]) -> str | None:
    for option in preset.get("options") or ():
        if isinstance(option, dict) and option.get("label") == "CEL":
            value = option.get("value")
            if isinstance(value, str):
                return readable_text(value, MAX_ACTIVE_PRESET_CEL_CHARS)
    return None


def user_zone(name: str | None) -> tuple[tzinfo, str]:
    """The zone of a ChatContext.timezone, and its name: datetime's own UTC when there
    is none, or when its data cannot be read after all."""
    if name:
        try:
            return ZoneInfo(name), name
        except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
            pass
    return timezone.utc, UTC_NAME


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
    reader: ReadGateway, fingerprints: list[str], notes: list[str]
) -> list[dict[str, Any]]:
    # More than a draft can hold are no use to the model.
    wanted = fingerprints[:MAX_ALERTS_PER_DRAFT]
    if not wanted:
        return []
    try:
        result = await reader.fetch_alerts_by_fingerprints(wanted)
    except GatewayError:
        notes.append("The selected alerts could not be read.")
        return []
    missing = len(wanted) - len(result.found)
    if missing:
        notes.append(f"{missing} of the selected alerts could not be found.")
    return [project_alert(alert) for alert in result.found.values()]


async def _fields(
    reader: ReadGateway, tenant_id: str, notes: list[str]
) -> list[str]:
    try:
        return await available_fields(reader, tenant_id)
    except GatewayError:
        notes.append("The other fields could not be read: use the core fields only.")
        return []


async def available_fields(reader: ReadGateway, tenant_id: str) -> list[str]:
    """The alert field names the context message shows, cached per tenant. Raises the
    read's GatewayError, which is not cached."""
    # By the tenant the gateway named for this token, never by one a client sent.
    until, names = _fields_cache.get(tenant_id, (0.0, []))
    if time.monotonic() < until:
        return names
    names = usable_fields(await reader.alert_fields())
    _remember(_fields_cache, tenant_id, names)
    return names


async def facet_ids(reader: ReadGateway, tenant_id: str) -> dict[str, str]:
    """The id of each of the alerts table's facets, by the field it counts, cached
    per tenant like the field names. Raises the read's GatewayError, which is not
    cached."""
    until, ids = _facets_cache.get(tenant_id, (0.0, {}))
    if time.monotonic() < until:
        return ids
    ids = {}
    for facet in await reader.facets():
        path, facet_id = facet.get("property_path"), facet.get("id")
        # Only an id the gateway can take: it answers any other with a 500.
        if isinstance(path, str) and _is_canonical_uuid(facet_id):
            ids.setdefault(path, facet_id)
    _remember(_facets_cache, tenant_id, ids)
    return ids


def _is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def drop_facet_ids(tenant_id: str) -> None:
    """Forget a tenant's facet ids: the gateway refused a read with one of them."""
    _facets_cache.pop(tenant_id, None)


def _remember(cache: dict[str, tuple[float, Any]], tenant_id: str, value: Any) -> None:
    # At most this many tenants: past it, all of them are read again.
    if tenant_id not in cache and len(cache) >= _MAX_CACHED_TENANTS:
        cache.clear()
    cache[tenant_id] = (
        time.monotonic() + get_settings().fields_cache_ttl_seconds,
        value,
    )


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


def reset_facets_cache() -> None:
    """Forget every tenant's facet ids (tests)."""
    _facets_cache.clear()
