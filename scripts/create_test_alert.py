"""Create a single alert directly in the database, linked to nothing.

Development helper for exercising alert behaviour by hand — the normal ingestion
route (`POST /alerts/event/{provider}`) publishes to Kafka and needs the
event-handler consumer running to land anything in the database, which is more
moving parts than a manual check usually wants.

Writes the same shape the application maintains for itself: an `alert` row for
the occurrence plus the `lastalert` row that points at it. No incident links, so
the alert can be attached by hand afterwards to watch what it inherits.

Usage:
    DATABASE_CONNECTION_STRING=postgresql://keep:keep@localhost:5432/keep \
        .venv/bin/python scripts/create_test_alert.py \
            --name "Queue depth critical" --service notifications --severity critical

    # re-run the same fingerprint to simulate a repeat occurrence
    ... --fingerprint manual-1 --status resolved
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from src.models.db.alert import Alert, LastAlert  # noqa: E402
from src.repositories.db import engine  # noqa: E402

TENANT_ID = "keep"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Manual test alert")
    parser.add_argument(
        "--fingerprint",
        default=None,
        help="defaults to a fresh manual-<uuid8> value; reuse one to simulate a "
        "repeat occurrence of the same alert",
    )
    parser.add_argument("--service", default="manual-test")
    parser.add_argument("--source", default="prometheus")
    parser.add_argument(
        "--severity",
        default="critical",
        choices=["critical", "high", "warning", "info", "low"],
    )
    parser.add_argument(
        "--status",
        default="firing",
        choices=["firing", "resolved", "acknowledged", "suppressed", "pending"],
    )
    parser.add_argument("--description", default=None)
    args = parser.parse_args()

    fingerprint = args.fingerprint or f"manual-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        alert = Alert(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            timestamp=now,
            provider_type=args.source,
            provider_id=f"{args.source}-manual",
            fingerprint=fingerprint,
            name=args.name,
            severity=args.severity,
            status=args.status,
            service=args.service,
            source=[args.source],
            description=args.description or f"{args.name} ({args.service})",
            environment="production",
            received_at=now,
        )
        session.add(alert)
        session.flush()

        existing = session.exec(
            select(LastAlert).where(
                LastAlert.tenant_id == TENANT_ID,
                LastAlert.fingerprint == fingerprint,
            )
        ).first()

        if existing:
            # Repeat occurrence: repoint lastalert at the new row, exactly as
            # set_last_alert would. Enrichment state on the row is left alone.
            existing.alert_id = alert.id
            existing.timestamp = now
            existing.last_received = now
            existing.firing_counter = (existing.firing_counter or 0) + 1
            if args.status != "resolved":
                existing.unresolved_counter = (existing.unresolved_counter or 0) + 1
            session.add(existing)
            action = "updated (repeat occurrence)"
        else:
            session.add(
                LastAlert(
                    tenant_id=TENANT_ID,
                    fingerprint=fingerprint,
                    alert_id=alert.id,
                    timestamp=now,
                    first_timestamp=now,
                    last_received=now,
                    firing_counter=1,
                    unresolved_counter=0 if args.status == "resolved" else 1,
                )
            )
            action = "created"

        session.commit()

    print(f"{action}: {fingerprint}")
    print(f"  name     : {args.name}")
    print(f"  severity : {args.severity}")
    print(f"  status   : {args.status}")
    print(f"  service  : {args.service}")
    print(f"  source   : {args.source}")
    print("  incidents: none (link it by hand to test inheritance)")


if __name__ == "__main__":
    main()
