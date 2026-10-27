"""
Phase 1 — active & connected users metrics.

Covers:
- compute_active_users: distinct-user counts per window over a seeded AlertAudit,
  excluding system actors and rows outside the window.
- refresh_active_users_metric: sets keep_active_users gauge with correct values.
- connected_users gauge: increments on SSE subscribe, decrements on disconnect.
"""

from datetime import datetime, timedelta

import pytest

from src.models.db.alert import AlertAudit
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.repositories.metrics import active_users_gauge, connected_users_gauge
from src.services.active_users import compute_active_users, refresh_active_users_metric
from src.services.sse import SSEBroadcaster


def _gauge_value(gauge, **labels) -> float:
    """
    Read a gauge child's value for this process.

    We deliberately avoid MultiProcessCollector here: the test shares
    PROMETHEUS_MULTIPROC_DIR with any locally-running gateway, whose mmap files
    can vanish mid-scan and raise FileNotFoundError. The per-process value is
    deterministic for a single-process test.
    """
    return gauge.labels(**labels)._value.get()


def _add_audit(
    session, user_id, when, *, tenant_id=SINGLE_TENANT_UUID, action="status_change"
):
    session.add(
        AlertAudit(
            fingerprint="fp-1",
            tenant_id=tenant_id,
            timestamp=when,
            user_id=user_id,
            action=action,
            description="test audit",
        )
    )


def _seed_audits(session, now):
    # 3 distinct humans active within the last hour
    for uid in ("alice", "bob", "carol"):
        _add_audit(session, uid, now - timedelta(hours=1))
    # 1 human active 10 days ago (only inside the 30d window)
    _add_audit(session, "dave", now - timedelta(days=10))
    # system actor within the last hour -> must be excluded everywhere
    _add_audit(session, "system", now - timedelta(hours=1))
    # human active 40 days ago -> outside every window
    _add_audit(session, "erin", now - timedelta(days=40))
    session.commit()


def test_compute_active_users_distinct_per_window(db_session):
    now = datetime.utcnow()
    _seed_audits(db_session, now)

    counts = compute_active_users(db_session, now=now)

    # Regression: tenant_id must be the full string, not the first character
    # (session.exec(select(distinct(col))) yields scalars, so `row[0]` would
    # have sliced "keep" -> "k").
    assert {tenant_id for (tenant_id, _window) in counts} == {SINGLE_TENANT_UUID}

    assert counts[(SINGLE_TENANT_UUID, "1d")] == 3  # alice, bob, carol
    assert counts[(SINGLE_TENANT_UUID, "7d")] == 3
    assert counts[(SINGLE_TENANT_UUID, "30d")] == 4  # + dave; erin & system excluded


def test_refresh_active_users_sets_gauge(db_session):
    now = datetime.utcnow()
    _seed_audits(db_session, now)

    refresh_active_users_metric(session=db_session, now=now)

    assert (
        _gauge_value(active_users_gauge, tenant_id=SINGLE_TENANT_UUID, window="1d") == 3
    )
    assert (
        _gauge_value(active_users_gauge, tenant_id=SINGLE_TENANT_UUID, window="30d")
        == 4
    )


def test_compute_active_users_excludes_system_only_tenant(db_session):
    now = datetime.utcnow()
    # Only a system actor -> the tenant should not appear at all.
    _add_audit(db_session, "system", now - timedelta(hours=1))
    db_session.commit()

    counts = compute_active_users(db_session, now=now)

    assert (SINGLE_TENANT_UUID, "1d") not in counts


@pytest.mark.asyncio
async def test_connected_users_gauge_inc_on_subscribe_dec_on_disconnect():
    tenant_id = "conn-test-tenant"
    broadcaster = SSEBroadcaster()

    before = _gauge_value(connected_users_gauge, tenant_id=tenant_id)

    gen = broadcaster.subscribe(tenant_id)
    # First item is the "connected" event; pulling it runs the subscribe body
    # (queue registered + gauge incremented).
    first = await gen.__anext__()
    assert "connected" in first

    during = _gauge_value(connected_users_gauge, tenant_id=tenant_id)
    assert during == before + 1

    # Closing the generator runs the finally block (gauge decremented).
    await gen.aclose()

    after = _gauge_value(connected_users_gauge, tenant_id=tenant_id)
    assert after == before
