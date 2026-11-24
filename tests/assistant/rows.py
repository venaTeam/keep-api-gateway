"""Rows put into and read from the assistant tables, past the code under test."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from src.services.assistant.models.api.draft import (
    ChangeAlertStatusPayload,
    DraftAlert,
    FingerprintSelector,
    SkippedAlert,
)
from src.services.assistant.models.db import Conversation, Draft, Message
from tests.assistant.conftest import TENANT

EMAIL = "ops@example.com"


def ago(**delta) -> datetime:
    """By the test's clock: the container shares the machine's."""
    return datetime.now(timezone.utc) - timedelta(**delta)


def add_conversation(engine, **values) -> uuid.UUID:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "created_by": EMAIL,
        "title": "a title",
        **values,
    }
    with engine.begin() as conn:
        conn.execute(sa.insert(Conversation).values(**values))
    return values["id"]


def add_message(engine, conversation_id, role="user", **values) -> uuid.UUID:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "conversation_id": conversation_id,
        "role": role,
        "content": "some text",
        **values,
    }
    with engine.begin() as conn:
        conn.execute(sa.insert(Message).values(**values))
    return values["id"]


def draft_payload(*fingerprints: str, target="acknowledged", **values) -> dict:
    payload = ChangeAlertStatusPayload(
        **{
            "selector": FingerprintSelector(fingerprints=list(fingerprints)),
            "target_status": target,
            "note": None,
            "dispose_on_new_alert": False,
            "matched_count": len(fingerprints),
            "alerts": [
                DraftAlert(
                    fingerprint=fp,
                    name=f"name of {fp}",
                    severity="critical",
                    from_status="firing",
                    service=None,
                    source="prometheus",
                    last_received="2026-11-24T09:58:00.000Z",
                    has_incident=False,
                    has_note=False,
                )
                for fp in fingerprints
            ],
            "skipped": [],
            "not_found": [],
            "unresolvable": [],
            **values,
        }
    )
    return json.loads(payload.json())


def skipped_alert(fingerprint: str, status="acknowledged") -> SkippedAlert:
    return SkippedAlert(
        fingerprint=fingerprint, name=f"name of {fingerprint}", status=status
    )


def add_draft(engine, conversation_id, *fingerprints: str, **values) -> uuid.UUID:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "conversation_id": conversation_id,
        "created_by": EMAIL,
        "kind": "change_alert_status",
        "payload": draft_payload(*(fingerprints or ("fp-1", "fp-2"))),
        "expires_at": ago(minutes=-30),
        **values,
    }
    with engine.begin() as conn:
        conn.execute(sa.insert(Draft).values(**values))
    return values["id"]


def read(engine, model, id_):
    """One row as a mapping, or None."""
    with engine.connect() as conn:
        return (
            conn.execute(sa.select(model.__table__).where(model.__table__.c.id == id_))
            .mappings()
            .one_or_none()
        )


def messages(engine, conversation_id) -> list:
    table = Message.__table__
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(table)
                .where(table.c.conversation_id == conversation_id)
                .order_by(table.c.seq)
            ).mappings()
        )


def change(engine, model, id_, **values) -> None:
    with engine.begin() as conn:
        conn.execute(sa.update(model).where(model.id == id_).values(**values))
