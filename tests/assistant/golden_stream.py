"""Builds tests/fixtures/assistant_stream.json from the real models.

keep-ui vendors that file byte for byte, so it is regenerated only together with a
contract change: `poetry run python -m tests.assistant.golden_stream`.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

from src.services.assistant import navigation as nav
from src.services.assistant.exceptions import ERRORS, VALIDATION_MESSAGES, error_body
from src.services.assistant.models.api import confirm as confirm_models
from src.services.assistant.models.api import conversation as conversation_models
from src.services.assistant.models.api import draft as draft_models
from src.services.assistant.models.api import stream as s
from src.services.assistant.models.api.chat import MAX_TIMEZONE_CHARS
from src.services.assistant.models.api.confirm import DerivedInput, SqlQueryInput
from src.services.assistant.models.api.conversation import (
    ConversationView,
    TimelineDraft,
    TimelineMessage,
)
from src.services.assistant.models.api.draft import (
    AlertOutcome,
    CelSelector,
    ChangeAlertStatusPayload,
    DraftAlert,
    DraftObject,
    DraftOutcome,
    DraftView,
    FingerprintSelector,
    SkippedAlert,
    StoredAlertOutcome,
    StoredObject,
    StoredOutcome,
)
from src.services.assistant.tools import (
    count_alerts,
    get_alerts,
    get_field_values,
    list_correlation_rules,
    list_dashboards,
    list_presets,
    list_tags,
    search_alerts,
    validate_cel,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "assistant_stream.json"

CREATED = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)
EXPIRES = CREATED + timedelta(minutes=30)
RESOLVED = CREATED + timedelta(minutes=2, microseconds=123456)

# The codes a client of the assistant API handles (spec 3.4).
CONTRACT_ERRORS = (
    "unauthenticated",
    "forbidden",
    "identity_unavailable",
    "conversation_not_found",
    "draft_not_found",
    "turn_in_progress",
    "draft_not_pending",
    "draft_expired",
    "draft_applying",
    # Without the draft: this version cannot show it as its own.
    "draft_unsupported",
    "payload_too_large",
    "rate_limited",
    "too_many_active_turns",
    "internal",
    "gateway_unavailable",
    "gateway_incompatible",
    "llm_not_configured",
)
ERRORS_WITH_DRAFT = {
    "draft_not_pending": "applied",
    "draft_expired": "expired",
    "draft_applying": "applying",
}
ERRORS_WITH_RETRY_AFTER = {"rate_limited": "30", "too_many_active_turns": "5"}


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


CONVERSATION = uid(1)

ALERTS = [
    DraftAlert(
        fingerprint="prod-db-01:cpu",
        name="High CPU on prod-db-01",
        severity="critical",
        from_status="firing",
        service="postgres",
        source="prometheus",
        last_received="2026-11-24T09:58:00.000Z",
        has_incident=True,
        has_note=True,
    ),
    DraftAlert(
        fingerprint="prod-db-02:disk",
        name="Disk almost full ‹prod-db-02› \"data\" é",
        severity="warning",
        from_status="firing",
        service="postgres",
        source="grafana",
        last_received="2026-11-24T09:59:30.000Z",
        has_incident=False,
        has_note=False,
    ),
    # Everything the gateway may leave out, left out.
    DraftAlert(
        fingerprint="5d41402abc4b2a76b9719d911017c592",
        name=None,
        severity=None,
        from_status=None,
        service=None,
        source=None,
        last_received=None,
        has_incident=None,
        has_note=False,
    ),
]

PAYLOAD = ChangeAlertStatusPayload(
    selector=FingerprintSelector(
        fingerprints=[a.fingerprint for a in ALERTS]
        + ["prod-db-03:cpu", "unnamed", "gone", "team/alert"]
    ),
    target_status="acknowledged",
    note="investigating",
    dispose_on_new_alert=False,
    matched_count=5,
    alerts=ALERTS,
    skipped=[
        SkippedAlert(
            fingerprint="prod-db-03:cpu",
            name="High CPU on prod-db-03",
            status="acknowledged",
        ),
        SkippedAlert(fingerprint="unnamed", name=None, status=None),
    ],
    not_found=["gone"],
    unresolvable=["team/alert"],
)
CEL_PAYLOAD = ChangeAlertStatusPayload(
    selector=CelSelector(cel='source.contains("prometheus") && severity == "critical"'),
    target_status="suppressed",
    note=None,
    dispose_on_new_alert=True,
    matched_count=1,
    alerts=ALERTS[:1],
    skipped=[],
    not_found=[],
    unresolvable=[],
)
RESOLVE_PAYLOAD = CEL_PAYLOAD.copy(
    update={"target_status": "resolved", "dispose_on_new_alert": False}
)
REOPEN_PAYLOAD = ChangeAlertStatusPayload(
    selector=FingerprintSelector(fingerprints=[ALERTS[0].fingerprint]),
    target_status="firing",
    note="still happening",
    dispose_on_new_alert=False,
    matched_count=1,
    alerts=[ALERTS[0].copy(update={"from_status": "resolved"})],
    skipped=[],
    not_found=[],
    unresolvable=[],
)


def draft(number: int, state: str, *, payload=PAYLOAD, permitted=True, outcome=None):
    final = state not in ("pending", "applying")
    return DraftView(
        id=uid(number),
        conversation_id=CONVERSATION,
        kind="change_alert_status",
        state=state,
        permitted=permitted,
        created_at=CREATED,
        expires_at=EXPIRES,
        resolved_at=RESOLVED if final else None,
        payload=json.loads(payload.json()),
        outcome=outcome,
    )


def outcome(code, message, gateway_result, per_alert, *, finished=True, after=None):
    """`per_alert` is one result per entry of ALERTS."""
    results = [
        AlertOutcome(
            fingerprint=alert.fingerprint,
            name=alert.name,
            result=result,
            status_after=(
                "acknowledged" if result in ("applied", "skipped") else after
            ),
        )
        for alert, result in zip(ALERTS, per_alert)
    ]

    def count(result):
        return sum(1 for r in results if r.result == result)

    return DraftOutcome(
        code=code,
        message=message,
        gateway_result=gateway_result,
        applied_count=count("applied"),
        failed_count=count("failed"),
        unconfirmed_count=count("unconfirmed"),
        skipped_count=count("skipped"),
        not_found_count=count("not_found"),
        results=results,
        finished_at=RESOLVED if finished else None,
    )


def drafts() -> dict[str, DraftView]:
    return {
        "pending": draft(10, "pending"),
        "pending_not_permitted": draft(
            11, "pending", payload=CEL_PAYLOAD, permitted=False
        ),
        "applying": draft(12, "applying"),
        "applying_outcome_pending": draft(
            13,
            "applying",
            outcome=outcome(
                "outcome_pending",
                "Keep is still applying this change.",
                "unknown",
                ["unconfirmed", "unconfirmed", "unconfirmed"],
                finished=False,
            ),
        ),
        "applied": draft(
            14,
            "applied",
            outcome=outcome(
                "applied",
                "All alerts were changed.",
                "ok",
                ["applied", "applied", "applied"],
            ),
        ),
        "applied_nothing_to_change": draft(
            15,
            "applied",
            outcome=outcome(
                "nothing_to_change",
                "Every alert already had this status.",
                None,
                ["skipped", "skipped", "not_found"],
            ),
        ),
        "applied_unverified": draft(
            16,
            "applied",
            outcome=outcome(
                "unverified",
                "Keep accepted the change, but it could not be checked.",
                "ok",
                ["unconfirmed", "unconfirmed", "unconfirmed"],
            ),
        ),
        "partially_applied": draft(
            17,
            "partially_applied",
            outcome=outcome(
                "partially_applied",
                "Some alerts were changed.",
                "failed_response",
                ["applied", "failed", "unconfirmed"],
                after="firing",
            ),
        ),
        "failed": draft(
            18,
            "failed",
            outcome=outcome(
                "failed",
                "No alert was changed.",
                "http_error",
                ["failed", "failed", "failed"],
                after="firing",
            ),
        ),
        "failed_rejected": draft(
            19,
            "failed",
            outcome=outcome(
                "failed",
                "Keep refused the change.",
                "rejected",
                ["failed", "failed", "failed"],
            ),
        ),
        "failed_rejected_maybe_written": draft(
            20,
            "failed",
            outcome=outcome(
                "failed",
                "No alert was changed.",
                "rejected_maybe_written",
                ["failed", "failed", "failed"],
                after="firing",
            ),
        ),
        "failed_not_permitted": draft(
            21,
            "failed",
            permitted=False,
            outcome=outcome(
                "not_permitted",
                "You do not have permission to change alert status.",
                "not_permitted",
                ["failed", "failed", "failed"],
            ),
        ),
        "failed_read_only": draft(
            22,
            "failed",
            outcome=outcome(
                "read_only",
                "Keep is in read-only mode.",
                "read_only",
                ["failed", "failed", "failed"],
            ),
        ),
        "failed_unauthenticated": draft(
            23,
            "failed",
            outcome=outcome(
                "failed",
                "Your session expired before the change was made.",
                "unauthenticated",
                ["failed", "failed", "failed"],
            ),
        ),
        "failed_interrupted": draft(
            24,
            "failed",
            outcome=outcome(
                "interrupted",
                "The change was interrupted and no alert was changed.",
                "unknown",
                ["failed", "failed", "unconfirmed"],
                after="firing",
            ),
        ),
        "failed_outcome_unknown": draft(
            25,
            "failed",
            outcome=outcome(
                "outcome_unknown",
                "The result of this change could not be determined.",
                "unknown",
                ["unconfirmed", "unconfirmed", "unconfirmed"],
            ),
        ),
        "discarded": draft(26, "discarded"),
        "expired": draft(27, "expired"),
        "pending_resolve": draft(28, "pending", payload=RESOLVE_PAYLOAD),
        "pending_reopen": draft(29, "pending", payload=REOPEN_PAYLOAD),
    }


PRESET = DraftObject(
    type="preset", id=uid(40), name="Night shift", path="/alerts/night-shift"
)
RULE = DraftObject(
    type="correlation_rule", id=uid(41), name="Database storms", path="/rules"
)
# Everything a client may get without, left out.
DASHBOARD = DraftObject(type="dashboard", id=uid(42), name=None, path=None)


def object_outcome(code, message, gateway_result, saved=None):
    return DraftOutcome(
        code=code,
        message=message,
        gateway_result=gateway_result,
        applied_count=0,
        failed_count=0,
        unconfirmed_count=0,
        skipped_count=0,
        not_found_count=0,
        results=[],
        finished_at=RESOLVED,
        object=saved,
    )


def outcomes() -> dict[str, DraftOutcome]:
    """Outcomes with an object of each type, and the codes and gateway results no draft
    above shows. Apart from any kind: an outcome has the same shape for all of them."""
    return {
        "preset_saved": object_outcome("applied", "The preset was saved.", "ok", PRESET),
        "correlation_rule_saved": object_outcome(
            "applied", "The correlation rule was saved.", "ok", RULE
        ),
        "dashboard_saved": object_outcome(
            "applied", "The dashboard was saved.", "ok", DASHBOARD
        ),
        "name_taken": object_outcome(
            "name_taken", "A preset with this name exists already.", None
        ),
        "object_gone": object_outcome(
            "object_gone",
            "The correlation rule does not exist any more.",
            "not_found",
            RULE.copy(update={"path": None}),
        ),
        "stale_draft": object_outcome(
            "stale_draft",
            "The dashboard changed after this draft was made.",
            None,
            DASHBOARD.copy(update={"name": "On call", "path": f"/dashboard/{uid(42)}"}),
        ),
        "invalid_filter": object_outcome(
            "invalid_filter", "Keep refused the filter of this preset.", "rejected"
        ),
        "opened_in_form": object_outcome(
            "opened_in_form", "Opened in the form, to finish there.", None
        ),
        "rules_unreadable": object_outcome(
            "rules_unreadable",
            "An existing rule's filter can't be read; fix it on the Correlations page.",
            None,
        ),
    }


# What a newer version may send that this one has no name for. A client reads them
# without knowing them: it drops an item it cannot show, and shows a generic text for a
# value it does not know.
NEWER = {
    "event": "event_of_a_newer_version",
    "item_type": "item_of_a_newer_version",
    "kind": "kind_of_a_newer_version",
    "outcome_code": "code_of_a_newer_version",
    "object_type": "object_of_a_newer_version",
    "tool_outcome": "tool_outcome_of_a_newer_version",
    "gateway_result": "result_of_a_newer_version",
    "alert_result": "alert_result_of_a_newer_version",
}


def _newer_draft(number: int, state: str, outcome=None) -> DraftView:
    final = state not in ("pending", "applying")
    return DraftView(
        id=uid(number),
        conversation_id=CONVERSATION,
        kind=NEWER["kind"],
        state=state,
        permitted=True,
        created_at=CREATED,
        expires_at=EXPIRES,
        resolved_at=RESOLVED if final else None,
        # Of a kind this version does not know, nothing in it is understood.
        payload={"version": 3, "title": "Quiet hours", "starts": "22:00"},
        outcome=outcome,
    )


def _newer_outcome() -> StoredOutcome:
    """Every value a newer version may have stored that this one does not know."""
    return StoredOutcome(
        code=NEWER["outcome_code"],
        message="The quiet hours were saved.",
        gateway_result=NEWER["gateway_result"],
        applied_count=1,
        failed_count=0,
        unconfirmed_count=0,
        skipped_count=0,
        not_found_count=0,
        results=[
            StoredAlertOutcome(
                fingerprint=ALERTS[0].fingerprint,
                name=ALERTS[0].name,
                result=NEWER["alert_result"],
                status_after="firing",
            )
        ],
        finished_at=RESOLVED,
        object=StoredObject(
            type=NEWER["object_type"], id=uid(43), name="Quiet hours", path=None
        ),
    )


def _frame(event: str, data: dict) -> dict:
    return {"event": event, "data": data}


def _raw(events: list[dict]) -> str:
    """Framed as StreamEvent.encode frames the events this version has."""
    return "".join(
        f"event: {e['event']}\ndata: {json.dumps(e['data'])}\n\n" for e in events
    )


def future() -> dict:
    """What a client must still read, from a newer version of this API: one of each
    kind of value it cannot know."""
    pending = _newer_draft(50, "pending")
    applied = _newer_draft(51, "applied", _newer_outcome())
    tool = {"tool": "save_quiet_hours", "label": "Preparing quiet hours"}
    events = [
        _frame("conversation", _conversation_event().data()),
        _frame(NEWER["event"], {"id": uid(52), "text": "Something new."}),
        _frame("tool_status", _tool("call_1", "started", **tool).data()),
        _frame("draft", s.DraftEvent(draft=pending).data()),
        _frame(
            "tool_status",
            {
                **_tool("call_1", "finished", **tool).data(),
                "outcome": NEWER["tool_outcome"],
            },
        ),
        _frame("text_delta", _text(4, "I drafted quiet hours.").data()),
        _frame("done", _done("stop", 4).data()),
    ]
    conversation = json.loads(
        ConversationView(
            id=CONVERSATION,
            title="quiet hours at night",
            created_at=CREATED,
            updated_at=RESOLVED,
            turn_in_progress=False,
            has_more=False,
            items=[
                TimelineMessage(
                    id=uid(2),
                    role="user",
                    content="quiet hours at night",
                    status="complete",
                    created_at=CREATED,
                ),
                TimelineDraft(draft=applied),
            ],
        ).json()
    )
    conversation["items"].insert(
        1,
        {
            "type": NEWER["item_type"],
            "id": uid(53),
            "created_at": CREATED.isoformat(),
            "text": "Something new.",
        },
    )
    return {
        "values": NEWER,
        "stream": {"raw": _raw(events), "events": events},
        "drafts": {
            "pending": json.loads(pending.json()),
            "applied": json.loads(applied.json()),
        },
        "conversation": conversation,
    }


def _conversation_event():
    return s.ConversationEvent(
        conversation_id=CONVERSATION, user_message_id=uid(2), turn_id=uid(3)
    )


def _text(message: int, delta: str):
    return s.TextDeltaEvent(message_id=uid(message), delta=delta)


def _tool(
    call: str,
    phase: str,
    outcome_code=None,
    *,
    tool: str = "change_alert_status",
    label: str = "Preparing a status change",
):
    return s.ToolStatusEvent(
        tool_call_id=call, tool=tool, phase=phase, label=label, outcome=outcome_code
    )


def _tool_round(call: str, outcome_code: str, **tool):
    return [_tool(call, "started", **tool), _tool(call, "finished", outcome_code, **tool)]


def _read_round(call: str, outcome_code: str, spec):
    return _tool_round(call, outcome_code, tool=spec.name, label=spec.label)


def _done(finish_reason: str, message: int | None = None):
    return s.DoneEvent(
        message_id=uid(message) if message else None, finish_reason=finish_reason
    )


HEARTBEAT = object()


def streams() -> dict[str, list]:
    all_drafts = drafts()
    result = {
        # Two model rounds, so two message ids; a heartbeat while the tool runs.
        "draft_created": [
            _conversation_event(),
            _text(4, "Let me look at "),
            _text(4, "the selected alerts."),
            _tool("call_1", "started"),
            HEARTBEAT,
            s.DraftEvent(draft=all_drafts["pending"]),
            _tool("call_1", "finished", "draft_created"),
            _text(5, "I drafted the change.\n\nConfirm it on the **card**: "),
            _text(5, "3 alerts → \"acknowledged\" ✓ \U0001f44d"),
            _done("stop", 5),
        ],
        "draft_not_permitted": [
            _conversation_event(),
            _tool("call_1", "started"),
            s.DraftEvent(draft=all_drafts["pending_not_permitted"]),
            _tool("call_1", "finished", "draft_created"),
            _done("length", 4),
        ],
        "narrowing": [
            _conversation_event(),
            *_tool_round("call_1", "invalid_cel"),
            *_tool_round("call_2", "invalid_arguments"),
            *_tool_round("call_3", "too_many_matches"),
            *_tool_round("call_4", "no_matches"),
            *_tool_round("call_5", "nothing_to_change"),
            _text(4, "More than 20 alerts match. Can you narrow it down?"),
            _done("tool_limit", 4),
        ],
        "tool_error": [
            _conversation_event(),
            *_tool_round("call_1", "error"),
            s.stream_error("gateway_unavailable"),
            _done("error"),
        ],
        "cancelled": [
            _conversation_event(),
            _text(4, "I was about to"),
            _done("cancelled", 4),
        ],
        # Tools that look things up, link to a page or save an object.
        "tool_outcomes": [
            _conversation_event(),
            *_tool_round("call_1", "found", tool="get_alerts", label="Reading alerts"),
            *_tool_round(
                "call_2", "not_found", tool="get_alerts", label="Reading alerts"
            ),
            *_tool_round(
                "call_3", "link_ready", tool="open_page", label="Preparing a link"
            ),
            *_tool_round(
                "call_4", "name_taken", tool="save_preset", label="Preparing a preset"
            ),
            _text(4, "A preset with this name exists already."),
            _done("stop", 4),
        ],
        # The tools that only read, with their own names and labels.
        "read_tools": [
            _conversation_event(),
            *_read_round("call_1", "found", get_field_values.SPEC),
            *_read_round("call_2", "invalid_cel", validate_cel.SPEC),
            *_read_round("call_3", "found", count_alerts.SPEC),
            *_read_round("call_4", "no_matches", search_alerts.SPEC),
            *_read_round("call_5", "error", search_alerts.SPEC),
            *_read_round("call_6", "found", get_alerts.SPEC),
            _text(4, "12 alerts are critical; none of them is firing."),
            _done("stop", 4),
        ],
        # The tools that list the tenant's objects; one rule it cannot read fails
        # the rules' list with the tool error rules_unreadable.
        "list_tools": [
            _conversation_event(),
            *_read_round("call_1", "found", list_presets.SPEC),
            *_read_round("call_2", "error", list_correlation_rules.SPEC),
            *_read_round("call_3", "found", list_dashboards.SPEC),
            *_read_round("call_4", "found", list_tags.SPEC),
            _text(4, "You have 3 presets; one correlation rule cannot be read."),
            _done("stop", 4),
        ],
        # The 12th call runs, the 13th of the same round does not; then no tools.
        "past_the_tool_calls": [
            _conversation_event(),
            *_tool_round(
                "call_12", "found", tool="get_alerts", label="Reading alerts"
            ),
            *_tool_round(
                "call_13", "tool_limit", tool="get_alerts", label="Reading alerts"
            ),
            _text(5, "That took too many steps. Ask me for less at a time."),
            _done("tool_limit", 5),
        ],
    }
    for code in get_args(s.StreamErrorCode):
        tool_events = (
            _tool_round("call_1", "unauthenticated") if code == "unauthenticated" else []
        )
        result[f"error_{code}"] = [
            _conversation_event(),
            *tool_events,
            s.stream_error(code),
            _done("error"),
        ]
    return result


def conversations() -> dict[str, ConversationView]:
    def message(number, role, content, status="complete"):
        return TimelineMessage(
            id=uid(number), role=role, content=content, status=status, created_at=CREATED
        )

    all_drafts = drafts()
    return {
        "finished": ConversationView(
            id=CONVERSATION,
            title="acknowledge these with the note investigating",
            created_at=CREATED,
            updated_at=RESOLVED,
            turn_in_progress=False,
            has_more=True,
            items=[
                message(2, "user", "acknowledge these with the note investigating"),
                message(4, "assistant", "Let me look at the selected alerts."),
                TimelineDraft(draft=all_drafts["applied"]),
                message(5, "assistant", "I drafted the change."),
            ],
        ),
        "in_progress": ConversationView(
            id=CONVERSATION,
            title=None,
            created_at=CREATED,
            updated_at=CREATED,
            turn_in_progress=True,
            has_more=False,
            items=[
                message(2, "user", "resolve them"),
                message(4, "assistant", "I was about to", "interrupted"),
                message(6, "user", "try again"),
                message(7, "assistant", "Looking", "error"),
                TimelineDraft(draft=all_drafts["applying_outcome_pending"]),
            ],
        ),
    }


def _encode_stream(items: list) -> dict:
    return {
        "raw": "".join(
            s.HEARTBEAT_FRAME if item is HEARTBEAT else item.encode() for item in items
        ),
        "events": [
            {"event": item.event, "data": item.data()}
            for item in items
            if item is not HEARTBEAT
        ],
    }


def _truncated(stream: dict) -> dict:
    """The same stream cut in the middle of its last frame, without a `done`."""
    frames = stream["raw"].split("\n\n")[:-1]
    cut = "\n\n".join(frames[:-1]) + "\n\n" + frames[-1][: len(frames[-1]) // 2]
    return {"raw": cut, "events": stream["events"][:-1]}


def errors() -> dict[str, dict]:
    all_drafts = drafts()
    result = {}
    for code in CONTRACT_ERRORS:
        body = error_body(code, ERRORS[code].message)
        if code in ERRORS_WITH_DRAFT:
            body["draft"] = json.loads(all_drafts[ERRORS_WITH_DRAFT[code]].json())
        headers = {}
        if code in ERRORS_WITH_RETRY_AFTER:
            headers["Retry-After"] = ERRORS_WITH_RETRY_AFTER[code]
        result[code] = {
            "status": ERRORS[code].status,
            "headers": headers,
            "body": body,
        }
    for name, (code, field) in VALIDATION_ERRORS.items():
        result[name] = {
            "status": 400,
            "headers": {},
            "body": error_body(code, VALIDATION_MESSAGES[code], field),
        }
    return result


# name -> (code, field) of the 400s a client handles by their field.
VALIDATION_ERRORS = {
    "validation": ("field_required", "message"),
    # A kind that needs what only the browser computes, Confirmed without it.
    "confirm_derived_required": ("field_required", "derived"),
    # Not what the kind takes, or `derived` sent to a kind that takes none.
    "confirm_derived_invalid": ("invalid_value", "derived"),
    # A reason the draft's kind has no use for.
    "discard_reason_invalid": ("invalid_value", "reason"),
}

# What keep-ui computes from `labels.team == "payments"` (shared/lib/cel-to-sql-query):
# the vectors of U1 give both.
DERIVED = DerivedInput(
    cel_roundtrip='labels.team == "payments"',
    sql_query=SqlQueryInput(
        sql="(labels.team = :labels.team_1)", params={"labels.team_1": "payments"}
    ),
)


def requests() -> dict:
    """The bodies of Confirm and Discard: none, or one of these."""
    return {
        "confirm": {"without_body": None, "derived": {"derived": DERIVED.dict()}},
        "discard": {
            "without_body": None,
            "opened_in_form": {"reason": "opened_in_form"},
        },
    }


def confirm_limits() -> dict:
    """Of `derived`; a Confirm past one answers 400 invalid_value on `derived`."""
    return {
        "sql_chars": confirm_models.MAX_SQL_CHARS,
        "params": confirm_models.MAX_PARAMS,
        "param_key_chars": confirm_models.MAX_PARAM_KEY_CHARS,
        "param_value_chars": confirm_models.MAX_PARAM_VALUE_CHARS,
        "cel_roundtrip_chars": confirm_models.MAX_CEL_ROUNDTRIP_CHARS,
        # JSON.stringify(derived), in UTF-8.
        "derived_bytes": confirm_models.MAX_DERIVED_BYTES,
    }


# What keep-ui may send as `context.timezone` (Intl's resolvedOptions().timeZone), and
# what the gateway keeps of it. Nothing here answers 400: a zone it cannot use is None,
# and the model is told the user's zone is not known.
TIMEZONES = [
    ("Europe/Berlin", "Europe/Berlin"),
    ("America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
    ("Asia/Kolkata", "Asia/Kolkata"),
    ("UTC", "UTC"),
    ("Mars/Olympus_Mons", None),
    ("europe/berlin", None),
    ("Europe/Berlin\n", None),
    (" Europe/Berlin", None),
    ("Europe/" + "B" * (MAX_TIMEZONE_CHARS - 6), None),
    ("", None),
    (None, None),
    (60, None),
]


def chat_context() -> dict:
    """Context keys keep-ui computes, and what the gateway keeps of each value."""
    return {
        "timezone_chars": MAX_TIMEZONE_CHARS,
        "timezone": [{"sent": sent, "kept": kept} for sent, kept in TIMEZONES],
    }


NAV_ID = "6f1c2b9e-3d4a-4c5b-8e7f-0a1b2c3d4e5f"

# The pages a link may open (navigation.py, keep-ui's navigation.ts). Every path is
# written out here, never built: each side must reach it on its own, with
# encodeURIComponent's encoding. A vector is (page, param, role, path, accepted,
# noc_allowed):
# - a path of a page: `build(page, param)` gives it and `parse` gives back (page, param);
#   `accepted` is for a user of `role`; `noc_allowed` is the page's;
# - any other path: page, param and noc_allowed are None and nobody accepts it.
NAVIGATION = [
    ("alerts_feed", None, "admin", "/alerts/feed", True, True),
    ("alerts_feed", None, "noc", "/alerts/feed", True, True),
    ("alerts_preset", "payments", "admin", "/alerts/payments", True, True),
    ("alerts_preset", "prod (eu)!", "noc", "/alerts/prod%20(eu)!", True, True),
    ("alerts_preset", "it's * ~ 100%", "admin", "/alerts/it's%20*%20~%20100%25", True, True),
    ("alerts_preset", "db/payments", "admin", "/alerts/db%2Fpayments", True, True),
    ("alerts_preset", "équipe nuit", "admin", "/alerts/%C3%A9quipe%20nuit", True, True),
    ("alerts_preset", "日本の警報", "admin",
     "/alerts/%E6%97%A5%E6%9C%AC%E3%81%AE%E8%AD%A6%E5%A0%B1", True, True),
    ("alerts_preset", "\U0001f525 sev1", "admin", "/alerts/%F0%9F%94%A5%20sev1", True, True),
    ("alerts_preset", "a+b=c&d", "admin", "/alerts/a%2Bb%3Dc%26d", True, True),
    ("alerts_preset", "v1.2...", "admin", "/alerts/v1.2...", True, True),
    ("alerts_search", 'name == "db"', "noc",
     "/alerts/feed?cel=name%20%3D%3D%20%22db%22", True, True),
    ("alerts_search", '!(status == "resolved") && count > 5', "admin",
     "/alerts/feed?cel=!(status%20%3D%3D%20%22resolved%22)%20%26%26%20count%20%3E%205",
     True, True),
    ("alerts_search", "service.startsWith('pay')", "admin",
     "/alerts/feed?cel=service.startsWith('pay')", True, True),
    ("incidents", None, "admin", "/incidents", True, False),
    ("rules", None, "admin", "/rules", True, False),
    ("rule", NAV_ID, "admin", f"/rules?id={NAV_ID}", True, False),
    ("dashboard", "Ops Overview", "admin", "/dashboard/Ops%20Overview", True, False),
    ("dashboard", "SRE: 50% (prod)", "editor",
     "/dashboard/SRE%3A%2050%25%20(prod)", True, False),
    # A NOC user is sent back to the feed from anywhere outside /alerts.
    ("incidents", None, "noc", "/incidents", False, False),
    ("rules", None, "noc", "/rules", False, False),
    ("rule", NAV_ID, "noc", f"/rules?id={NAV_ID}", False, False),
    ("dashboard", "Ops Overview", "noc", "/dashboard/Ops%20Overview", False, False),
    # Elsewhere, or not spelled the one way a page is.
    *(
        (None, None, "admin", path, False, None)
        for path in [
            "",
            "alerts/feed",
            "//evil.example",
            "//evil.example/alerts/feed",
            "\\\\evil.example",
            "/\\evil.example",
            "javascript:alert(1)",
            "https://evil.example/alerts/feed",
            "/alerts/%2F..",
            "/alerts/..",
            "/alerts/.",
            "/alerts/%2e%2e",
            "/alerts/%2E%2E",
            "/alerts/my%2520preset",
            "/alerts/feed?cel=name%20%3D%3D%20%22a%2520b%22",
            "/alerts/Payments",
            "/alerts/a%2fb",
            "/alerts/%61bc",
            "/alerts/my preset",
            "/alerts/café",
            "/alerts/caf%C3",
            "/alerts/a%00b",
            "/alerts/a/b",
            "/alerts/",
            "/alerts",
            "/alerts/feed/",
            "/alerts/feed#top",
            "/alerts/feed?cel=",
            "/alerts/feed?cel=%20",
            "/alerts/feed?cel=name%20%3D%3D%20%22a%22%0A",
            "/alerts/feed?cel=name\n",
            "/alerts/feed?cel=a%20b&x=1",
            "/alerts/feed?x=1",
            "/alerts/feed?cel=a?b",
            f"/rules?id={NAV_ID.upper()}",
            f"/rules?id={NAV_ID.replace('-', '')}",
            f"/rules?id=%7B{NAV_ID}%7D",
            "/rules?id=not-a-uuid",
            f"/rules?id={NAV_ID}&x=1",
            "/rules?id=",
            "/rules/",
            "/incidents?x=1",
            f"/incidents/{NAV_ID}",
            "/dashboard/",
            "/dashboard/..",
            "/workflows",
            "/providers",
            "/settings",
            "/deduplication",
            "/extraction",
            "/mapping",
            "/topology",
        ]
    ),
]  # fmt: skip

# What a link is built from, when the page reads it another way: a preset is found by
# its lowercased name.
NAVIGATION_BUILT = [
    ("alerts_preset", "Payments", "/alerts/payments"),
    ("alerts_preset", "Prod (EU)!", "/alerts/prod%20(eu)!"),
    ("alerts_preset", "Équipe Nuit", "/alerts/%C3%A9quipe%20nuit"),
]

# What no link is built from.
NAVIGATION_REFUSED = [
    ("alerts_feed", "payments"),
    ("incidents", "x"),
    ("rules", NAV_ID),
    ("alerts_preset", None),
    ("alerts_preset", ""),
    ("alerts_preset", "   "),
    ("alerts_preset", "feed"),
    ("alerts_preset", "Feed"),
    ("alerts_preset", "."),
    ("alerts_preset", ".."),
    ("alerts_preset", "/.."),
    ("alerts_preset", "a/../b"),
    ("alerts_preset", "a\\..\\b"),
    ("alerts_preset", "my%20preset"),
    ("alerts_preset", "a\nb"),
    ("alerts_preset", "x" * 257),
    ("alerts_search", None),
    ("alerts_search", ""),
    ("alerts_search", "name ==\t1"),
    ("alerts_search", 'name == "a%20b"'),
    ("alerts_search", "x" * 2001),
    ("rule", None),
    ("rule", NAV_ID.upper()),
    ("rule", "not-a-uuid"),
    ("dashboard", ""),
    ("dashboard", ".."),
    ("workflows", None),
    ("incident", NAV_ID),
]


def navigation() -> dict:
    keys = ("page", "param", "role", "path", "accepted", "noc_allowed")
    return {
        "pages": list(nav.PAGES),
        "name_chars": nav.MAX_NAME_CHARS,
        "cel_chars": nav.MAX_CEL_CHARS,
        "vectors": [dict(zip(keys, vector)) for vector in NAVIGATION],
        "built": [
            {"page": page, "param": param, "path": path}
            for page, param, path in NAVIGATION_BUILT
        ],
        "refused": [{"page": page, "param": param} for page, param in NAVIGATION_REFUSED],
    }


def build() -> dict:
    encoded = {name: _encode_stream(items) for name, items in streams().items()}
    encoded["truncated"] = _truncated(encoded["draft_created"])
    return {
        "vocabulary": {
            "events": [event.event for event in s.STREAM_EVENTS],
            "tool_phases": list(get_args(s.ToolPhase)),
            "tool_outcomes": list(get_args(s.ToolOutcomeCode)),
            "stream_error_codes": list(get_args(s.StreamErrorCode)),
            "finish_reasons": list(get_args(s.FinishReason)),
            "draft_states": list(get_args(draft_models.DraftState)),
            "target_statuses": list(get_args(draft_models.TargetStatus)),
            "outcome_codes": list(get_args(draft_models.OutcomeCode)),
            "gateway_results": list(get_args(draft_models.GatewayResult)),
            "alert_results": list(get_args(draft_models.AlertResult)),
            "object_types": list(get_args(draft_models.ObjectType)),
            "message_statuses": list(get_args(conversation_models.MessageStatus)),
            "discard_reasons": list(get_args(confirm_models.DiscardReason)),
        },
        "heartbeat": s.HEARTBEAT_FRAME,
        "response_headers": {"Content-Type": s.STREAM_MEDIA_TYPE, **s.STREAM_HEADERS},
        "streams": encoded,
        "drafts": {name: json.loads(d.json()) for name, d in drafts().items()},
        "outcomes": {name: json.loads(o.json()) for name, o in outcomes().items()},
        "conversations": {
            name: json.loads(c.json()) for name, c in conversations().items()
        },
        "errors": errors(),
        "requests": requests(),
        "confirm_limits": confirm_limits(),
        "chat_context": chat_context(),
        "navigation": navigation(),
        "future": future(),
    }


def render() -> bytes:
    # ASCII and LF only, so the bytes are the same on every platform and checkout.
    return (json.dumps(build(), indent=2, ensure_ascii=True) + "\n").encode("ascii")


if __name__ == "__main__":
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_bytes(render())
    print(f"wrote {FIXTURE}")
