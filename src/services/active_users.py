"""
Active-users (DAU/WAU/MAU) metric refresh job.

Distinct active users per tenant over rolling windows cannot be a Prometheus
label (cardinality) and cannot be summed from counters. Instead a periodic job
runs `COUNT(DISTINCT user_id)` over `AlertAudit` for each window and sets the
`keep_active_users{tenant_id, window}` gauge. Bounded series = tenants × windows.

Definition note: this reflects users who took an *audited* action (alert/incident
lifecycle), so pure read-only sessions are under-counted — an "engaged users"
definition, acceptable for now.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from sqlalchemy import distinct, func
from sqlmodel import Session, select

from src.models.db.alert import AlertAudit
from src.repositories.metrics import active_users_gauge

logger = logging.getLogger(__name__)

# Rolling windows -> Prometheus `window` label.
ACTIVE_USER_WINDOWS: Dict[str, timedelta] = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Non-human actors excluded from engagement counts. AlertAudit.user_id is set to
# one of these for worker/system-originated actions (vs. a real identity for UI
# actions). Override via env if a deployment uses different system identities.
SYSTEM_ACTORS = frozenset({"system", "keep", "Keep"})


def compute_active_users(
    session: Session,
    now: Optional[datetime] = None,
) -> Dict[Tuple[str, str], int]:
    """
    Return {(tenant_id, window): distinct_user_count} across all windows.

    Any tenant active in at least one window is reported for *every* window
    (0 where it had no activity), so the gauge reflects decreases (a tenant
    going quiet in a shorter window) rather than holding a stale high value.
    """
    now = now or datetime.utcnow()

    # window -> {tenant_id: distinct_user_count}
    per_window: Dict[str, Dict[str, int]] = {}
    tenants: set = set()
    for window, delta in ACTIVE_USER_WINDOWS.items():
        cutoff = now - delta
        rows = session.exec(
            select(
                AlertAudit.tenant_id,
                func.count(distinct(AlertAudit.user_id)),
            )
            .where(AlertAudit.timestamp >= cutoff)
            .where(AlertAudit.user_id.notin_(SYSTEM_ACTORS))
            .group_by(AlertAudit.tenant_id)
        ).all()
        # This two-column select yields Row tuples (not scalars).
        counts = {tenant_id: int(count) for tenant_id, count in rows}
        per_window[window] = counts
        tenants.update(counts.keys())

    # Emit every (tenant, window) — 0 where the tenant had no activity.
    result: Dict[Tuple[str, str], int] = {
        (tenant_id, window): per_window[window].get(tenant_id, 0)
        for tenant_id in tenants
        for window in ACTIVE_USER_WINDOWS
    }
    return result


def refresh_active_users_metric(
    session: Optional[Session] = None,
    now: Optional[datetime] = None,
) -> Dict[Tuple[str, str], int]:
    """Compute distinct active users and set the gauge. Returns the computed map."""
    own_session = session is None
    if own_session:
        # Imported lazily so the module is importable without a configured engine.
        from src.repositories.db import engine

        session = Session(engine)
    try:
        counts = compute_active_users(session, now=now)
        for (tenant_id, window), count in counts.items():
            active_users_gauge.labels(tenant_id=tenant_id, window=window).set(count)
        logger.debug(
            "Refreshed active-users metric",
            extra={"series": len(counts)},
        )
        return counts
    finally:
        if own_session:
            session.close()


async def active_users_refresh_loop(interval_seconds: int = 300) -> None:
    """Background loop: refresh the active-users gauge every `interval_seconds`."""
    logger.info(
        "Starting active-users refresh loop",
        extra={"interval_seconds": interval_seconds},
    )
    while True:
        try:
            # The query is synchronous; run it off the event loop.
            await asyncio.to_thread(refresh_active_users_metric)
        except Exception:
            logger.exception("Failed to refresh active-users metric")
        await asyncio.sleep(interval_seconds)
