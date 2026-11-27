"""Drafts: the only thing the model can make, and only the user can confirm.

A draft changes its state in one conditional UPDATE, whoever does it: of two that race,
one finds no row. Locks are always taken in the order conversation, draft, message.

The stored outcome may hold Confirm's raw `confirm_state` (kinds/base.py), which no
view shows. Each write here says what becomes of it: claim (none), checkpoint (set),
keep_applying and resolve (keep), revert (clear), the sweep's close (keep, by its
merge), discard (none: a pending draft has none).
"""
import json
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session

from src.services.assistant.bl import outcomes
from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.core.db import get_session
from src.services.assistant.exceptions import AssistantError, validation_error
from src.services.assistant.kinds.base import MAX_CONFIRM_STATE_BYTES
from src.services.assistant.kinds.registry import kind_of
from src.services.assistant.models.api.confirm import DiscardReason, DiscardRequest
from src.services.assistant.models.api.draft import DraftOutcome, DraftState, DraftView
from src.services.assistant.models.db import Conversation, Draft, Message
from src.services.assistant.models.identity import Identity
from src.services.assistant.tools.base import NewDraft


def insert_draft(
    session: Session,
    *,
    tenant_id: str,
    created_by: str,
    conversation_id: uuid.UUID,
    draft: NewDraft,
) -> Draft:
    """Part of the caller's transaction. Expiry is the database's clock."""
    return session.scalars(
        sa.insert(Draft)
        .values(
            id=draft.id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            created_by=created_by,
            kind=draft.kind,
            permitted=draft.permitted,
            payload=json.loads(draft.payload.json()),
            expires_at=sa.func.now()
            + timedelta(seconds=get_settings().draft_ttl_seconds),
        )
        .returning(Draft)
    ).one()


CONFIRM_STATE = "confirm_state"
ConfirmState = dict[str, Any]


def to_view(draft: Draft) -> DraftView:
    return DraftView(
        id=draft.id,
        conversation_id=draft.conversation_id,
        kind=draft.kind,
        state=draft.state,
        permitted=draft.permitted,
        created_at=draft.created_at,
        expires_at=draft.expires_at,
        resolved_at=draft.resolved_at,
        payload=draft.payload,
        outcome=_shown(draft.outcome),
    )


def _shown(stored: Any) -> Any:
    """The outcome a view shows: never `confirm_state`, and none while that is all
    there is (the checkpoint before the first write)."""
    if stored is None:
        return None
    if isinstance(stored, dict):
        stored = {key: value for key, value in stored.items() if key != CONFIRM_STATE}
        if not stored:
            return None
    return outcomes.readable(stored)


def confirm_state_of(draft: Draft) -> Optional[ConfirmState]:
    """The raw `confirm_state` of a stored outcome; None without one."""
    return state_or_none(
        draft.outcome.get(CONFIRM_STATE) if isinstance(draft.outcome, dict) else None
    )


def state_or_none(value: Any) -> Optional[ConfirmState]:
    return value if isinstance(value, dict) else None


def _strict(outcome: DraftOutcome) -> dict[str, Any]:
    """What is stored, checked again: only values this version knows are written.
    Without an object there is no `object` key, so a merge keeps a stored one."""
    value = json.loads(DraftOutcome(**outcome.dict()).json())
    if value["object"] is None:
        del value["object"]
    return value


@dataclass(frozen=True)
class DraftKey:
    """One draft of one user: every statement below is scoped by all of it."""

    id: uuid.UUID
    tenant_id: str
    created_by: str
    conversation_id: uuid.UUID

    def matches(self) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            Draft.id == self.id,
            Draft.tenant_id == self.tenant_id,
            Draft.created_by == self.created_by,
        )


def key_of(identity: Identity, draft: DraftView) -> DraftKey:
    return DraftKey(draft.id, identity.tenant_id, identity.email, draft.conversation_id)


def owned(identity: Identity) -> tuple[sa.ColumnElement[bool], ...]:
    return Draft.tenant_id == identity.tenant_id, Draft.created_by == identity.email


def claim(identity: Identity, draft_id: uuid.UUID) -> DraftView:
    """pending -> applying, for exactly one caller.

    Raises draft_not_found, or draft_applying, draft_expired or draft_not_pending with
    the draft as it is.
    """
    mine = (Draft.id == draft_id, *owned(identity))
    with get_session() as session, session.begin():
        claimed = _change(
            session,
            *mine,
            Draft.state == "pending",
            Draft.expires_at > sa.func.now(),
            state="applying",
        )
        if claimed is not None:
            return to_view(claimed)
        expired = _change(
            session,
            *mine,
            Draft.state == "pending",
            Draft.expires_at <= sa.func.now(),
            state="expired",
            resolved_at=sa.func.now(),
        )
        draft = expired or session.scalar(sa.select(Draft).where(*mine))
        if draft is None:
            raise AssistantError("draft_not_found")
        view = to_view(draft)
    if expired is not None:
        metrics.drafts_total.labels(kind=view.kind, event="expired").inc()
    raise conflict(view)


def conflict(draft: DraftView) -> AssistantError:
    code = {
        "applying": "draft_applying",
        "expired": "draft_expired",
        # It was being applied a moment ago, and was put back since.
        "pending": "draft_applying",
    }.get(draft.state, "draft_not_pending")
    return AssistantError(code, extra={"draft": json.loads(draft.json())})


def revert(key: DraftKey) -> None:
    """applying -> pending: nothing was sent. Confirm's `confirm_state` goes, and with
    it the outcome when that was all of it."""
    with get_session() as session, session.begin():
        _change(
            session,
            *key.matches(),
            Draft.state == "applying",
            state="pending",
            outcome=sa.case(
                (
                    sa.func.jsonb_typeof(Draft.outcome) == "object",
                    sa.func.nullif(
                        Draft.outcome.op("-", return_type=JSONB)(
                            sa.cast(sa.literal(CONFIRM_STATE), sa.Text)
                        ),
                        sa.cast(sa.literal("{}"), JSONB),
                    ),
                ),
                else_=Draft.outcome,
            ),
        )


def checkpoint(
    key: DraftKey,
    confirm_state: Mapping[str, Any],
    outcome: DraftOutcome | None = None,
) -> bool:
    """Before a write: Confirm's `confirm_state` and, with `outcome`, what is known so
    far in place of what was stored. False: the draft is not being applied any more,
    and nothing may be sent.

    `updated_at` stays, as with `keep_applying`. Raises ValueError for a state of more
    than MAX_CONFIRM_STATE_BYTES (as compact JSON in UTF-8): nothing is stored then.
    """
    size = len(
        json.dumps(confirm_state, separators=(",", ":"), ensure_ascii=False).encode()
    )
    if size > MAX_CONFIRM_STATE_BYTES:
        raise ValueError("confirm_state too large")
    state = {CONFIRM_STATE: dict(confirm_state)}
    if outcome is None:
        value = sa.case(
            (
                sa.func.jsonb_typeof(Draft.outcome) == "object",
                Draft.outcome.op("||", return_type=JSONB)(_jsonb(state)),
            ),
            else_=_jsonb(state),
        )
    else:
        value = _jsonb({**_strict(outcome), **state})
    with get_session() as session, session.begin():
        draft = _change(
            session,
            *key.matches(),
            Draft.state == "applying",
            outcome=value,
            updated_at=Draft.updated_at,
        )
        return draft is not None


def keep_applying(key: DraftKey, outcome: DraftOutcome) -> DraftView | None:
    """Store what is known so far; Confirm's `confirm_state` stays. None: it is not
    being applied any more.

    `updated_at` stays: it says when somebody may look at the alerts again.
    """
    with get_session() as session, session.begin():
        draft = _change(
            session,
            *key.matches(),
            Draft.state == "applying",
            outcome=_keeping_state(_jsonb(_strict(outcome))),
            updated_at=Draft.updated_at,
        )
        return None if draft is None else to_view(draft)


def _jsonb(value: Any) -> Any:
    return sa.bindparam(None, value, type_=JSONB(none_as_null=True))


def _keeping_state(value: Any) -> Any:
    """`value`, with the stored outcome's `confirm_state` when it has one."""
    stored = Draft.outcome[CONFIRM_STATE]
    return sa.case(
        (
            sa.func.jsonb_typeof(stored) == "object",
            value.op("||", return_type=JSONB)(
                sa.func.jsonb_build_object(
                    sa.cast(sa.literal(CONFIRM_STATE), sa.Text), stored
                )
            ),
        ),
        else_=value,
    )


def resolve(
    key: DraftKey,
    *,
    from_state: DraftState,
    state: DraftState,
    event: str,
    outcome: DraftOutcome | None = None,
    by_user: bool = True,
    merge_outcome: bool = False,
) -> DraftView | None:
    """The final state, and the row that tells the model, all or nothing. None: the
    draft was not in `from_state` any more.

    Only what a user does counts as activity in the conversation. Confirm's
    `confirm_state` stays with an `outcome`. `merge_outcome` keeps every key of the
    stored outcome that `outcome` does not have: a newer version's too, which this one
    cannot read.
    """
    with get_session() as session, session.begin():
        conversation = (
            Conversation.id == key.conversation_id,
            Conversation.tenant_id == key.tenant_id,
        )
        session.execute(
            sa.select(Conversation.id)
            .where(*conversation)
            .with_for_update(key_share=True, read=not by_user)
        )
        draft = _change(
            session,
            *key.matches(),
            Draft.state == from_state,
            state=state,
            resolved_at=sa.func.now(),
            outcome=_outcome_value(outcome, merge_outcome),
        )
        if draft is None:
            return None
        if by_user:
            session.execute(
                sa.update(Conversation)
                .where(*conversation)
                .values(updated_at=sa.func.now())
            )
        session.execute(
            sa.insert(Message).values(
                id=uuid.uuid4(),
                tenant_id=key.tenant_id,
                conversation_id=key.conversation_id,
                role="event",
                content=event,
                draft_id=key.id,
            )
        )
        return to_view(draft)


def _outcome_value(outcome: DraftOutcome | None, merge: bool) -> Any:
    if outcome is None:
        return None
    value = _jsonb(_strict(outcome))
    if not merge:
        return _keeping_state(value)
    return sa.case(
        (
            sa.func.jsonb_typeof(Draft.outcome) == "object",
            Draft.outcome.op("||", return_type=JSONB)(value),
        ),
        else_=value,
    )


def claim_look(
    key: DraftKey, after_seconds: int
) -> tuple[DraftView, Optional[ConfirmState]] | None:
    """The draft and its Confirm's `confirm_state`, for one caller per `after_seconds`
    since it was claimed. None: it is too early, or it is not being applied any
    more."""
    with get_session() as session, session.begin():
        draft = _change(
            session,
            *key.matches(),
            Draft.state == "applying",
            Draft.updated_at < sa.func.now() - timedelta(seconds=after_seconds),
        )
        return None if draft is None else (to_view(draft), confirm_state_of(draft))


def owned_draft(identity: Identity, draft_id: uuid.UUID) -> DraftView:
    """The caller's draft, as it is. Raises draft_not_found: another user's draft does
    not exist for this one."""
    with get_session() as session:
        draft = session.scalar(
            sa.select(Draft).where(Draft.id == draft_id, *owned(identity))
        )
        if draft is None:
            raise AssistantError("draft_not_found")
        return to_view(draft)


def discard(identity: Identity, draft_id: uuid.UUID, body: Any = None) -> DraftView:
    """pending -> discarded; again is fine, whatever the reason. Raises as `claim`
    does, and a 400 for a body this draft cannot take: after the draft was found, so
    that another user's draft does not exist for any body."""
    view = owned_draft(identity, draft_id)
    reason = _discard_reason(view, body)
    if view.state == "pending":
        key = key_of(identity, view)
        discarded = resolve(
            key,
            from_state="pending",
            state="discarded",
            event=outcomes.discarded_text(view, reason),
            outcome=None if reason is None else outcomes.opened_in_form(),
        )
        if discarded is not None:
            event = "discarded" if reason is None else "opened_in_form"
            metrics.drafts_total.labels(kind=view.kind, event=event).inc()
            return discarded
        view = load(key)  # somebody was faster
    if view.state == "discarded":
        return view
    raise conflict(view)


def _discard_reason(draft: DraftView, body: Any) -> DiscardReason | None:
    if body is not None and not isinstance(body, dict):
        raise validation_error("body", "invalid_value")
    reason = DiscardRequest.parse_obj(body or {}).reason
    if reason is not None:
        kind = kind_of(draft.kind)
        if kind is None or not kind.can_open_in_form:
            raise validation_error("reason", "invalid_value")
    return reason


def load(key: DraftKey) -> DraftView:
    """Raises draft_not_found."""
    with get_session() as session:
        draft = session.scalar(sa.select(Draft).where(*key.matches()))
        if draft is None:
            raise AssistantError("draft_not_found")
        return to_view(draft)


def _change(
    session: Session, *where: sa.ColumnElement[bool], **values: Any
) -> Draft | None:
    return session.scalars(
        sa.update(Draft)
        .where(*where)
        .values(**{"updated_at": sa.func.now(), **values})
        .returning(Draft)
        .execution_options(synchronize_session=False, populate_existing=True)
    ).one_or_none()


def load_many(
    session: Session, identity: Identity, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Draft]:
    if not ids:
        return {}
    found = session.scalars(sa.select(Draft).where(Draft.id.in_(ids), *owned(identity)))
    return {draft.id: draft for draft in found}
