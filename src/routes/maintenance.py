import logging
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from src.repositories.db import get_session
from src.models.db.maintenance_window import (
    MaintenanceRuleCreate,
    MaintenanceRuleRead,
    MaintenanceWindowRule,
)
from src.services.cel_validation import (
    InvalidCelException,
    ensure_valid_cel,
    http_exception_from_invalid_cel,
)
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.services.maintenance_windows import release_alerts_no_longer_matched
from src.services.sse import notify_sse

router = APIRouter()
logger = logging.getLogger(__name__)


def _notify_feed_changed(tenant_id: str) -> None:
    """Tell open feeds to reload after a window changed or was deleted.

    Alerts a "Hide From Feed" window hid are filtered on read, so ending,
    disabling, deleting or re-moding a window reveals them without any row
    changing; an empty poll-alerts is the UI's signal to refetch.
    """
    try:
        notify_sse(tenant_id, "poll-alerts", {})
    except Exception:
        logger.exception("Failed to tell client to poll alerts")


def _validate_cel_query(cel_query: str) -> None:
    """Reject a maintenance rule whose condition cannot be evaluated.

    A client can bypass the UI entirely, so the write endpoint validates rather
    than trusting a preflight. Maintenance conditions are evaluated by celpy in
    the event handler, not run as SQL, so they are checked in the `maintenance`
    context - the alert-query field rules do not apply to them.
    """
    if not cel_query or not cel_query.strip():
        # Unlike an alert search, a maintenance rule with no condition would
        # silence everything, so an expression is required here.
        raise HTTPException(status_code=400, detail="cel_query is required")

    try:
        ensure_valid_cel(cel_query, "maintenance")
    except InvalidCelException as e:
        raise http_exception_from_invalid_cel(e) from e


@router.get(
    "",
    response_model=list[MaintenanceRuleRead],
    description="Get all maintenance rules",
)
def get_maintenance_rules(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:maintenance"])
    ),
    session: Session = Depends(get_session),
) -> list[MaintenanceRuleRead]:
    rules = (
        session.query(MaintenanceWindowRule)
        .filter(MaintenanceWindowRule.tenant_id == authenticated_entity.tenant_id)
        .all()
    )
    return [MaintenanceRuleRead(**rule.dict()) for rule in rules]


@router.post(
    "", response_model=MaintenanceRuleRead, description="Create a new maintenance rule"
)
def create_maintenance_rule(
    rule_dto: MaintenanceRuleCreate,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:maintenance"])
    ),
    session: Session = Depends(get_session),
) -> MaintenanceRuleRead:
    _validate_cel_query(rule_dto.cel_query)
    rule_dto.start_time = rule_dto.start_time.astimezone(timezone.utc).replace(
        tzinfo=None
    )
    end_time = rule_dto.start_time + timedelta(seconds=rule_dto.duration_seconds)
    new_rule = MaintenanceWindowRule(
        **rule_dto.dict(),
        end_time=end_time,
        created_by=authenticated_entity.email,
        tenant_id=authenticated_entity.tenant_id,
    )
    session.add(new_rule)
    session.commit()
    session.refresh(new_rule)
    return MaintenanceRuleRead(**new_rule.dict())


@router.put(
    "/{rule_id}",
    response_model=MaintenanceRuleRead,
    description="Update an existing maintenance rule",
)
def update_maintenance_rule(
    rule_id: int,
    rule_dto: MaintenanceRuleCreate,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:maintenance"])
    ),
    session: Session = Depends(get_session),
) -> MaintenanceRuleRead:
    rule: MaintenanceWindowRule = (
        session.query(MaintenanceWindowRule)
        .filter(
            MaintenanceWindowRule.tenant_id == authenticated_entity.tenant_id,
            MaintenanceWindowRule.id == rule_id,
        )
        .first()
    )
    if not rule:
        raise HTTPException(
            status_code=404, detail="Maintenance rule not found or access denied"
        )
    _validate_cel_query(rule_dto.cel_query)
    rule_dto.start_time = rule_dto.start_time.astimezone(timezone.utc).replace(
        tzinfo=None
    )
    for key, value in rule_dto.dict().items():
        setattr(rule, key, value)

    end_time = rule_dto.start_time + timedelta(seconds=rule_dto.duration_seconds)
    rule.end_time = end_time

    session.commit()
    session.refresh(rule)
    try:
        # A changed filter or ignore statuses is not covered by the feed's
        # read-time check, so release what the window no longer matches.
        release_alerts_no_longer_matched(session, rule)
    except Exception:
        session.rollback()
        logger.exception(
            "Failed to re-check alerts hidden by the edited maintenance window",
            extra={"tenant_id": rule.tenant_id, "maintenance_rule_id": rule.id},
        )
    _notify_feed_changed(authenticated_entity.tenant_id)
    # The re-check commits (or rolls back), which expires `rule`; rule.dict()
    # does not lazy-load, so reload it before building the response.
    session.refresh(rule)
    return MaintenanceRuleRead(**rule.dict())


@router.delete("/{rule_id}", description="Delete a maintenance rule")
def delete_maintenance_rule(
    rule_id: int,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:maintenance"])
    ),
    session: Session = Depends(get_session),
):
    rule = (
        session.query(MaintenanceWindowRule)
        .filter(
            MaintenanceWindowRule.tenant_id == authenticated_entity.tenant_id,
            MaintenanceWindowRule.id == rule_id,
        )
        .first()
    )
    if not rule:
        raise HTTPException(
            status_code=404, detail="Maintenance rule not found or access denied"
        )
    session.delete(rule)
    session.commit()
    _notify_feed_changed(authenticated_entity.tenant_id)
    return {"detail": "Maintenance rule deleted successfully"}


