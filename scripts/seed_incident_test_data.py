"""Seed the local database with incidents, alerts and the links between them.

Development helper for exercising incident behaviour by hand. Writes through the
application's own repository functions rather than raw SQL, so the rows it
creates carry the same invariants the app maintains for itself — LastAlert
pointers, per-tenant running numbers, incident alert counts, sources and
affected services.

Covers the interesting dismiss states, including an already-expired one: the row
still says `dismiss_until` but the deadline has passed, so every reader should
show it at its underlying status without anything having rewritten it.

Usage:
    DATABASE_CONNECTION_STRING=postgresql://keep:keep@localhost:5432/keep \
        .venv/bin/python scripts/seed_incident_test_data.py [--wipe]

`--wipe` deletes the incidents and alerts this script created on a previous run
(matched on the `seed-` fingerprint prefix and the seeded incident names) so it
can be re-run without piling up duplicates. Nothing else is touched.
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from src.models.db.alert import Alert, LastAlert, LastAlertToIncident  # noqa: E402
from src.models.db.incident import (  # noqa: E402
    Incident,
    IncidentDismissMode,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
)
from src.repositories.db import add_alerts_to_incident, engine  # noqa: E402

TENANT_ID = "keep"
FINGERPRINT_PREFIX = "seed-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# (fingerprint suffix, name, service, source, severity, status, age in minutes)
ALERTS = [
    ("db-conn-pool", "Postgres connection pool exhausted", "payments-api", "postgres", "critical", "firing", 12),
    ("db-replica-lag", "Replica lag above 30s", "payments-api", "postgres", "warning", "firing", 18),
    ("db-disk", "Data volume 91% full", "payments-api", "postgres", "high", "firing", 25),
    ("api-5xx", "5xx rate above 2%", "checkout-api", "datadog", "critical", "firing", 8),
    ("api-latency", "p99 latency above 1.2s", "checkout-api", "datadog", "high", "firing", 9),
    ("cache-evict", "Redis eviction rate spike", "checkout-api", "redis", "warning", "firing", 40),
    ("queue-depth", "Worker queue depth above 10k", "notifications", "rabbitmq", "high", "firing", 55),
    ("queue-dlq", "Dead letter queue growing", "notifications", "rabbitmq", "warning", "firing", 60),
    ("cert-expiry", "TLS certificate expires in 6 days", "edge-proxy", "prometheus", "warning", "firing", 180),
    ("node-memory", "Node memory pressure", "edge-proxy", "prometheus", "high", "firing", 95),
    ("deploy-rollback", "Deployment rolled back", "search-api", "argocd", "info", "resolved", 240),
    ("oom-kill", "Container OOMKilled", "search-api", "kubernetes", "critical", "resolved", 300),
    ("login-errors", "Elevated login failures", "auth-api", "datadog", "high", "firing", 33),
    ("ratelimit", "Rate limit rejections spiking", "auth-api", "datadog", "warning", "firing", 37),
    ("cron-stall", "Nightly reconciliation did not run", "billing", "airflow", "critical", "firing", 480),
]

# name, severity, status, dismiss (mode, minutes from now or None), alert suffixes
INCIDENTS = [
    (
        "Payments database degraded",
        IncidentSeverity.CRITICAL,
        IncidentStatus.FIRING,
        None,
        ["db-conn-pool", "db-replica-lag", "db-disk"],
    ),
    (
        "Checkout error rate elevated",
        IncidentSeverity.HIGH,
        IncidentStatus.ACKNOWLEDGED,
        None,
        ["api-5xx", "api-latency", "cache-evict"],
    ),
    (
        "Notification backlog",
        IncidentSeverity.WARNING,
        IncidentStatus.FIRING,
        (IncidentDismissMode.PERMANENT, None),
        ["queue-depth", "queue-dlq"],
    ),
    (
        "Edge proxy maintenance noise",
        IncidentSeverity.WARNING,
        IncidentStatus.ACKNOWLEDGED,
        (IncidentDismissMode.DISMISS_UNTIL, 120),
        ["cert-expiry", "node-memory"],
    ),
    (
        "Search API rollout incident",
        IncidentSeverity.HIGH,
        IncidentStatus.FIRING,
        # Deadline already passed: reads as FIRING, and the row is untouched.
        (IncidentDismissMode.DISMISS_UNTIL, -90),
        ["deploy-rollback", "oom-kill"],
    ),
    (
        "Auth service instability",
        IncidentSeverity.HIGH,
        IncidentStatus.RESOLVED,
        None,
        ["login-errors", "ratelimit"],
    ),
    (
        "Billing reconciliation missed",
        IncidentSeverity.CRITICAL,
        IncidentStatus.FIRING,
        None,
        ["cron-stall"],
    ),
]

SEEDED_NAMES = [spec[0] for spec in INCIDENTS]


def wipe(session: Session) -> None:
    incidents = session.exec(
        select(Incident).where(
            Incident.tenant_id == TENANT_ID,
            Incident.user_generated_name.in_(SEEDED_NAMES),
        )
    ).all()
    for incident in incidents:
        # Links and enrichment rows cascade; alerts are removed separately below
        # because they outlive the incident that grouped them.
        session.delete(incident)
    session.commit()

    last_alerts = session.exec(
        select(LastAlert).where(
            LastAlert.tenant_id == TENANT_ID,
            LastAlert.fingerprint.like(f"{FINGERPRINT_PREFIX}%"),
        )
    ).all()
    for last_alert in last_alerts:
        session.delete(last_alert)
    session.commit()

    alerts = session.exec(
        select(Alert).where(
            Alert.tenant_id == TENANT_ID,
            Alert.fingerprint.like(f"{FINGERPRINT_PREFIX}%"),
        )
    ).all()
    for alert in alerts:
        session.delete(alert)
    session.commit()

    print(f"wiped {len(incidents)} incidents and {len(alerts)} alerts")


def seed_alerts(session: Session) -> dict:
    fingerprints = {}
    for suffix, name, service, source, severity, status, age_minutes in ALERTS:
        fingerprint = f"{FINGERPRINT_PREFIX}{suffix}"
        timestamp = _now() - timedelta(minutes=age_minutes)

        # Alert carries typed columns, not a provider-payload blob. Severity in
        # particular has to be a real column value: add_alerts_to_incident
        # recalculates the incident's severity from the max across its alerts, so
        # a NULL here silently drags every incident down to the lowest severity.
        alert = Alert(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            timestamp=timestamp,
            provider_type=source,
            provider_id=f"{source}-1",
            fingerprint=fingerprint,
            name=name,
            severity=severity,
            status=status,
            service=service,
            source=[source],
            description=f"{name} ({service})",
            environment="production",
            received_at=timestamp,
        )
        session.add(alert)
        session.flush()

        session.add(
            LastAlert(
                tenant_id=TENANT_ID,
                fingerprint=fingerprint,
                alert_id=alert.id,
                timestamp=timestamp,
                first_timestamp=timestamp,
                last_received=timestamp,
                firing_counter=1,
                unresolved_counter=0 if status == "resolved" else 1,
            )
        )
        fingerprints[suffix] = fingerprint

    session.commit()
    print(f"created {len(fingerprints)} alerts")
    return fingerprints


def seed_incidents(session: Session, fingerprints: dict) -> None:
    for name, severity, status, dismiss, alert_suffixes in INCIDENTS:
        dismiss_mode = None
        dismissed_until = None
        if dismiss:
            mode, offset_minutes = dismiss
            dismiss_mode = mode.value
            if offset_minutes is not None:
                dismissed_until = _now() + timedelta(minutes=offset_minutes)

        started = _now() - timedelta(hours=2)
        incident = Incident(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            user_generated_name=name,
            ai_generated_name=None,
            user_summary=f"Seeded incident: {name}",
            generated_summary="",
            assignee=None,
            severity=severity.order,
            status=status.value,
            dismiss_mode=dismiss_mode,
            dismissed_until=dismissed_until,
            creation_time=started,
            start_time=started,
            last_seen_time=_now(),
            end_time=_now() if status == IncidentStatus.RESOLVED else None,
            is_predicted=False,
            is_candidate=False,
            is_visible=True,
            incident_type=IncidentType.MANUAL.value,
        )
        session.add(incident)
        session.commit()
        session.refresh(incident)

        # Maintains the link rows plus alerts_count / sources / affected_services.
        add_alerts_to_incident(
            TENANT_ID,
            incident,
            [fingerprints[suffix] for suffix in alert_suffixes],
            session=session,
        )

        session.refresh(incident)
        effective = incident.get_effective_status()
        note = "" if effective == incident.status else f" (stored: {incident.status})"
        # Read severity back rather than echoing the requested one: linking alerts
        # recalculates it from the max across them unless forced_severity is set.
        actual_severity = IncidentSeverity.from_number(incident.severity).value
        print(
            f"  {name}: status={effective}{note} "
            f"severity={actual_severity} alerts={incident.alerts_count}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="remove previously seeded rows before seeding",
    )
    args = parser.parse_args()

    with Session(engine) as session:
        if args.wipe:
            wipe(session)
        existing = session.exec(
            select(LastAlert).where(
                LastAlert.tenant_id == TENANT_ID,
                LastAlert.fingerprint.like(f"{FINGERPRINT_PREFIX}%"),
            )
        ).first()
        if existing:
            print(
                "Seed data already present. Re-run with --wipe to replace it.",
                file=sys.stderr,
            )
            sys.exit(1)

        fingerprints = seed_alerts(session)
        print("created incidents:")
        seed_incidents(session, fingerprints)

    total_links = sum(len(spec[4]) for spec in INCIDENTS)
    print(f"\ndone: {len(INCIDENTS)} incidents, {len(ALERTS)} alerts, {total_links} links")


if __name__ == "__main__":
    main()
