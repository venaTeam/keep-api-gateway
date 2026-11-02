"""Re-checks the alerts a "Hide From Feed" window hid after the window is edited.

The event-handler records every window matching an alert it hides (marker
lastalert.maintenance_window_id plus 'hide' rows in lastalertmaintenancewindow),
and the feed hides the row while any of them runs. Ending, disabling,
deleting, rescheduling or switching a window to suppress mode is handled at
read time. Editing its filter or ignore statuses is not, so the update route
calls this to take the window out of the alerts its new definition no longer
matches; another window that still lists the alert keeps it hidden.

Alerts that were already on screen when windows matched them are frozen
instead: their row keeps the pre-window occurrence and holds the latest one
back, with the windows as maintenance_frozen_window_id plus 'freeze' tags.
Once no window is left the marker is cleared, and the keep-workflows watcher
catches the row up to the held-back occurrence on its next run, as it does
when the windows stop being active.

The filter is evaluated against the stored latest occurrence (for a frozen
alert, the one held back), which keeps the native alert columns only: AlertDto
fields without a column (e.g. `component`) and extra keys a sender included
are not stored. When the filter references one of those, the outcome is
unknown and the alert stays hidden or frozen until the window stops running.
"""

import json
import logging

import celpy
from sqlalchemy import and_, delete, exists, func, or_, update
from sqlmodel import Session, select

from src.models.db.alert import Alert, LastAlert, LastAlertMaintenanceWindow
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


_MARKERS = {"hide": "maintenance_window_id", "freeze": "maintenance_frozen_window_id"}


def _tagged_by(rule: MaintenanceWindowRule, kind: str):
    """The row lists `rule` among its windows of `kind`, as its marker or as a
    tag."""
    tag = LastAlertMaintenanceWindow
    return or_(
        getattr(LastAlert, _MARKERS[kind]) == rule.id,
        exists().where(
            tag.tenant_id == LastAlert.tenant_id,
            tag.fingerprint == LastAlert.fingerprint,
            tag.kind == kind,
            tag.maintenance_window_id == rule.id,
        ),
    )


def _ending_last(session: Session, tenant_id: str, window_ids: set[int]) -> int | None:
    """Of `window_ids`, the window that ends last (ties by id), the same order
    the event-handler picks the marker in. None when none of them exists."""
    if not window_ids:
        return None
    windows = session.exec(
        select(MaintenanceWindowRule.id, MaintenanceWindowRule.end_time).where(
            MaintenanceWindowRule.tenant_id == tenant_id,
            MaintenanceWindowRule.id.in_(window_ids),
        )
    ).all()
    if not windows:
        return None
    return min(windows, key=lambda w: (-w.end_time.timestamp(), w.id)).id


def _remove_window(
    session: Session,
    rule: MaintenanceWindowRule,
    fingerprint: str,
    kind: str,
    marker: int | None,
    guard,
) -> int:
    """Take `rule` out of the row's windows of `kind`. The marker moves to the
    remaining window that ends last, or becomes NULL when none is left (hidden:
    shown; frozen: released for the keep-workflows watcher to catch up). Only
    applies while `guard` holds and the marker is unchanged, so an occurrence
    written meanwhile is left to the event-handler. Returns 1 if applied."""
    tag = LastAlertMaintenanceWindow
    remaining = set(
        session.exec(
            select(tag.maintenance_window_id).where(
                tag.tenant_id == rule.tenant_id,
                tag.fingerprint == fingerprint,
                tag.kind == kind,
            )
        ).all()
    )
    if marker is not None:
        remaining.add(marker)
    remaining.discard(rule.id)
    new_marker = _ending_last(session, rule.tenant_id, remaining)

    marker_column = getattr(LastAlert, _MARKERS[kind])
    result = session.execute(
        update(LastAlert)
        .where(
            LastAlert.tenant_id == rule.tenant_id,
            LastAlert.fingerprint == fingerprint,
            guard,
            marker_column.is_(None) if marker is None else marker_column == marker,
        )
        .values({_MARKERS[kind]: new_marker})
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return 0
    removed = delete(tag).where(
        tag.tenant_id == rule.tenant_id,
        tag.fingerprint == fingerprint,
        tag.kind == kind,
    )
    if new_marker is not None:
        removed = removed.where(tag.maintenance_window_id == rule.id)
    session.execute(removed)
    return 1


def release_alerts_no_longer_matched(
    session: Session, rule: MaintenanceWindowRule
) -> int:
    """Take this window out of the rows it hides or freezes whose latest
    occurrence its current definition no longer matches. A row another active
    window still lists stays hidden or frozen under that window; otherwise a
    hidden row shows and a frozen one is released for catch-up. Returns the
    number of rows the window no longer applies to; the caller notifies open
    feeds."""
    rows = session.exec(
        select(LastAlert.fingerprint, LastAlert.maintenance_window_id, Alert)
        .join(
            Alert,
            and_(Alert.id == LastAlert.alert_id, Alert.tenant_id == LastAlert.tenant_id),
        )
        .where(LastAlert.tenant_id == rule.tenant_id, _tagged_by(rule, "hide"))
    ).all()
    frozen_rows = session.exec(
        select(
            LastAlert.fingerprint,
            LastAlert.maintenance_frozen_window_id,
            LastAlert.maintenance_pending_alert_id,
            Alert,
        )
        .join(
            Alert,
            and_(
                Alert.id
                == func.coalesce(
                    LastAlert.maintenance_pending_alert_id, LastAlert.alert_id
                ),
                Alert.tenant_id == LastAlert.tenant_id,
            ),
        )
        .where(LastAlert.tenant_id == rule.tenant_id, _tagged_by(rule, "freeze"))
    ).all()
    if not rows and not frozen_rows:
        return 0

    try:
        environment = celpy.Environment()
        program = environment.program(
            environment.compile(preprocess_cel_expression(rule.cel_query))
        )
    except Exception:
        logger.exception(
            "Failed to compile maintenance window filter, keeping alerts hidden or frozen",
            extra={"tenant_id": rule.tenant_id, "maintenance_rule_id": rule.id},
        )
        return 0

    released = 0
    for fingerprint, marker, alert in rows:
        if _still_matches(program, rule, alert):
            continue
        # Only if the row still points at the occurrence evaluated here.
        released += _remove_window(
            session, rule, fingerprint, "hide", marker, LastAlert.alert_id == alert.id
        )
    for fingerprint, marker, pending_alert_id, alert in frozen_rows:
        if _still_matches(program, rule, alert):
            continue
        # Only if the row still holds back the occurrence evaluated here.
        held_back = (
            LastAlert.maintenance_pending_alert_id.is_(None)
            if pending_alert_id is None
            else LastAlert.maintenance_pending_alert_id == pending_alert_id
        )
        released += _remove_window(
            session, rule, fingerprint, "freeze", marker, held_back
        )
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
