"""Hard delete — the in-process, tenant-scoped port.

`DELETE /alerts` with `soft_delete=false` used to write `{"deleted": true}` here
and leave the actual row removal to keep-event-handler's `delete` handler. Two
problems, both covered below:

  1. Once the gateway stops producing to Kafka nothing consumes that event, so a
     hard delete silently degrades into a soft delete — rows stay in the database
     while the caller is told they are gone.
  2. The consumer's `delete_alert` takes a fingerprint with **no tenant
     predicate**, so one tenant's hard delete removes every tenant's rows sharing
     that fingerprint. Fingerprints derive from alert content, so a cross-tenant
     collision is plausible rather than theoretical.

`test_hard_delete_is_scoped_to_the_acting_tenant` is the guard for (2). It is
written to fail against the consumer's implementation — that is the point of it.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.db.alert import (
    Alert,
    AlertAudit,
    CommentMention,
    LastAlert,
    LastAlertToIncident,
)
from src.models.db.tenant import Tenant
from src.repositories.db import delete_alert
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.routes import alerts as alerts_route

OTHER_TENANT = "other-tenant"
SHARED_FINGERPRINT = "shared-fingerprint"


def _seed_alert(db_session, tenant_id, fingerprint):
    """One alert plus every row a hard delete is meant to take with it."""
    ts = datetime.now(timezone.utc)
    alert = Alert(
        tenant_id=tenant_id,
        provider_type="test",
        provider_id="test",
        fingerprint=fingerprint,
        source="test",
        status="firing",
        severity="critical",
        name="name-" + fingerprint,
        timestamp=ts,
    )
    db_session.add(alert)
    db_session.commit()

    db_session.add(
        LastAlert(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            timestamp=ts,
            first_timestamp=ts,
            alert_id=alert.id,
            last_received=ts,
        )
    )
    audit = AlertAudit(
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        user_id="tester",
        action="created",
        description="seeded",
    )
    db_session.add(audit)
    db_session.commit()

    db_session.add(
        CommentMention(
            tenant_id=tenant_id,
            comment_id=audit.id,
            mentioned_user_id="someone",
        )
    )
    db_session.add(
        LastAlertToIncident(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            incident_id=uuid4(),
        )
    )
    db_session.commit()
    return alert


def _counts(db_session, tenant_id, fingerprint):
    def q(model):
        return (
            db_session.query(model)
            .filter(model.tenant_id == tenant_id, model.fingerprint == fingerprint)
            .count()
        )

    return {
        "alert": q(Alert),
        "lastalert": q(LastAlert),
        "audit": q(AlertAudit),
        "last_alert_to_incident": q(LastAlertToIncident),
        "mentions": db_session.query(CommentMention)
        .filter(CommentMention.tenant_id == tenant_id)
        .count(),
    }


ALL_GONE = {
    "alert": 0,
    "lastalert": 0,
    "audit": 0,
    "last_alert_to_incident": 0,
    "mentions": 0,
}
ALL_PRESENT = {
    "alert": 1,
    "lastalert": 1,
    "audit": 1,
    "last_alert_to_incident": 1,
    "mentions": 1,
}


def test_hard_delete_removes_every_row_for_the_fingerprint(db_session):
    """The point of the port: rows actually go, with no consumer involved."""
    _seed_alert(db_session, SINGLE_TENANT_UUID, "fp-hard-delete")
    assert _counts(db_session, SINGLE_TENANT_UUID, "fp-hard-delete") == ALL_PRESENT

    delete_alert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint="fp-hard-delete",
        session=db_session,
    )

    assert _counts(db_session, SINGLE_TENANT_UUID, "fp-hard-delete") == ALL_GONE


def test_hard_delete_is_scoped_to_the_acting_tenant(db_session):
    """Two tenants, one fingerprint: deleting for A must leave B untouched.

    **This test fails against the consumer's implementation**, which deletes by
    fingerprint with no tenant predicate. That is a live cross-tenant defect
    today, reachable whenever the gateway publishes an `EventType.DELETE` event,
    and closing it is why the port is a rewrite rather than a copy.
    """
    db_session.add(Tenant(id=OTHER_TENANT, name="other", created_by="tests@keephq.dev"))
    db_session.commit()

    _seed_alert(db_session, SINGLE_TENANT_UUID, SHARED_FINGERPRINT)
    _seed_alert(db_session, OTHER_TENANT, SHARED_FINGERPRINT)

    delete_alert(
        tenant_id=SINGLE_TENANT_UUID,
        fingerprint=SHARED_FINGERPRINT,
        session=db_session,
    )

    assert _counts(db_session, SINGLE_TENANT_UUID, SHARED_FINGERPRINT) == ALL_GONE
    # The other tenant is untouched — including its CommentMention, which hangs
    # off an AlertAudit id the unscoped subquery would have matched.
    assert _counts(db_session, OTHER_TENANT, SHARED_FINGERPRINT) == ALL_PRESENT


def test_hard_delete_of_an_absent_fingerprint_is_a_noop(db_session):
    _seed_alert(db_session, SINGLE_TENANT_UUID, "fp-keep-me")

    delete_alert(
        tenant_id=SINGLE_TENANT_UUID, fingerprint="fp-not-here", session=db_session
    )

    assert _counts(db_session, SINGLE_TENANT_UUID, "fp-keep-me") == ALL_PRESENT


# --------------------------------------------------------------------------- #
# Route level — the Phase 3 regression guard
# --------------------------------------------------------------------------- #
def _delete_body(soft_delete):
    body = MagicMock()
    body.fingerprint = "fp-route"
    body.soft_delete = soft_delete
    body.restore = False
    body.last_received = "2026-01-01T00:00:00Z"
    return body


def _entity():
    entity = MagicMock()
    entity.tenant_id = SINGLE_TENANT_UUID
    entity.email = "user@example.com"
    return entity


@pytest.mark.asyncio
async def test_route_hard_delete_removes_rows_without_a_producer():
    """`DELETE /alerts` with `soft_delete=false` must not depend on Kafka.

    The Phase 3 guard: once the producer is gone this route still has to remove
    rows. It also must not publish — the consumer's handler is the unscoped one,
    so asking it to help is what makes the cross-tenant defect reachable.
    """
    producer = AsyncMock()

    with patch.object(alerts_route, "delete_alert_db") as hard_delete:
        with patch.object(alerts_route, "EnrichmentsBl") as bl:
            resp = await alerts_route.delete_alert(
                delete_alert=_delete_body(soft_delete=False),
                authenticated_entity=_entity(),
                event_producer=producer,
            )

    assert resp == {"status": "ok"}
    hard_delete.assert_called_once_with(
        tenant_id=SINGLE_TENANT_UUID, fingerprint="fp-route"
    )
    # No enrichment write, and nothing published.
    bl.assert_not_called()
    producer.produce.assert_not_called()


@pytest.mark.asyncio
async def test_route_soft_delete_still_goes_through_enrichment():
    """Soft delete is unchanged — a typed column write, not a row removal."""
    producer = AsyncMock()
    bl_instance = AsyncMock()

    with patch.object(alerts_route, "delete_alert_db") as hard_delete:
        with patch.object(alerts_route, "EnrichmentsBl", return_value=bl_instance):
            resp = await alerts_route.delete_alert(
                delete_alert=_delete_body(soft_delete=True),
                authenticated_entity=_entity(),
                event_producer=producer,
            )

    assert resp == {"status": "ok"}
    hard_delete.assert_not_called()
    bl_instance.enrich_entity.assert_awaited_once()
    kwargs = bl_instance.enrich_entity.await_args.kwargs
    assert kwargs["enrichments"]["deleted"] is True
    assert kwargs["event_type"] is alerts_route.EventType.ENRICH
