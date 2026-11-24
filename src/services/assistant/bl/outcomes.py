"""What became of a confirmed draft, alert by alert. Pure: no reads, no writes."""
from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from typing import get_args

from src.services.assistant.bl.projection import (
    MAX_LABEL_CHARS,
    clean_text,
    text_or_none,
)
from src.services.assistant.gateway.client import FetchedAlert, FetchResult
from src.services.assistant.models.api.draft import (
    AlertOutcome,
    AlertResult,
    ChangeAlertStatusPayload,
    DraftAlert,
    DraftOutcome,
    DraftState,
    DraftView,
    GatewayResult,
    OutcomeCode,
    TargetStatus,
)

_MESSAGES: dict[OutcomeCode, str] = {
    "applied": "All alerts were changed.",
    "partially_applied": "Some alerts were changed.",
    "nothing_to_change": "Every alert already had this status.",
    "unverified": "Keep accepted the change, but it could not be checked.",
    "outcome_pending": "Keep is still applying this change.",
    "not_permitted": "You do not have permission to change alert status.",
    "read_only": "Keep is in read-only mode.",
    "failed": "No alert was changed.",
    "interrupted": "The change was interrupted and no alert was changed.",
    "outcome_unknown": "The result of this change could not be determined.",
}
# Of `failed`, where the gateway said why.
_FAILED_MESSAGES: dict[GatewayResult, str] = {
    "rejected": "Keep refused the change.",
    "unauthenticated": "Your session expired before the change was made.",
}

# Results of a refused write: nothing was changed, so there is nothing to look up.
REFUSED: frozenset[GatewayResult] = frozenset(
    {"rejected", "read_only", "not_permitted", "unauthenticated"}
)

# (final state, outcome) of a draft; "applying" is not final.
Decision = tuple[DraftState, DraftOutcome]


def status_of(alert: FetchedAlert) -> str | None:
    status = text_or_none(alert.data.get("status"))
    return None if status is None else clean_text(status, MAX_LABEL_CHARS)


def before_write(
    payload: ChangeAlertStatusPayload, current: FetchResult
) -> dict[str, AlertOutcome]:
    """What needs no write: alerts already at the target, and alerts that are gone.
    Keyed by fingerprint; every other alert of the payload is to be written."""
    settled: dict[str, AlertOutcome] = {}
    for alert in payload.alerts:
        found = current.found.get(alert.fingerprint)
        if found is None:
            settled[alert.fingerprint] = _result(alert, "not_found")
        elif found.data.get("status") == payload.target_status:
            settled[alert.fingerprint] = _result(
                alert, "skipped", payload.target_status
            )
    return settled


def nothing_to_change(
    payload: ChangeAlertStatusPayload, settled: Mapping[str, AlertOutcome]
) -> Decision:
    return "applied", _outcome(payload, settled, {}, "nothing_to_change", None)


def refused(
    payload: ChangeAlertStatusPayload,
    settled: Mapping[str, AlertOutcome],
    result: GatewayResult,
) -> Decision:
    code = result if result in ("read_only", "not_permitted") else "failed"
    return "failed", _outcome(
        payload, settled, _rest(payload, settled, "failed"), code, result
    )


def pending(
    payload: ChangeAlertStatusPayload,
    settled: Mapping[str, AlertOutcome],
    result: GatewayResult,
) -> Decision:
    """The write may still be running."""
    return "applying", _outcome(
        payload,
        settled,
        _rest(payload, settled, "unconfirmed"),
        "outcome_pending",
        result,
        finished=False,
    )


def unverified(
    payload: ChangeAlertStatusPayload, settled: Mapping[str, AlertOutcome]
) -> Decision:
    return "applied", _outcome(
        payload, settled, _rest(payload, settled, "unconfirmed"), "unverified", "ok"
    )


def verified(
    payload: ChangeAlertStatusPayload,
    settled: Mapping[str, AlertOutcome],
    result: GatewayResult | None,
    now: FetchResult,
    *,
    after_timeout: bool = False,
) -> Decision:
    """By what the alerts look like after the write."""
    written: dict[str, AlertOutcome] = {}
    for alert in payload.alerts:
        if alert.fingerprint in settled:
            continue
        found = now.found.get(alert.fingerprint)
        if found is None:
            written[alert.fingerprint] = _result(alert, "unconfirmed")
        elif found.data.get("status") == payload.target_status:
            written[alert.fingerprint] = _result(
                alert, "applied", payload.target_status
            )
        else:
            written[alert.fingerprint] = _result(alert, "failed", status_of(found))

    results = {item.result for item in written.values()}
    state: DraftState
    code: OutcomeCode
    if results <= {"applied"}:
        state = code = "applied"
    elif "applied" in results:
        state = code = "partially_applied"
    elif "failed" in results:
        state, code = "failed", "interrupted" if after_timeout else "failed"
    elif result == "ok":
        state, code = "applied", "unverified"
    else:
        state, code = "failed", "outcome_unknown"
    return state, _outcome(payload, settled, written, code, result)


def unknown_for_good(
    payload: ChangeAlertStatusPayload, stored: DraftOutcome | None
) -> Decision:
    """Nobody has looked for too long."""
    settled = settled_before_write(stored)
    return "failed", _outcome(
        payload,
        settled,
        _rest(payload, settled, "unconfirmed"),
        "outcome_unknown",
        stored.gateway_result if stored else "unknown",
    )


def settled_before_write(outcome: DraftOutcome | None) -> dict[str, AlertOutcome]:
    """As `before_write`, from the outcome of a draft that is still being applied."""
    if outcome is None:
        return {}
    return {
        item.fingerprint: item
        for item in outcome.results
        if item.result in ("skipped", "not_found")
    }


def event_text(draft: DraftView, outcome: DraftOutcome) -> str:
    """What the model is told. No text that came from an alert or from a user: it is
    read as the system's own words."""
    return (
        f"{_subject(draft)}: {outcome.code}. {outcome.applied_count} applied, "
        f"{outcome.failed_count} failed, {outcome.unconfirmed_count} unconfirmed, "
        f"{outcome.skipped_count} skipped, {outcome.not_found_count} not found."
    )


def discarded_text(draft: DraftView) -> str:
    return f"{_subject(draft)}: discarded by the user."


def _subject(draft: DraftView) -> str:
    target = draft.payload.get("target_status")
    to = f" to {target}" if target in get_args(TargetStatus) else ""
    return f"draft {draft.id} ({draft.kind}{to})"


def _rest(
    payload: ChangeAlertStatusPayload,
    settled: Collection[str],
    result: AlertResult,
) -> dict[str, AlertOutcome]:
    return {
        alert.fingerprint: _result(alert, result)
        for alert in payload.alerts
        if alert.fingerprint not in settled
    }


def _result(
    alert: DraftAlert, result: AlertResult, status_after: str | None = None
) -> AlertOutcome:
    return AlertOutcome(
        fingerprint=alert.fingerprint,
        name=alert.name,
        result=result,
        status_after=status_after,
    )


def _outcome(
    payload: ChangeAlertStatusPayload,
    settled: Mapping[str, AlertOutcome],
    written: Mapping[str, AlertOutcome],
    code: OutcomeCode,
    result: GatewayResult | None,
    *,
    finished: bool = True,
) -> DraftOutcome:
    # The order of the card.
    by_fingerprint = {**settled, **written}
    results = [by_fingerprint[alert.fingerprint] for alert in payload.alerts]

    def count(wanted: AlertResult) -> int:
        return sum(1 for item in results if item.result == wanted)

    message = _MESSAGES[code]
    if code == "failed" and result in _FAILED_MESSAGES:
        message = _FAILED_MESSAGES[result]
    return DraftOutcome(
        code=code,
        message=message,
        gateway_result=result,
        applied_count=count("applied"),
        failed_count=count("failed"),
        unconfirmed_count=count("unconfirmed"),
        skipped_count=count("skipped"),
        not_found_count=count("not_found"),
        results=results,
        finished_at=datetime.now(timezone.utc) if finished else None,
    )
