"""Drafts: the only thing the model can make, and only the user can confirm.

A draft changes its state in one conditional UPDATE, whoever does it: of two that race,
one finds no row. Locks are always taken in the order conversation, draft, message.
"""
import json
import uuid
from collections.abc import Collection
from datetime import timedelta

import sqlalchemy as sa
from sqlmodel import Session

from src.services.assistant.config import get_settings
from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.db import Draft
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
        outcome=draft.outcome,
    )


def owned(identity: Identity) -> tuple[sa.ColumnElement[bool], ...]:
    return Draft.tenant_id == identity.tenant_id, Draft.created_by == identity.email


def load_many(
    session: Session, identity: Identity, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Draft]:
    if not ids:
        return {}
    found = session.scalars(sa.select(Draft).where(Draft.id.in_(ids), *owned(identity)))
    return {draft.id: draft for draft in found}
