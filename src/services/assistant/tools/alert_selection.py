"""Which alerts a draft is about: those the model named by fingerprint, or those a CEL
filter matches. Every tool that drafts something for alerts selects them here.

A selection holds at most MAX_ALERTS_PER_DRAFT alerts, and only alerts that can be read
back one by one by their fingerprint: Confirm reads each of them again before it
writes. More than that, or none, is an outcome for the model, never a draft.
"""
from collections.abc import Sequence
from dataclasses import dataclass

from src.services.assistant.bl.projection import clean_text
from src.services.assistant.gateway.client import (
    FetchedAlert,
    can_use_cel,
    can_use_path,
)
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.models.api.chat import MAX_FINGERPRINT_CHARS
from src.services.assistant.models.api.draft import MAX_ALERTS_PER_DRAFT
from src.services.assistant.tools.base import (
    READ_FAILURES,
    ToolContext,
    ToolOutcome,
    read_failure_outcome,
    tool_error,
)


@dataclass(frozen=True)
class AlertSelection:
    # 1..MAX_ALERTS_PER_DRAFT, distinct fingerprints, each readable back by its
    # fingerprint, in the order found.
    alerts: tuple[FetchedAlert, ...]
    # Asked for by fingerprint, and the gateway does not have them.
    not_found: tuple[str, ...] = ()
    # Matched or asked for, but they cannot be read back one by one: not selected.
    unresolvable: tuple[str, ...] = ()

    @property
    def fingerprints(self) -> list[str]:
        return [alert.fingerprint for alert in self.alerts]

    def missing(self) -> dict[str, list[str]]:
        """What was left out, for the model: cleaned, and without empty lists."""
        return _missing(self.not_found, self.unresolvable)


async def select_alerts(
    context: ToolContext,
    reader: ReadGateway,
    *,
    fingerprints: Sequence[str] | None = None,
    cel: str | None = None,
) -> AlertSelection | ToolOutcome:
    """By exactly one of `fingerprints` or `cel`. `reader` is the tool's own, under the
    id of the draft it may make. A failed read is an outcome, never raised.

    A filter costs one unit of the turn's filter budget; fingerprints cost none.
    """
    if bool(fingerprints) == bool(cel):
        raise ValueError("select by exactly one of fingerprints or cel")
    try:
        if cel:
            return await _by_cel(context, reader, cel)
        return await _by_fingerprints(reader, list(dict.fromkeys(fingerprints)))
    except READ_FAILURES as exc:
        return read_failure_outcome(exc)


async def _by_fingerprints(
    reader: ReadGateway, fingerprints: list[str]
) -> AlertSelection | ToolOutcome:
    if len(fingerprints) > MAX_ALERTS_PER_DRAFT:
        return _too_many(len(fingerprints))
    fetched = await reader.fetch_alerts_by_fingerprints(fingerprints)
    return _selected(
        list(fetched.found.values()),
        not_found=fetched.not_found,
        unresolvable=fetched.unresolvable,
    )


async def _by_cel(
    context: ToolContext, reader: ReadGateway, cel: str
) -> AlertSelection | ToolOutcome:
    if not context.take_filter_lookup():
        return tool_error("cel_lookup_limit")
    alerts = await reader.query_alerts(cel, MAX_ALERTS_PER_DRAFT + 1)
    matched = {alert.fingerprint: alert for alert in alerts}
    if len(matched) > MAX_ALERTS_PER_DRAFT:
        count = await reader.count_alerts(cel)
        # The alerts can change between the two calls.
        exact = count is not None and count > MAX_ALERTS_PER_DRAFT
        return _too_many(count if exact else None)
    # Confirm reads each alert again: one that cannot be looked up by its
    # fingerprint could not be checked there, so it is not selected.
    readable = [fp for fp in matched if can_use_cel(fp) or can_use_path(fp)]
    return _selected(
        [matched[fp] for fp in readable],
        unresolvable=[fp for fp in matched if fp not in readable],
    )


def _selected(
    alerts: list[FetchedAlert],
    *,
    not_found: Sequence[str] = (),
    unresolvable: Sequence[str] = (),
) -> AlertSelection | ToolOutcome:
    if not alerts:
        return ToolOutcome(
            "no_matches",
            {"status": "no_matches", **_missing(not_found, unresolvable)},
        )
    return AlertSelection(tuple(alerts), tuple(not_found), tuple(unresolvable))


def _too_many(matched_count: int | None) -> ToolOutcome:
    # None: more than the limit, and the exact number could not be had.
    return ToolOutcome(
        "too_many_matches",
        {
            "status": "too_many_matches",
            "matched_count": matched_count,
            "limit": MAX_ALERTS_PER_DRAFT,
        },
    )


def _missing(
    not_found: Sequence[str], unresolvable: Sequence[str]
) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    if not_found:
        missing["not_found"] = _shown(not_found)
    if unresolvable:
        missing["unresolvable"] = _shown(unresolvable)
    return missing


def _shown(fingerprints: Sequence[str]) -> list[str]:
    # The model's own words, or text from an alert.
    return [clean_text(fp, MAX_FINGERPRINT_CHARS) for fp in fingerprints]
