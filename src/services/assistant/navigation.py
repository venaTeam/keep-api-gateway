"""keep-ui's pages the assistant may link to, and what a keep-ui path shows.

Pure, the standard library only. This is the one module whose strings are keep-ui
paths (tests/assistant/test_parity.py pins them all); none of them is a gateway route,
and nothing here reaches the gateway. A link's path is built here from a page and its
parameter, never written by the model, and read back here before it is shown again.
keep-ui accepts exactly the same paths, pinned on both sides by the fixture's
`navigation` vectors.

A new page is one entry in PAGES, plus its vectors.
"""
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote

# What encodeURIComponent leaves as it is, besides what quote() always leaves: keep-ui
# builds and reads its links with it.
SAFE = "!'()*"
MAX_NAME_CHARS = 256
MAX_CEL_CHARS = 2000

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A value that holds an escape looks encoded twice: refused, so that no layer on the
# way can decode it into something else.
_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# "." or ".." between slashes or backslashes: a URL parser would walk up with it.
_DOT_SEGMENT = re.compile(r"(?:^|[/\\])\.\.?(?:[/\\]|$)")
# What quote(value, SAFE) gives, and nothing else: one spelling per value.
_ENCODED = re.compile(r"(?:[A-Za-z0-9_.~!'()*-]|%[0-9A-F]{2})+")


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("not a usable value")
    if _CONTROL.search(value) or _ESCAPE.search(value):
        raise ValueError("not a usable value")
    value.encode("utf-8")  # a lone surrogate: UnicodeEncodeError, a ValueError
    return value


def _name(value: object) -> str:
    """A preset's or a dashboard's name, as keep-ui puts it in a path segment."""
    name = _text(value, MAX_NAME_CHARS)
    if _DOT_SEGMENT.search(name):
        raise ValueError("not a usable value")
    return name


def _preset_name(value: object) -> str:
    """keep-ui finds a preset by its lowercased name. The feed has a page of its own."""
    name = _name(value).lower()
    if name == "feed":
        raise ValueError("not a usable value")
    return name


def _cel(value: object) -> str:
    return _text(value, MAX_CEL_CHARS)


def _uuid(value: object) -> str:
    """Canonical only: lowercase, with hyphens, nothing around it."""
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("not a usable value")
    if str(uuid.UUID(value)) != value:
        raise ValueError("not a usable value")
    return value


@dataclass(frozen=True)
class Page:
    """A keep-ui page: `path` alone, or with its parameter as the last segment (a
    `path` that ends with a slash) or as the only query value, named `query`."""

    path: str
    # keep-ui sends a NOC user anywhere outside /alerts back to the feed.
    noc_allowed: bool
    # Checks a parameter and gives it as the page reads it; ValueError when it can't.
    param: Optional[Callable[[object], str]] = None
    query: Optional[str] = None


PAGES: Mapping[str, Page] = MappingProxyType(
    {
        "alerts_feed": Page("/alerts/feed", noc_allowed=True),
        "alerts_preset": Page("/alerts/", noc_allowed=True, param=_preset_name),
        "alerts_search": Page("/alerts/feed", noc_allowed=True, param=_cel, query="cel"),
        "incidents": Page("/incidents", noc_allowed=False),
        "rules": Page("/rules", noc_allowed=False),
        "rule": Page("/rules", noc_allowed=False, param=_uuid, query="id"),
        "dashboard": Page("/dashboard/", noc_allowed=False, param=_name),
    }
)


def build(page: str, param: Optional[str] = None) -> str:
    """The path of `page` showing `param`. ValueError for a page that cannot be
    linked, and for a parameter the page does not take or would not read back the
    same (a preset name is lowercased, as keep-ui looks it up)."""
    entry = PAGES.get(page)
    if entry is None:
        raise ValueError("not a page")
    if entry.param is None:
        if param is not None:
            raise ValueError("not a usable value")
        return entry.path
    value = quote(entry.param(param), safe=SAFE)
    if entry.query is None:
        return entry.path + value
    return entry.path + "?" + entry.query + "=" + value


def parse(path: object) -> Optional[tuple[str, Optional[str]]]:
    """(page, parameter) of a path `build` gives, and None for every other path:
    another spelling, a scheme or host, a fragment, another query key, a dot segment."""
    if not isinstance(path, str):
        return None
    base, mark, query = path.partition("?")
    for name, entry in PAGES.items():
        if entry.param is None:
            if path == entry.path:
                return name, None
            continue
        if entry.query is None:
            if mark or not base.startswith(entry.path):
                continue
            raw = base[len(entry.path) :]
        else:
            key = entry.query + "="
            if not mark or base != entry.path or not query.startswith(key):
                continue
            raw = query[len(key) :]
        value = _decoded(raw)
        if value is None:
            continue
        try:
            if entry.param(value) == value:
                return name, value
        except ValueError:
            continue
    return None


def _decoded(raw: str) -> Optional[str]:
    if not _ENCODED.fullmatch(raw):
        return None
    try:
        value = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    return value if quote(value, safe=SAFE) == raw else None


def noc_allowed(page: str) -> bool:
    """Whether a NOC user may be sent to this page."""
    entry = PAGES.get(page)
    return entry is not None and entry.noc_allowed


@dataclass(frozen=True)
class PageObjects:
    """What the page a user is on shows, as their browser named it. Not checked
    against the gateway: ids and names only."""

    # Lowercased, as keep-ui finds the preset; never the feed.
    preset_name: Optional[str] = None
    # Of /incidents/<id> and the pages below it.
    incident_id: Optional[str] = None
    # Of the rule /rules?id=<id> opens.
    rule_id: Optional[str] = None
    dashboard_name: Optional[str] = None


_INCIDENT = "/incidents/"


def page_objects(path: object) -> PageObjects:
    """What a keep-ui path shows. Lenient, unlike `parse`: it reads the user's own
    address, whichever way the browser spelled it, and never raises."""
    if not isinstance(path, str):
        return PageObjects()
    base, _, query = path.partition("?")
    for prefix, field in (
        (PAGES["alerts_preset"].path, "preset_name"),
        (PAGES["dashboard"].path, "dashboard_name"),
    ):
        segment = base[len(prefix) :] if base.startswith(prefix) else None
        name = _segment_name(segment)
        if field == "preset_name" and name is not None:
            name = name.lower()
            if name == "feed":
                name = None
        if name is not None:
            return PageObjects(**{field: name})
    if base.startswith(_INCIDENT):
        incident = base[len(_INCIDENT) :].partition("/")[0]
        return PageObjects(incident_id=_uuid_or_none(incident))
    if base == PAGES["rule"].path:
        values = [v for k, v in parse_qsl(query) if k == PAGES["rule"].query]
        if len(values) == 1:
            return PageObjects(rule_id=_uuid_or_none(values[0]))
    return PageObjects()


def _segment_name(segment: Optional[str]) -> Optional[str]:
    if not segment or "/" in segment:
        return None
    try:
        name = unquote(segment, errors="strict")
    except UnicodeDecodeError:
        return None
    if not name.strip() or len(name) > MAX_NAME_CHARS or _CONTROL.search(name):
        return None
    return name


def _uuid_or_none(value: str) -> Optional[str]:
    try:
        return _uuid(value)
    except ValueError:
        return None
