"""The one tool of the MVP: draft a status change of up to 20 alerts."""
import json
import uuid
from typing import Any, List, Optional

from pydantic import (
    BaseModel,
    StrictBool,
    ValidationError,
    constr,
    root_validator,
    validator,
)

from src.services.assistant.bl.projection import (
    clean_text,
    project_alert,
    visible_text,
)
from src.services.assistant.bl.scopes import has_scope
from src.services.assistant.gateway.client import Caller, FetchedAlert, can_use_cel, can_use_path
from src.services.assistant.gateway.errors import (
    GatewayForbidden,
    GatewayUnauthorized,
    GatewayUnavailable,
    InvalidCel,
    QueryFailed,
)
from src.services.assistant.models.api.chat import MAX_FINGERPRINT_CHARS, has_control_characters, storable
from src.services.assistant.models.api.draft import (
    MAX_ALERTS_PER_DRAFT,
    CelSelector,
    ChangeAlertStatusPayload,
    DraftAlert,
    FingerprintSelector,
    SkippedAlert,
    TargetStatus,
)
from src.services.assistant.tools.base import (
    NewDraft,
    ToolContext,
    ToolOutcome,
    ToolSpec,
    tool_error,
)

NAME = "change_alert_status"
WRITE_SCOPE = "write:alert"

MAX_CEL_CHARS = 2000
MAX_NOTE_CHARS = 2000
MAX_ERRORS_SHOWN = 10
MAX_DIAGNOSTICS_SHOWN = 5
MAX_DIAGNOSTIC_CHARS = 300

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fingerprints", "cel", "status", "note", "dispose_on_new_alert"],
    "properties": {
        "fingerprints": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Fingerprints of the alerts to change, exactly as given to you. "
                "Null when cel is used. At most 20."
            ),
        },
        "cel": {
            "type": ["string", "null"],
            "description": (
                "A CEL filter selecting the alerts to change. "
                "Null when fingerprints is used."
            ),
        },
        "status": {
            "type": "string",
            "enum": ["firing", "acknowledged", "resolved", "suppressed"],
            "description": "The status the alerts should get.",
        },
        "note": {
            "type": ["string", "null"],
            "description": (
                "A note to put on the alerts, in the user's words. It replaces any "
                "existing note. Null unless the user asked for one."
            ),
        },
        "dispose_on_new_alert": {
            "type": "boolean",
            "description": (
                "True only if the user wants the change undone when the alert "
                "fires again."
            ),
        },
    },
}

PROMPT_GUIDANCE = """\
change_alert_status:
- Pass exactly one of fingerprints or cel.
- When the user says "these", "selected" or "them", pass the fingerprints of the \
selected alerts you were given. Never invent a fingerprint.
- A draft covers at most 20 alerts. If more match, tell the user how many and ask \
them to narrow it down. Never split one request into several drafts.
- Pass a note only when the user asked for one, in their words.
- The result says whether a draft was made. The user still has to confirm it on the \
card, so say that the change is drafted, not done."""


class ChangeAlertStatusArgs(BaseModel):
    fingerprints: Optional[
        List[constr(min_length=1, max_length=MAX_FINGERPRINT_CHARS)]
    ] = None
    cel: Optional[constr(strip_whitespace=True, max_length=MAX_CEL_CHARS)] = None
    status: TargetStatus
    note: Optional[constr(strip_whitespace=True, max_length=MAX_NOTE_CHARS)] = None
    dispose_on_new_alert: StrictBool = False

    class Config:
        extra = "forbid"

    @validator("fingerprints")
    def _clean_and_distinct(cls, value):
        for fingerprint in value or ():
            if has_control_characters(fingerprint):
                raise ValueError("a fingerprint has no control characters")
            storable(fingerprint)
        return list(dict.fromkeys(value or ())) or None

    @validator("cel", "note")
    def _storable_or_none(cls, value):
        return storable(value) if value else None

    @validator("note")
    def _as_it_is_read(cls, value):
        return (visible_text(value).strip() or None) if value else None

    @root_validator(skip_on_failure=True)
    def _exactly_one_selector(cls, values):
        if bool(values["fingerprints"]) == bool(values["cel"]):
            raise ValueError("pass exactly one of fingerprints or cel")
        return values


def _invalid(errors: list[dict[str, str]]) -> ToolOutcome:
    return ToolOutcome(
        "invalid_arguments", {"status": "invalid_arguments", "errors": errors}
    )


def _parse(arguments: str) -> ChangeAlertStatusArgs | ToolOutcome:
    try:
        raw = json.loads(arguments)
    except (ValueError, RecursionError):
        return _invalid([{"field": "arguments", "message": "not valid JSON"}])
    try:
        return ChangeAlertStatusArgs.parse_obj(raw)
    except ValidationError as exc:
        return _invalid(
            [
                {
                    "field": clean_text(".".join(map(str, e["loc"])), 100),
                    "message": clean_text(e["msg"], 200),
                }
                for e in exc.errors()[:MAX_ERRORS_SHOWN]
            ]
        )


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


def _invalid_cel(exc: InvalidCel) -> ToolOutcome:
    return ToolOutcome(
        "invalid_cel",
        {
            "status": "invalid_cel",
            "diagnostics": [
                clean_text(str(item.get("message", "")), MAX_DIAGNOSTIC_CHARS)
                for item in exc.diagnostics[:MAX_DIAGNOSTICS_SHOWN]
            ],
        },
    )


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = _parse(arguments)
    if isinstance(args, ToolOutcome):
        return args
    if args.fingerprints and len(args.fingerprints) > MAX_ALERTS_PER_DRAFT:
        return _too_many(len(args.fingerprints))

    # Minted before any lookup: it is also the X-Request-ID of this run's calls.
    draft_id = uuid.uuid4()
    caller = context.identity.caller(draft_id)
    try:
        if args.cel:
            return await _draft_by_cel(context, caller, draft_id, args)
        return await _draft_by_fingerprints(context, caller, draft_id, args)
    except InvalidCel as exc:
        return _invalid_cel(exc)
    except QueryFailed:
        # The expression itself can do this; the model can ask for another wording.
        return tool_error("query_failed")
    except GatewayUnauthorized:
        return ToolOutcome(
            "unauthenticated",
            {"status": "unauthenticated"},
            stream_error="unauthenticated",
        )
    except GatewayForbidden:
        return tool_error("forbidden")
    except GatewayUnavailable:
        return tool_error("gateway_unavailable", stream_error="gateway_unavailable")


async def _draft_by_cel(
    context: ToolContext,
    caller: Caller,
    draft_id: uuid.UUID,
    args: ChangeAlertStatusArgs,
) -> ToolOutcome:
    if context.cel_lookups_left <= 0:
        return tool_error("cel_lookup_limit")
    context.cel_lookups_left -= 1
    alerts = await context.gateway.query_alerts(
        caller, args.cel, MAX_ALERTS_PER_DRAFT + 1
    )
    matched = {alert.fingerprint: alert for alert in alerts}
    if len(matched) > MAX_ALERTS_PER_DRAFT:
        count = await context.gateway.count_alerts(caller, args.cel)
        # The alerts can change between the two calls.
        exact = count is not None and count > MAX_ALERTS_PER_DRAFT
        return _too_many(count if exact else None)
    # Confirm reads each alert again: one that cannot be looked up by its
    # fingerprint could not be checked there, so it is not drafted.
    readable = [fp for fp in matched if can_use_cel(fp) or can_use_path(fp)]
    return _draft(
        context,
        draft_id,
        args,
        [matched[fp] for fp in readable],
        not_found=[],
        unresolvable=[fp for fp in matched if fp not in readable],
    )


async def _draft_by_fingerprints(
    context: ToolContext,
    caller: Caller,
    draft_id: uuid.UUID,
    args: ChangeAlertStatusArgs,
) -> ToolOutcome:
    fetched = await context.gateway.fetch_alerts_by_fingerprints(
        caller, args.fingerprints
    )
    return _draft(
        context,
        draft_id,
        args,
        list(fetched.found.values()),
        not_found=fetched.not_found,
        unresolvable=fetched.unresolvable,
    )


def _draft(
    context: ToolContext,
    draft_id: uuid.UUID,
    args: ChangeAlertStatusArgs,
    matched: list[FetchedAlert],
    *,
    not_found: list[str],
    unresolvable: list[str],
) -> ToolOutcome:
    missing: dict[str, Any] = {}
    if not_found:
        missing["not_found"] = _shown(not_found)
    if unresolvable:
        missing["unresolvable"] = _shown(unresolvable)
    if not matched:
        return ToolOutcome("no_matches", {"status": "no_matches", **missing})

    to_change: list[DraftAlert] = []
    skipped: list[SkippedAlert] = []
    for alert in matched:
        view = project_alert(alert)
        if alert.data.get("status") == args.status:
            skipped.append(
                SkippedAlert(
                    fingerprint=alert.fingerprint,
                    name=view["name"],
                    status=view["status"],
                )
            )
        else:
            to_change.append(
                DraftAlert(
                    fingerprint=alert.fingerprint,
                    name=view["name"],
                    severity=view["severity"],
                    from_status=view["status"],
                    service=view["service"],
                    source=view["source"],
                    last_received=view["lastReceived"],
                    has_incident=view["has_incident"],
                    has_note=view["has_note"],
                )
            )
    if not to_change:
        return ToolOutcome(
            "nothing_to_change",
            {
                "status": "nothing_to_change",
                "already_in_status": len(skipped),
                **missing,
            },
        )

    permitted = has_scope(context.identity.scopes, WRITE_SCOPE)
    return ToolOutcome(
        "draft_created",
        {
            "status": "draft_created",
            "draft_id": str(draft_id),
            "target_status": args.status,
            "to_change_count": len(to_change),
            "to_change": [
                {
                    "fingerprint": clean_text(a.fingerprint, MAX_FINGERPRINT_CHARS),
                    "name": a.name,
                    "from_status": a.from_status,
                }
                for a in to_change
            ],
            "skipped_count": len(skipped),
            **missing,
            "user_can_apply": permitted,
            "requires_user_confirmation": True,
        },
        draft=NewDraft(
            id=draft_id,
            kind=NAME,
            permitted=permitted,
            payload=ChangeAlertStatusPayload(
                selector=(
                    CelSelector(cel=args.cel)
                    if args.cel
                    else FingerprintSelector(fingerprints=args.fingerprints)
                ),
                target_status=args.status,
                note=args.note,
                dispose_on_new_alert=args.dispose_on_new_alert,
                matched_count=len(matched),
                alerts=to_change,
                skipped=skipped,
                not_found=not_found,
                unresolvable=unresolvable,
            ),
        ),
    )


def _shown(fingerprints: list[str]) -> list[str]:
    return [clean_text(fp, MAX_FINGERPRINT_CHARS) for fp in fingerprints]


SPEC = ToolSpec(
    name=NAME,
    description=(
        "Draft a status change for up to 20 alerts. It changes nothing by itself: "
        "the user sees the draft on a card and confirms or discards it."
    ),
    parameters=PARAMETERS,
    label="Preparing a status change",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
