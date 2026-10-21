"""Re-checks the alerts a "Hide From Feed" window hid after the window is edited.

The event-handler records the matching window's id on lastalert when it stores
an alert the window hides, and the feed hides the row while that window runs.
Ending, disabling, deleting, rescheduling or switching the window to suppress
mode is handled at read time. Editing its filter or ignore statuses is not, so
the update route calls this to release the alerts the new definition no longer
matches.

The filter is evaluated against the stored latest occurrence, which keeps the
native alert columns only: AlertDto fields without a column (e.g. `component`)
and extra keys a sender included are not stored. When the filter references
one of those, the outcome is unknown and the alert stays hidden until the
window stops running.
"""

import json
import logging

import celpy
from sqlalchemy import and_, update
from sqlmodel import Session, select

from src.models.db.alert import Alert, LastAlert
from src.models.db.maintenance_window import MaintenanceWindowRule
from src.utils.cel_utils import preprocess_cel_expression

logger = logging.getLogger(__name__)


def _still_matches(program, rule: MaintenanceWindowRule, alert: Alert) -> bool:
    """Same rules as the event-handler's MaintenanceWindowsBl: an ignored status
    never matches, otherwise the CEL filter decides. Anything that cannot be
    evaluated counts as a match, so an alert is only released on a clear no."""
    if alert.status in (rule.ignore_statuses or []):
        return False
    payload = alert.dict()
    source = payload.get("source")
    if isinstance(source, list) and source:
        payload["source"] = source[0]
    activation = celpy.json_to_cel(json.loads(json.dumps(payload, default=str)))
    try:
        return bool(program.evaluate(activation))
    except celpy.evaluation.CELEvalError:
        return True


def release_alerts_no_longer_matched(
    session: Session, rule: MaintenanceWindowRule
) -> int:
    """Clear maintenance_window_id on the rows this window hid whose latest
    occurrence the window's current definition no longer matches. Returns the
    number of alerts released; the caller notifies open feeds."""
    rows = session.exec(
        select(LastAlert.fingerprint, Alert)
        .join(
            Alert,
            and_(Alert.id == LastAlert.alert_id, Alert.tenant_id == LastAlert.tenant_id),
        )
        .where(
            LastAlert.tenant_id == rule.tenant_id,
            LastAlert.maintenance_window_id == rule.id,
        )
    ).all()
    if not rows:
        return 0

    try:
        environment = celpy.Environment()
        program = environment.program(
            environment.compile(preprocess_cel_expression(rule.cel_query))
        )
    except Exception:
        logger.exception(
            "Failed to compile maintenance window filter, keeping alerts hidden",
            extra={"tenant_id": rule.tenant_id, "maintenance_rule_id": rule.id},
        )
        return 0

    released = 0
    for fingerprint, alert in rows:
        if _still_matches(program, rule, alert):
            continue
        # Only if the row still points at the occurrence evaluated here, so a
        # newer occurrence written meanwhile is left to the event-handler.
        result = session.execute(
            update(LastAlert)
            .where(
                LastAlert.tenant_id == rule.tenant_id,
                LastAlert.fingerprint == fingerprint,
                LastAlert.alert_id == alert.id,
                LastAlert.maintenance_window_id == rule.id,
            )
            .values(maintenance_window_id=None)
            .execution_options(synchronize_session=False)
        )
        released += result.rowcount
    session.commit()
    if released:
        logger.info(
            "Released alerts no longer matched by an edited maintenance window",
            extra={
                "tenant_id": rule.tenant_id,
                "maintenance_rule_id": rule.id,
                "count": released,
            },
        )
    return released
