"""A status change of up to 20 alerts: the kind of `change_alert_status` drafts.

One write, the alerts table's own `batch_enrich`, of the alerts that are not already
at the target. Looks before and after it decide what the card says.
"""
from collections.abc import Mapping, Sequence
from typing import Any, Optional, get_args

from pydantic import BaseModel

from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.kinds import alert_set
from src.services.assistant.kinds.base import (
    ConfirmContext,
    ConfirmPlan,
    Decision,
    DraftKind,
    Sent,
    Step,
    WriteRequest,
)
from src.services.assistant.models.api.draft import (
    AlertOutcome,
    ChangeAlertStatusPayload,
    DraftOutcome,
    DraftView,
    StoredOutcome,
    TargetStatus,
)

NAME = "change_alert_status"
WRITE_SCOPE = "write:alert"
ENDPOINT = "batch_enrich"


def status_change(
    fingerprints: list[str],
    status: TargetStatus,
    note: str | None,
    dispose_on_new_alert: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The body and query of the `batch_enrich` write: what the UI sends for the same
    change."""
    enrichments: dict[str, Any] = {"status": status}
    if status != "suppressed":
        enrichments["dismiss_mode"] = None
        enrichments["dismissed_until"] = None
    if note:
        enrichments["note"] = note
    return (
        {"fingerprints": fingerprints, "enrichments": enrichments},
        {"dispose_on_new_alert": "true" if dispose_on_new_alert else "false"},
    )


class _Confirm:
    """One Confirm of one draft."""

    def __init__(self, ctx: ConfirmContext):
        self._ctx = ctx
        self._payload: ChangeAlertStatusPayload = ctx.payload  # type: ignore[assignment]
        self._settled: dict[str, AlertOutcome] = {}
        self._to_write: list[str] = []

    def plan(self) -> ConfirmPlan:
        return ConfirmPlan(
            rows=(ENDPOINT,),
            first=self.first,
            after=self.after,
            pending=self.pending,
            stopped=self.stopped,
        )

    async def first(self) -> Step:
        payload = self._payload
        current = await alert_set.look_before(self._ctx, payload)
        self._settled = alert_set.before_write(payload, current)
        self._to_write = [
            alert.fingerprint
            for alert in payload.alerts
            if alert.fingerprint not in self._settled
        ]
        if not self._to_write:
            return alert_set.nothing_to_change(payload, self._settled)
        body, query = status_change(
            self._to_write,
            payload.target_status,
            payload.note,
            payload.dispose_on_new_alert,
        )
        return WriteRequest(ENDPOINT, body, query=query)

    async def after(self, sent: Sequence[Sent]) -> Step:
        payload = self._payload
        result = sent[-1].answer.result
        if result in alert_set.REFUSED:
            return alert_set.refused(payload, self._settled, result)
        now = await alert_set.look_after(self._ctx, self._to_write)
        if now is not None:
            return alert_set.verified(payload, self._settled, result, now)
        if result == "ok":
            return alert_set.unverified(payload, self._settled)
        return alert_set.pending(payload, self._settled, result)

    def pending(self, sent: Sequence[Sent]) -> DraftOutcome:
        _, outcome = alert_set.pending(
            self._payload, self._settled, sent[-1].answer.result
        )
        return outcome

    def stopped(self, sent: Sequence[Sent]) -> Decision:
        # One write: nothing comes after it that could be stopped.
        raise RuntimeError("a plan of one write has nothing to stop")


class AlertStatusKind(DraftKind):
    name = NAME
    payload_model = ChangeAlertStatusPayload
    scope_hint = frozenset({WRITE_SCOPE})

    def write_scope(self, payload: BaseModel) -> str:
        return WRITE_SCOPE

    def plan(self, ctx: ConfirmContext) -> ConfirmPlan:
        return _Confirm(ctx).plan()

    async def reconcile(
        self,
        reader: ReadGateway,
        view: DraftView,
        confirm_state: Optional[Mapping[str, Any]],
    ) -> Optional[Decision]:
        payload = ChangeAlertStatusPayload.parse_obj(view.payload)
        before_write = alert_set.settled_before_write(view.outcome)
        now = await reader.fetch_alerts_by_fingerprints(
            [
                alert.fingerprint
                for alert in payload.alerts
                if alert.fingerprint not in before_write
            ]
        )
        return alert_set.verified(
            payload,
            before_write,
            # Without an outcome the worker ended in the middle of it.
            view.outcome.gateway_result if view.outcome else "unknown",  # type: ignore[arg-type]
            now,
            after_timeout=True,
        )

    def close_unknown(
        self,
        payload: BaseModel,
        stored: Optional[StoredOutcome],
        confirm_state: Optional[Mapping[str, Any]],
    ) -> Decision:
        return alert_set.unknown_for_good(payload, stored)  # type: ignore[arg-type]

    def event_text(self, draft: DraftView, outcome: DraftOutcome) -> str:
        return f"{_subject(draft)}: {outcome.code}. {alert_set.counts_text(outcome)}"

    def discarded_text(self, draft: DraftView, reason: Optional[str]) -> str:
        return f"{_subject(draft)}: discarded by the user."


def _subject(draft: DraftView) -> str:
    # Read from the stored payload as it is: it may not parse.
    target = draft.payload.get("target_status")
    to = f" to {target}" if target in get_args(TargetStatus) else ""
    return f"draft {draft.id} ({draft.kind}{to})"


KIND = AlertStatusKind()
