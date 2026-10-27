"""Operator API-key provisioning service.

Coordinates Kong + Grafana with the local Operator row. The flow mirrors
pandora-backend's `api_key_service` but stores the resulting key in
`Operator.apikey` instead of MongoDB.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.exceptions.tenant_exceptions import OperatorGroupTaken, OperatorNameTaken
from src.integrations.grafana import GrafanaIntegration
from src.integrations.kong import KongIntegration
from src.models.db.operator import Operator
from src.repositories.db import engine

logger = logging.getLogger(__name__)


class ProvisioningStep(Enum):
    """Steps in the operator API-key provisioning process."""

    KONG_CONSUMER = "kong_consumer"
    KONG_API_KEY = "kong_api_key"
    OPERATOR_ROW = "operator_row"
    GRAFANA = "grafana"


@dataclass
class ProvisioningState:
    """Tracks what has been provisioned for potential rollback."""

    operator: str
    mail_group: str
    provisioned_steps: List[ProvisioningStep] = field(default_factory=list)
    kong_consumer_id: Optional[str] = None
    api_key: Optional[str] = None

    def mark_step_complete(self, step: ProvisioningStep) -> None:
        """Mark a provisioning step as complete."""
        if step not in self.provisioned_steps:
            self.provisioned_steps.append(step)

    def get_completed_steps_for_rollback(self) -> List[ProvisioningStep]:
        """Return steps in reverse order for rollback."""
        return list(reversed(self.provisioned_steps))


# Service instances (stateless)
_kong_integration = KongIntegration()
_grafana_integration = GrafanaIntegration()


def _rollback_provisioning(state: ProvisioningState) -> None:
    """Rollback provisioning in reverse order of creation.

    Each failure during rollback is logged but doesn't stop the process.
    """
    logger.info(
        "Starting rollback for operator=%s, steps to rollback: %s",
        state.operator,
        [s.value for s in state.get_completed_steps_for_rollback()],
    )

    for step in state.get_completed_steps_for_rollback():
        try:
            if step == ProvisioningStep.GRAFANA:
                _grafana_integration.delete_grafana_connections(operator=state.operator)
                logger.info("Rolled back Grafana for operator=%s", state.operator)

            elif step == ProvisioningStep.OPERATOR_ROW:
                # Delete the local operator row (best effort).
                with Session(engine) as session:
                    operator = session.exec(
                        select(Operator).where(Operator.name == state.operator)
                    ).first()
                    if operator:
                        session.delete(operator)
                        session.commit()
                logger.info("Rolled back operator row for operator=%s", state.operator)

            elif step == ProvisioningStep.KONG_API_KEY:
                _kong_integration.delete_api_key(operator=state.operator)
                logger.info("Rolled back Kong API key for operator=%s", state.operator)

            elif step == ProvisioningStep.KONG_CONSUMER:
                _kong_integration.delete_consumer(operator=state.operator)
                logger.info("Rolled back Kong consumer for operator=%s", state.operator)

        except Exception as rollback_error:
            # Log but continue rolling back other steps
            logger.warning(
                "Rollback step %s failed for operator=%s: %s",
                step.value,
                state.operator,
                rollback_error,
            )

    logger.info("Rollback completed for operator=%s", state.operator)


def _name_taken_error(op_name: str, group: str, exc: IntegrityError) -> Exception:
    """Return the right conflict exception based on what collided."""
    with Session(engine) as session:
        if session.exec(select(Operator).where(Operator.name == op_name)).first():
            return OperatorNameTaken(op_name)
        return OperatorGroupTaken(group)


def provision_operator(
    *,
    operator_name: str,
    mail_group: str,
    tenant_id: str,
) -> Operator:
    """Provision an operator with a Kong-issued API key and Grafana contact point.

    The operator row is created only after Kong and Grafana succeed. If anything
    fails, all external resources created so far are rolled back and the
    exception propagates to the caller.
    """
    state = ProvisioningState(operator=operator_name, mail_group=mail_group)

    try:
        # Step 1: Ensure Kong consumer exists
        kong_result = _kong_integration.ensure_consumer(
            operator=operator_name, mail_group=mail_group
        )
        state.kong_consumer_id = kong_result.consumer_id
        state.mark_step_complete(ProvisioningStep.KONG_CONSUMER)

        # Step 2: Create API key in Kong
        provisioned_key = _kong_integration.create_or_update_api_key(
            consumer_id=kong_result.consumer_id,
            operator=operator_name,
            api_key=None,
        )
        state.api_key = provisioned_key
        state.mark_step_complete(ProvisioningStep.KONG_API_KEY)

        # Step 3: Create the operator row with the Kong-issued key
        with Session(engine) as session:
            operator = Operator(
                name=operator_name,
                group=mail_group,
                tenant_id=tenant_id,
                apikey=provisioned_key,
            )
            session.add(operator)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise _name_taken_error(operator_name, mail_group, exc) from exc
            session.refresh(operator)
        state.mark_step_complete(ProvisioningStep.OPERATOR_ROW)

        # Step 4: Create Grafana contact point and sub-policy
        _grafana_integration.ensure_grafana_connections(
            operator=operator_name, api_key=provisioned_key
        )
        state.mark_step_complete(ProvisioningStep.GRAFANA)

        logger.info(
            "Operator provisioned operator=%s tenant_id=%s",
            operator_name,
            tenant_id,
        )
        return operator

    except Exception as e:
        logger.error(
            "Provisioning failed for operator=%s at step=%s, rolling back: %s",
            state.operator,
            state.provisioned_steps[-1].value if state.provisioned_steps else "unknown",
            e,
        )
        _rollback_provisioning(state)
        raise