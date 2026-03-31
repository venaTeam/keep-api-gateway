"""
Keep main database module.

This module contains the CRUD database functions for Keep.
"""

import hashlib
import json
import logging
import random
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Type, Union
from uuid import UUID, uuid4

import pytz
from dateutil.parser import parse
from dateutil.tz import tz
from dotenv import find_dotenv, load_dotenv
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from psycopg2.errors import NoActiveSqlTransaction
from retry import retry
from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    desc,
    func,
    literal,
    null,
    select,
    union,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import foreign, joinedload, subqueryload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql import exists, expression
from sqlalchemy.sql.functions import count
from sqlmodel import Session, SQLModel, col, or_, select, text
 
from src.config.consts import STATIC_PRESETS
from src.config.core import config
from src.repositories.db_utils import (
    create_db_engine,
    custom_serialize,
    get_json_extract_field,
    get_or_create,
)
from src.repositories.dependencies import SINGLE_TENANT_UUID

# This import is required to create the tables
from src.models.action_type import ActionType
from src.models.ai_external import (
    ExternalAIConfigAndMetadata,
    ExternalAIConfigAndMetadataDto,
)
from src.models.alert import AlertStatus
from src.models.db.action import Action
from src.models.db.ai_external import *  # pylint: disable=unused-wildcard-import
from src.models.db.alert import *  # pylint: disable=unused-wildcard-import
from src.models.db.dashboard import *  # pylint: disable=unused-wildcard-import
from src.models.db.enrichment_event import *  # pylint: disable=unused-wildcard-import
from src.models.db.extraction import *  # pylint: disable=unused-wildcard-import
from src.models.db.incident import *  # pylint: disable=unused-wildcard-import
from src.models.db.maintenance_window import *  # pylint: disable=unused-wildcard-import
from src.models.db.mapping import *  # pylint: disable=unused-wildcard-import
from src.models.db.preset import *  # pylint: disable=unused-wildcard-import
from src.models.db.provider import *  # pylint: disable=unused-wildcard-import
from src.models.db.provider_image import *  # pylint: disable=unused-wildcard-import
from src.models.db.rule import *  # pylint: disable=unused-wildcard-import
from src.models.db.system import *  # pylint: disable=unused-wildcard-import
from src.models.db.tenant import *  # pylint: disable=unused-wildcard-import

from src.models.incident import IncidentDto, IncidentDtoIn, IncidentSorting
from src.models.time_stamp import TimeStampFilter

logger = logging.getLogger(__name__)


# this is a workaround for gunicorn to load the env vars
# because somehow in gunicorn it doesn't load the .env file
load_dotenv(find_dotenv())


engine = create_db_engine()
SQLAlchemyInstrumentor().instrument(enable_commenter=True, engine=engine)


ALLOWED_INCIDENT_FILTERS = [
    "status",
    "severity",
    "sources",
    "affected_services",
    "assignee",
]
KEEP_AUDIT_EVENTS_ENABLED = config("KEEP_AUDIT_EVENTS_ENABLED", cast=bool, default=True)




def dispose_session():
    logger.info("Disposing engine pool")
    if engine.dialect.name != "sqlite":
        engine.dispose(close=False)
        logger.info("Engine pool disposed")
    else:
        logger.info("Engine pool is sqlite, not disposing")


@contextmanager
def existed_or_new_session(session: Optional[Session] = None) -> Iterator[Session]:
    try:
        if session:
            yield session
        else:
            with Session(engine) as session:
                yield session
    except Exception as e:
        e.session = session
        raise e


def get_session() -> Session:
    """
    Creates a database session.

    Yields:
        Session: A database session
    """
    from opentelemetry import trace  # pylint: disable=import-outside-toplevel

    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("get_session"):
        with Session(engine) as session:
            yield session


def get_session_sync() -> Session:
    """
    Creates a database session.

    Returns:
        Session: A database session
    """
    return Session(engine)


def __convert_to_uuid(value: str, should_raise: bool = False) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        if should_raise:
            raise ValueError(f"Invalid UUID: {value}")
        return None


def retry_on_db_error(f):
    @retry(
        exceptions=(OperationalError, IntegrityError, StaleDataError),
        tries=3,
        delay=0.1,
        backoff=2,
        jitter=(0, 0.1),
        logger=logger,
    )
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (OperationalError, IntegrityError, StaleDataError) as e:
            if hasattr(e, "session") and not e.session.is_active:
                e.session.rollback()

            if "Deadlock found" in str(e):
                logger.warning(
                    "Deadlock detected, retrying transaction", extra={"error": str(e)}
                )
                raise  # retry will catch this
            else:
                logger.exception(
                    f"Error while executing transaction during {f.__name__}",
                )
            raise  # if it's not a deadlock, let it propagate

    return wrapper





def get_mapping_rule_by_id(
    tenant_id: str, rule_id: str, session: Optional[Session] = None
) -> MappingRule | None:
    with existed_or_new_session(session) as session:
        query = select(MappingRule).where(
            MappingRule.tenant_id == tenant_id, MappingRule.id == rule_id
        )
        return session.exec(query).first()


def get_extraction_rule_by_id(
    tenant_id: str, rule_id: str, session: Optional[Session] = None
) -> ExtractionRule | None:
    with existed_or_new_session(session) as session:
        query = select(ExtractionRule).where(
            ExtractionRule.tenant_id == tenant_id, ExtractionRule.id == rule_id
        )
        return session.exec(query).first()








def get_all_provisioned_providers(tenant_id: str) -> List[Provider]:
    with Session(engine) as session:
        providers = session.exec(
            select(Provider)
            .where(Provider.tenant_id == tenant_id)
            .where(Provider.provisioned == True)
        ).all()
    return list(providers)







def get_installed_providers(tenant_id: str) -> List[Provider]:
    with Session(engine) as session:
        providers = session.exec(
            select(Provider).where(Provider.tenant_id == tenant_id)
        ).all()
    return providers


def get_consumer_providers() -> List[Provider]:
    # get all the providers that installed as consumers
    with Session(engine) as session:
        providers = session.exec(
            select(Provider).where(Provider.consumer == True)
        ).all()
    return providers





def add_audit(
    tenant_id: str,
    fingerprint: str,
    user_id: str,
    action: ActionType,
    description: str,
    session: Session = None,
    commit: bool = True,
) -> AlertAudit:
    with existed_or_new_session(session) as session:
        audit = AlertAudit(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            user_id=user_id,
            action=action.value,
            description=description,
        )
        session.add(audit)
        if commit:
            session.commit()
            session.refresh(audit)
    return audit


def _enrich_entity(
    session,
    tenant_id,
    fingerprint,
    enrichments,
    action_type: ActionType,
    action_callee: str,
    action_description: str,
    force=False,
    audit_enabled=True,
):
    """
    Enrich an alert with the provided enrichments.

    Args:
        session (Session): The database session.
        tenant_id (str): The tenant ID to filter the alert enrichments by.
        fingerprint (str): The alert fingerprint to filter the alert enrichments by.
        enrichments (dict): The enrichments to add to the alert.
        force (bool): Whether to force the enrichment to be updated. This is used to dispose enrichments if necessary.
    """
    enrichment = get_enrichment_with_session(session, tenant_id, fingerprint)
    if enrichment:
        # if force - override exisitng enrichments. being used to dispose enrichments if necessary
        if force:
            new_enrichment_data = enrichments
        else:
            new_enrichment_data = {**enrichment.enrichments, **enrichments}
        # Preserve existing note if incoming note is empty/None/not provided
        # BUT only when NOT forcing — force=True means the caller (e.g. unenrich)
        # explicitly wants to replace all enrichments, including removing the note.
        if not force:
            incoming_note = enrichments.get("note")
            if not incoming_note or (
                isinstance(incoming_note, str) and not incoming_note.strip()
            ):
                existing_note = enrichment.enrichments.get("note")
                if existing_note:
                    new_enrichment_data["note"] = existing_note
        # Remove keys with None values (e.g., status=None when undismissing)
        # This allows the alert to revert to its original value from event data
        for key, value in list(enrichments.items()):
            if value is None and key in new_enrichment_data:
                del new_enrichment_data[key]

        # When forcing update (e.g. making enrichments permanent/disposing),
        # ensure we don't accidentally keep status if it's not in the new enrichments
        if force and "status" not in enrichments and "status" in enrichment.enrichments:
            # If we are forcing and status is NOT in the new enrichments, it means we want to remove it
            # But new_enrichment_data = enrichments (line 1303), so it's already not there.
            # However, we need to make sure we don't re-add it from existing if we are forcing?
            # No, if force=True, new_enrichment_data IS enrichments.
            # So if 'status' is not in 'enrichments', it won't be in 'new_enrichment_data'.
            # BUT, we have logic above that preserves note.
            pass
        # SQLAlchemy doesn't support updating JSON fields, so we need to do it manually
        # https://github.com/sqlalchemy/sqlalchemy/discussions/8396#discussion-4308891
        stmt = (
            update(AlertEnrichment)
            .where(AlertEnrichment.id == enrichment.id)
            .values(enrichments=new_enrichment_data)
        )
        session.execute(stmt)
        if audit_enabled:
            # add audit event
            audit = AlertAudit(
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                user_id=action_callee,
                action=action_type.value,
                description=action_description,
            )
            session.add(audit)
        session.commit()
        # Refresh the instance to get updated data from the database
        session.refresh(enrichment)
        return enrichment
    else:
        try:
            alert_enrichment = AlertEnrichment(
                tenant_id=tenant_id,
                alert_fingerprint=fingerprint,
                enrichments=enrichments,
            )
            session.add(alert_enrichment)
            # add audit event
            if audit_enabled:
                audit = AlertAudit(
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    user_id=action_callee,
                    action=action_type.value,
                    description=action_description,
                )
                session.add(audit)
            session.commit()
            return alert_enrichment
        except IntegrityError:
            # If we hit a duplicate entry error, rollback and get the existing enrichment
            logger.warning(
                "Duplicate entry error",
                extra={
                    "tenant_id": tenant_id,
                    "fingerprint": fingerprint,
                    "enrichments": enrichments,
                },
            )
            session.rollback()
            return get_enrichment_with_session(session, tenant_id, fingerprint)


def batch_enrich(
    tenant_id,
    fingerprints,
    enrichments,
    action_type: ActionType,
    action_callee: str,
    action_description: str,
    session=None,
    audit_enabled=True,
):
    """
    Batch enrich multiple alerts with the same enrichments in a single transaction.

    Args:
        tenant_id (str): The tenant ID to filter the alert enrichments by.
        fingerprints (List[str]): List of alert fingerprints to enrich.
        enrichments (dict): The enrichments to add to all alerts.
        action_type (ActionType): The type of action being performed.
        action_callee (str): The ID of the user performing the action.
        action_description (str): Description of the action.
        session (Session, optional): Database session to use.
        force (bool, optional): Whether to override existing enrichments. Defaults to False.
        audit_enabled (bool, optional): Whether to create audit entries. Defaults to True.

    Returns:
        List[AlertEnrichment]: List of enriched alert objects.
    """
    with existed_or_new_session(session) as session:
        # Get all existing enrichments in one query
        existing_enrichments = {
            e.alert_fingerprint: e
            for e in session.exec(
                select(AlertEnrichment)
                .where(AlertEnrichment.tenant_id == tenant_id)
                .where(AlertEnrichment.alert_fingerprint.in_(fingerprints))
            ).all()
        }

        # Prepare bulk update for existing enrichments
        to_update = {}
        to_create = []
        audit_entries = []

        for fingerprint in fingerprints:
            existing = existing_enrichments.get(fingerprint)

            if existing:
                merged_enrichments = {**existing.enrichments, **enrichments}
                # Preserve existing note if incoming note is empty/None/not provided
                incoming_note = enrichments.get("note")
                if not incoming_note or (
                    isinstance(incoming_note, str) and not incoming_note.strip()
                ):
                    existing_note = existing.enrichments.get("note")
                    if existing_note:
                        merged_enrichments["note"] = existing_note

                # Remove keys with None values (e.g., status=None when undismissing)
                # This allows the alert to revert to its original value from event data
                for key, value in enrichments.items():
                    if value is None and key in merged_enrichments:
                        del merged_enrichments[key]

                to_update[existing.id] = merged_enrichments
            else:
                # For new entries
                to_create.append(
                    AlertEnrichment(
                        tenant_id=tenant_id,
                        alert_fingerprint=fingerprint,
                        enrichments=enrichments,
                    )
                )

            if audit_enabled:
                audit_entries.append(
                    AlertAudit(
                        tenant_id=tenant_id,
                        fingerprint=fingerprint,
                        user_id=action_callee,
                        action=action_type.value,
                        description=action_description,
                    )
                )

        # Update each enrichment individually with merged data
        if to_update:
            for enrichment_id, merged_enrichments in to_update.items():
                stmt = (
                    update(AlertEnrichment)
                    .where(AlertEnrichment.id == enrichment_id)
                    .values(enrichments=merged_enrichments)
                )
                session.execute(stmt)

        # Bulk insert new enrichments
        if to_create:
            session.add_all(to_create)

        # Bulk insert audit entries
        if audit_entries:
            session.add_all(audit_entries)

        session.commit()

        # Get all updated/created enrichments
        result = session.exec(
            select(AlertEnrichment)
            .where(AlertEnrichment.tenant_id == tenant_id)
            .where(AlertEnrichment.alert_fingerprint.in_(fingerprints))
        ).all()

        return result


def enrich_entity(
    tenant_id,
    fingerprint,
    enrichments,
    action_type: ActionType,
    action_callee: str,
    action_description: str,
    session=None,
    force=False,
    audit_enabled=True,
):
    with existed_or_new_session(session) as session:
        return _enrich_entity(
            session,
            tenant_id,
            fingerprint,
            enrichments,
            action_type,
            action_callee,
            action_description,
            force=force,
            audit_enabled=audit_enabled,
        )


def count_alerts(
    provider_type: str,
    provider_id: str,
    ever: bool,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    tenant_id: str,
):
    with Session(engine) as session:
        if ever:
            return (
                session.query(Alert)
                .filter(
                    Alert.tenant_id == tenant_id,
                    Alert.provider_id == provider_id,
                    Alert.provider_type == provider_type,
                )
                .count()
            )
        else:
            return (
                session.query(Alert)
                .filter(
                    Alert.tenant_id == tenant_id,
                    Alert.provider_id == provider_id,
                    Alert.provider_type == provider_type,
                    Alert.timestamp >= start_time,
                    Alert.timestamp <= end_time,
                )
                .count()
            )


def get_enrichment(tenant_id, fingerprint, refresh=False):
    with Session(engine) as session:
        return get_enrichment_with_session(session, tenant_id, fingerprint, refresh)


@retry(exceptions=(Exception,), tries=3, delay=0.1, backoff=2)
def get_enrichment_with_session(session, tenant_id, fingerprint, refresh=False):
    try:
        alert_enrichment = session.exec(
            select(AlertEnrichment)
            .where(AlertEnrichment.tenant_id == tenant_id)
            .where(AlertEnrichment.alert_fingerprint == fingerprint)
        ).first()

        if refresh and alert_enrichment:
            try:
                session.refresh(alert_enrichment)
            except Exception:
                logger.exception(
                    "Failed to refresh enrichment",
                    extra={"tenant_id": tenant_id, "fingerprint": fingerprint},
                )
                session.rollback()
                raise  # This will trigger a retry

        return alert_enrichment

    except Exception as e:
        if "PendingRollbackError" in str(e):
            logger.warning(
                "Session has pending rollback, attempting recovery",
                extra={"tenant_id": tenant_id, "fingerprint": fingerprint},
            )
            session.rollback()
            raise  # This will trigger a retry
        else:
            logger.exception(
                "Unexpected error getting enrichment",
                extra={"tenant_id": tenant_id, "fingerprint": fingerprint},
            )
            raise  # This will trigger a retry


def get_enrichments(
    tenant_id: int, fingerprints: List[str]
) -> List[Optional[AlertEnrichment]]:
    """
    Get a list of alert enrichments for a list of fingerprints using a single DB query.

    :param tenant_id: The tenant ID to filter the alert enrichments by.
    :param fingerprints: A list of fingerprints to get the alert enrichments for.
    :return: A list of AlertEnrichment objects or None for each fingerprint.
    """
    with Session(engine) as session:
        result = session.exec(
            select(AlertEnrichment)
            .where(AlertEnrichment.tenant_id == tenant_id)
            .where(AlertEnrichment.alert_fingerprint.in_(fingerprints))
        ).all()
    return result




def query_alerts(
    tenant_id,
    provider_id=None,
    limit=1000,
    timeframe=None,
    upper_timestamp=None,
    lower_timestamp=None,
    skip_alerts_with_null_timestamp=True,
    sort_ascending=False,
) -> list[Alert]:
    """
    Get all alerts for a given tenant_id.

    Args:
        tenant_id (_type_): The tenant_id to filter the alerts by.
        provider_id (_type_, optional): The provider id to filter by. Defaults to None.
        limit (_type_, optional): The maximum number of alerts to return. Defaults to 1000.
        timeframe (_type_, optional): The number of days to look back for alerts. Defaults to None.
        upper_timestamp (_type_, optional): The upper timestamp to filter by. Defaults to None.
        lower_timestamp (_type_, optional): The lower timestamp to filter by. Defaults to None.

    Returns:
        List[Alert]: A list of Alert objects."""

    with Session(engine) as session:
        # Create the query
        query = session.query(Alert)

        # Apply subqueryload to force-load the alert_enrichment relationship
        query = query.options(subqueryload(Alert.alert_enrichment))

        # Filter by tenant_id
        query = query.filter(Alert.tenant_id == tenant_id)

        # if timeframe is provided, filter the alerts by the timeframe
        if timeframe:
            query = query.filter(
                Alert.timestamp
                >= datetime.now(tz=timezone.utc) - timedelta(days=timeframe)
            )

        filter_conditions = []

        if upper_timestamp is not None:
            filter_conditions.append(Alert.timestamp < upper_timestamp)

        if lower_timestamp is not None:
            filter_conditions.append(Alert.timestamp >= lower_timestamp)

        # Apply the filter conditions
        if filter_conditions:
            query = query.filter(*filter_conditions)  # Unpack and apply all conditions

        if provider_id:
            query = query.filter(Alert.provider_id == provider_id)

        if skip_alerts_with_null_timestamp:
            query = query.filter(Alert.timestamp.isnot(None))

        if sort_ascending:
            query = query.order_by(Alert.timestamp.asc())
        else:
            query = query.order_by(Alert.timestamp.desc())

        if limit:
            query = query.limit(limit)

        # Execute the query
        alerts = query.all()

    return alerts




def get_last_alerts(
    tenant_id,
    provider_id=None,
    limit=1000,
    timeframe=None,
    upper_timestamp=None,
    lower_timestamp=None,
    with_incidents=False,
    fingerprints=None,
) -> list[Alert]:
    with Session(engine) as session:
        dialect_name = session.bind.dialect.name

        # Build the base query using select()
        stmt = (
            select(Alert, LastAlert.first_timestamp.label("startedAt"))
            .select_from(LastAlert)
            .join(Alert, LastAlert.alert_id == Alert.id)
            .where(LastAlert.tenant_id == tenant_id)
            .where(Alert.tenant_id == tenant_id)
        )

        if timeframe:
            stmt = stmt.where(
                LastAlert.timestamp
                >= datetime.now(tz=timezone.utc) - timedelta(days=timeframe)
            )

        # Apply additional filters
        filter_conditions = []

        if upper_timestamp is not None:
            filter_conditions.append(LastAlert.timestamp < upper_timestamp)

        if lower_timestamp is not None:
            filter_conditions.append(LastAlert.timestamp >= lower_timestamp)

        if fingerprints:
            filter_conditions.append(LastAlert.fingerprint.in_(tuple(fingerprints)))

        logger.info(f"filter_conditions: {filter_conditions}")

        if filter_conditions:
            stmt = stmt.where(*filter_conditions)

        # Main query for alerts
        stmt = stmt.options(subqueryload(Alert.alert_enrichment))

        if with_incidents:
            if dialect_name == "sqlite":
                # SQLite version - using JSON
                incidents_subquery = (
                    select(
                        LastAlertToIncident.fingerprint,
                        func.json_group_array(
                            cast(LastAlertToIncident.incident_id, String)
                        ).label("incidents"),
                    )
                    .where(
                        LastAlertToIncident.tenant_id == tenant_id,
                        LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                    )
                    .group_by(LastAlertToIncident.fingerprint)
                    .subquery()
                )

            elif dialect_name == "mysql":
                # MySQL version - using GROUP_CONCAT
                incidents_subquery = (
                    select(
                        LastAlertToIncident.fingerprint,
                        func.group_concat(
                            cast(LastAlertToIncident.incident_id, String)
                        ).label("incidents"),
                    )
                    .where(
                        LastAlertToIncident.tenant_id == tenant_id,
                        LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                    )
                    .group_by(LastAlertToIncident.fingerprint)
                    .subquery()
                )

            elif dialect_name == "postgresql":
                # PostgreSQL version - using string_agg
                incidents_subquery = (
                    select(
                        LastAlertToIncident.fingerprint,
                        func.string_agg(
                            cast(LastAlertToIncident.incident_id, String),
                            ",",
                        ).label("incidents"),
                    )
                    .where(
                        LastAlertToIncident.tenant_id == tenant_id,
                        LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                    )
                    .group_by(LastAlertToIncident.fingerprint)
                    .subquery()
                )
            else:
                raise ValueError(f"Unsupported dialect: {dialect_name}")

            stmt = stmt.add_columns(incidents_subquery.c.incidents)
            stmt = stmt.outerjoin(
                incidents_subquery,
                Alert.fingerprint == incidents_subquery.c.fingerprint,
            )

        if provider_id:
            stmt = stmt.where(Alert.provider_id == provider_id)

        # Order by timestamp in descending order and limit the results
        stmt = stmt.order_by(desc(Alert.timestamp)).limit(limit)

        # Execute the query
        alerts_with_start = session.execute(stmt).all()

        # Process results based on dialect
        alerts = []
        for alert_data in alerts_with_start:
            alert = alert_data[0]
            startedAt = alert_data[1]
            if not alert.event.get("startedAt"):
                alert.event["startedAt"] = str(startedAt)
            else:
                alert.event["firstTimestamp"] = str(startedAt)
            alert.event["event_id"] = str(alert.id)

            if with_incidents:
                incident_id = alert_data[2]
                if dialect_name == "sqlite":
                    # Parse JSON array for SQLite
                    incident_id = json.loads(incident_id)[0] if incident_id else None
                elif dialect_name in ("mysql", "postgresql"):
                    # Split comma-separated string for MySQL and PostgreSQL
                    incident_id = incident_id.split(",")[0] if incident_id else None

                alert.event["incident"] = str(incident_id) if incident_id else None

            alerts.append(alert)

        return alerts


def get_alerts_by_fingerprint(
    tenant_id: str,
    fingerprint: str,
    limit=1,
    status=None,
    with_alert_instance_enrichment=False,
) -> List[Alert]:
    """
    Get all alerts for a given fingerprint.

    Args:
        tenant_id (str): The tenant_id to filter the alerts by.
        fingerprint (str): The fingerprint to filter the alerts by.

    Returns:
        List[Alert]: A list of Alert objects.
    """
    with Session(engine) as session:
        # Create the query using select() instead of session.query()
        query = select(Alert).options(subqueryload(Alert.alert_enrichment))

        if with_alert_instance_enrichment:
            query = query.options(subqueryload(Alert.alert_instance_enrichment))

        # Filter by tenant_id
        query = query.where(Alert.tenant_id == tenant_id)

        query = query.where(Alert.fingerprint == fingerprint)

        query = query.order_by(Alert.timestamp.desc())

        if status:
            query = query.where(
                get_json_extract_field(session, Alert.event, "status") == status
            )

        if limit:
            query = query.limit(limit)

        # Execute the query using exec() instead of execute()
        alerts = session.exec(query).all()

        return alerts


def get_all_alerts_by_fingerprints(
    tenant_id: str, fingerprints: List[str], session: Optional[Session] = None
) -> List[Alert]:
    with existed_or_new_session(session) as session:
        query = (
            select(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.fingerprint.in_(fingerprints))
            .order_by(Alert.timestamp.desc())
        )
        return session.exec(query).all()




def get_alert_by_event_id(
    tenant_id: str, event_id: str, session: Optional[Session] = None
) -> Alert:
    with existed_or_new_session(session) as session:
        query = (
            select(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.id == uuid.UUID(event_id))
        )
        query = query.options(subqueryload(Alert.alert_enrichment))
        alert = session.exec(query).first()
    return alert


def get_alerts_by_ids(
    tenant_id: str, alert_ids: list[str | UUID], session: Optional[Session] = None
) -> List[Alert]:
    with existed_or_new_session(session) as session:
        query = (
            select(Alert)
            .filter(Alert.tenant_id == tenant_id)
            .filter(Alert.id.in_(alert_ids))
        )
        query = query.options(subqueryload(Alert.alert_enrichment))
        return session.exec(query).all()




def get_api_key(api_key: str, include_deleted: bool = False) -> TenantApiKey:
    with Session(engine) as session:
        api_key_hashed = hashlib.sha256(api_key.encode()).hexdigest()
        statement = select(TenantApiKey).where(TenantApiKey.key_hash == api_key_hashed)
        if not include_deleted:
            statement = statement.where(TenantApiKey.is_deleted != True)
        tenant_api_key = session.exec(statement).first()
    return tenant_api_key




# this is only for single tenant
def get_user(username, password, update_sign_in=True):
    from src.models.db.user import User

    password_hash = hashlib.sha256(password.encode()).hexdigest()
    with Session(engine, expire_on_commit=False) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == SINGLE_TENANT_UUID)
            .where(User.username == username)
            .where(User.password_hash == password_hash)
        ).first()
        if user and update_sign_in:
            user.last_sign_in = datetime.utcnow()
            session.add(user)
            session.commit()
    return user


def get_users(tenant_id=None):
    from src.models.db.user import User

    tenant_id = tenant_id or SINGLE_TENANT_UUID

    with Session(engine) as session:
        users = session.exec(select(User).where(User.tenant_id == tenant_id)).all()
    return users


def delete_user(username):
    from src.models.db.user import User

    with Session(engine) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == SINGLE_TENANT_UUID)
            .where(User.username == username)
        ).first()
        if user:
            session.delete(user)
            session.commit()


def user_exists(tenant_id, username):
    from src.models.db.user import User

    with Session(engine) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == tenant_id)
            .where(User.username == username)
        ).first()
        return user is not None


def create_user(tenant_id, username, password, role):
    from src.models.db.user import User

    password_hash = hashlib.sha256(password.encode()).hexdigest()
    with Session(engine) as session:
        user = User(
            tenant_id=tenant_id,
            username=username,
            password_hash=password_hash,
            role=role,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def update_user_last_sign_in(tenant_id, username):
    from src.models.db.user import User

    with Session(engine) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == tenant_id)
            .where(User.username == username)
        ).first()
        if user:
            user.last_sign_in = datetime.utcnow()
            session.add(user)
            session.commit()
    return user


def push_logs_to_db(log_entries):
    # avoid circular import
    from src.utils.logging import LOG_FORMAT, LOG_FORMAT_OPEN_TELEMETRY

    db_log_entries = []
    if LOG_FORMAT == LOG_FORMAT_OPEN_TELEMETRY:
        for log_entry in log_entries:
            try:
                try:
                    # after formatting
                    message = log_entry["message"][0:255]
                except Exception:
                    # before formatting, fallback
                    message = log_entry["msg"][0:255]

                try:
                    timestamp = datetime.strptime(
                        log_entry["asctime"], "%Y-%m-%d %H:%M:%S,%f"
                    )
                except Exception:
                    timestamp = log_entry["created"]

                db_log_entries.append(log_entry)
            except Exception:
                print("Failed to parse log entry - ", log_entry)

    else:
        for log_entry in log_entries:
            try:
                try:
                    # after formatting
                    message = log_entry["message"][0:255]
                except Exception:
                    # before formatting, fallback
                    message = log_entry["msg"][0:255]
                    
                db_log_entries.append(log_entry)
            except Exception:
                print("Failed to parse log entry - ", log_entry)

    # Add the LogEntry instances to the database session
    with Session(engine) as session:
        session.add_all(db_log_entries)
        session.commit()


def update_user_role(tenant_id, username, role):
    from src.models.db.user import User

    with Session(engine) as session:
        user = session.exec(
            select(User)
            .where(User.tenant_id == tenant_id)
            .where(User.username == username)
        ).first()
        if user and user.role != role:
            user.role = role
            session.add(user)
            session.commit()
    return user





def create_rule(
    tenant_id,
    name,
    timeframe,
    timeunit,
    definition,
    definition_cel,
    created_by,
    grouping_criteria=None,
    group_description=None,
    require_approve=False,
    resolve_on=ResolveOn.NEVER.value,
    create_on=CreateIncidentOn.ANY.value,
    incident_name_template=None,
    incident_prefix=None,
    multi_level=False,
    multi_level_property_name=None,
    threshold=1,
    assignee=None,
):
    grouping_criteria = grouping_criteria or []
    with Session(engine) as session:
        rule = Rule(
            tenant_id=tenant_id,
            name=name,
            timeframe=timeframe,
            timeunit=timeunit,
            definition=definition,
            definition_cel=definition_cel,
            created_by=created_by,
            creation_time=datetime.utcnow(),
            grouping_criteria=grouping_criteria,
            group_description=group_description,
            require_approve=require_approve,
            resolve_on=resolve_on,
            create_on=create_on,
            incident_name_template=incident_name_template,
            incident_prefix=incident_prefix,
            multi_level=multi_level,
            multi_level_property_name=multi_level_property_name,
            threshold=threshold,
            assignee=assignee,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule


def update_rule(
    tenant_id,
    rule_id,
    name,
    timeframe,
    timeunit,
    definition,
    definition_cel,
    updated_by,
    grouping_criteria,
    require_approve,
    resolve_on,
    create_on,
    incident_name_template,
    incident_prefix,
    multi_level,
    multi_level_property_name,
    threshold,
    assignee=None,
):
    rule_uuid = __convert_to_uuid(rule_id)
    if not rule_uuid:
        return False

    with Session(engine) as session:
        rule = session.exec(
            select(Rule).where(Rule.tenant_id == tenant_id).where(Rule.id == rule_uuid)
        ).first()

        if rule:
            rule.name = name
            rule.timeframe = timeframe
            rule.timeunit = timeunit
            rule.definition = definition
            rule.definition_cel = definition_cel
            rule.grouping_criteria = grouping_criteria
            rule.require_approve = require_approve
            rule.updated_by = updated_by
            rule.update_time = datetime.utcnow()
            rule.resolve_on = resolve_on
            rule.create_on = create_on
            rule.incident_name_template = incident_name_template
            rule.incident_prefix = incident_prefix
            rule.multi_level = multi_level
            rule.multi_level_property_name = multi_level_property_name
            rule.threshold = threshold
            rule.assignee = assignee
            session.commit()
            session.refresh(rule)
            return rule
        else:
            return None


def get_rules(tenant_id, ids=None) -> list[Rule]:
    with Session(engine) as session:
        # Start building the query
        query = (
            select(Rule)
            .where(Rule.tenant_id == tenant_id)
            .where(Rule.is_deleted.is_(False))
        )

        # Apply additional filters if ids are provided
        if ids is not None:
            query = query.where(Rule.id.in_(ids))

        # Execute the query
        rules = session.exec(query).all()
        return rules




def delete_rule(tenant_id, rule_id):
    with Session(engine) as session:
        rule_uuid = __convert_to_uuid(rule_id)
        if not rule_uuid:
            return False

        rule = session.exec(
            select(Rule).where(Rule.tenant_id == tenant_id).where(Rule.id == rule_uuid)
        ).first()

        if rule and not rule.is_deleted:
            rule.is_deleted = True
            session.commit()
            return True
        return False




def get_rule(tenant_id, rule_id):
    with Session(engine) as session:
        rule = session.exec(
            select(Rule).where(Rule.tenant_id == tenant_id).where(Rule.id == rule_id)
        ).first()
    return rule


def get_rule_incidents_count_db(tenant_id):
    with Session(engine) as session:
        query = (
            session.query(Incident.rule_id, func.count(Incident.id))
            .select_from(Incident)
            .filter(Incident.tenant_id == tenant_id, col(Incident.rule_id).isnot(None))
            .group_by(Incident.rule_id)
        )
        return dict(query.all())


def get_rule_distribution(tenant_id, minute=False):
    """Returns hits per hour for each rule, optionally breaking down by groups if the rule has 'group by', limited to the last 7 days."""
    with Session(engine) as session:
        # Get the timestamp for 7 days ago
        seven_days_ago = datetime.utcnow() - timedelta(days=1)

        # Check the dialect
        if session.bind.dialect.name == "mysql":
            time_format = "%Y-%m-%d %H:%i" if minute else "%Y-%m-%d %H"
            timestamp_format = func.date_format(
                LastAlertToIncident.timestamp, time_format
            )
        elif session.bind.dialect.name == "postgresql":
            time_format = "YYYY-MM-DD HH:MI" if minute else "YYYY-MM-DD HH"
            timestamp_format = func.to_char(LastAlertToIncident.timestamp, time_format)
        elif session.bind.dialect.name == "sqlite":
            time_format = "%Y-%m-%d %H:%M" if minute else "%Y-%m-%d %H"
            timestamp_format = func.strftime(time_format, LastAlertToIncident.timestamp)
        else:
            raise ValueError("Unsupported database dialect")
        # Construct the query
        query = (
            session.query(
                Rule.id.label("rule_id"),
                Rule.name.label("rule_name"),
                Incident.id.label("incident_id"),
                Incident.rule_fingerprint.label("rule_fingerprint"),
                timestamp_format.label("time"),
                func.count(LastAlertToIncident.fingerprint).label("hits"),
            )
            .join(Incident, Rule.id == Incident.rule_id)
            .join(LastAlertToIncident, Incident.id == LastAlertToIncident.incident_id)
            .filter(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.timestamp >= seven_days_ago,
            )
            .filter(Rule.tenant_id == tenant_id)  # Filter by tenant_id
            .group_by(
                Rule.id, "rule_name", Incident.id, "rule_fingerprint", "time"
            )  # Adjusted here
            .order_by("time")
        )

        results = query.all()

        # Convert the results into a dictionary
        rule_distribution = {}
        for result in results:
            rule_id = result.rule_id
            rule_fingerprint = result.rule_fingerprint
            timestamp = result.time
            hits = result.hits

            if rule_id not in rule_distribution:
                rule_distribution[rule_id] = {}

            if rule_fingerprint not in rule_distribution[rule_id]:
                rule_distribution[rule_id][rule_fingerprint] = {}

            rule_distribution[rule_id][rule_fingerprint][timestamp] = hits

        return rule_distribution


def get_all_deduplication_rules(tenant_id):
    with Session(engine) as session:
        rules = session.exec(
            select(AlertDeduplicationRule).where(
                AlertDeduplicationRule.tenant_id == tenant_id
            )
        ).all()
    return rules


def get_deduplication_rule_by_id(tenant_id, rule_id: str):
    rule_uuid = __convert_to_uuid(rule_id)
    if not rule_uuid:
        return None

    with Session(engine) as session:
        rules = session.exec(
            select(AlertDeduplicationRule)
            .where(AlertDeduplicationRule.tenant_id == tenant_id)
            .where(AlertDeduplicationRule.id == rule_uuid)
        ).first()
    return rules


def get_custom_deduplication_rule(tenant_id, provider_id, provider_type):
    with Session(engine) as session:
        rule = session.exec(
            select(AlertDeduplicationRule)
            .where(AlertDeduplicationRule.tenant_id == tenant_id)
            .where(AlertDeduplicationRule.provider_id == provider_id)
            .where(AlertDeduplicationRule.provider_type == provider_type)
        ).one_or_none()
        return rule


def create_deduplication_rule(
    tenant_id: str,
    name: str,
    description: str,
    provider_id: str | None,
    provider_type: str,
    created_by: str,
    enabled: bool = True,
    fingerprint_fields: list[str] = [],
    full_deduplication: bool = False,
    ignore_fields: list[str] = [],
    priority: int = 0,
    is_provisioned: bool = False,
):
    with Session(engine) as session:
        new_rule = AlertDeduplicationRule(
            tenant_id=tenant_id,
            name=name,
            description=description,
            provider_id=provider_id,
            provider_type=provider_type,
            last_updated_by=created_by,  # on creation, last_updated_by is the same as created_by
            created_by=created_by,
            enabled=enabled,
            fingerprint_fields=fingerprint_fields,
            full_deduplication=full_deduplication,
            ignore_fields=ignore_fields,
            priority=priority,
            is_provisioned=is_provisioned,
        )
        session.add(new_rule)
        session.commit()
        session.refresh(new_rule)
    return new_rule


def update_deduplication_rule(
    rule_id: str,
    tenant_id: str,
    name: str,
    description: str,
    provider_id: str | None,
    provider_type: str,
    last_updated_by: str,
    enabled: bool = True,
    fingerprint_fields: list[str] = [],
    full_deduplication: bool = False,
    ignore_fields: list[str] = [],
    priority: int = 0,
):
    rule_uuid = __convert_to_uuid(rule_id)
    if not rule_uuid:
        return False

    with Session(engine) as session:
        rule = session.exec(
            select(AlertDeduplicationRule)
            .where(AlertDeduplicationRule.id == rule_uuid)
            .where(AlertDeduplicationRule.tenant_id == tenant_id)
        ).first()
        if not rule:
            raise ValueError(f"No deduplication rule found with id {rule_id}")

        rule.name = name
        rule.description = description
        rule.provider_id = provider_id
        rule.provider_type = provider_type
        rule.last_updated_by = last_updated_by
        rule.enabled = enabled
        rule.fingerprint_fields = fingerprint_fields
        rule.full_deduplication = full_deduplication
        rule.ignore_fields = ignore_fields
        rule.priority = priority

        session.add(rule)
        session.commit()
        session.refresh(rule)
    return rule


def delete_deduplication_rule(rule_id: str, tenant_id: str) -> bool:
    rule_uuid = __convert_to_uuid(rule_id)
    if not rule_uuid:
        return False

    with Session(engine) as session:
        rule = session.exec(
            select(AlertDeduplicationRule)
            .where(AlertDeduplicationRule.id == rule_uuid)
            .where(AlertDeduplicationRule.tenant_id == tenant_id)
        ).first()
        if not rule:
            return False

        session.delete(rule)
        session.commit()
    return True


def create_deduplication_event(
    tenant_id, deduplication_rule_id, deduplication_type, provider_id, provider_type
):
    logger.debug(
        "Adding deduplication event",
        extra={
            "deduplication_rule_id": deduplication_rule_id,
            "deduplication_type": deduplication_type,
            "provider_id": provider_id,
            "provider_type": provider_type,
            "tenant_id": tenant_id,
        },
    )
    if isinstance(deduplication_rule_id, str):
        deduplication_rule_id = __convert_to_uuid(deduplication_rule_id)
        if not deduplication_rule_id:
            logger.debug(
                "Deduplication rule id is not a valid uuid",
                extra={
                    "deduplication_rule_id": deduplication_rule_id,
                    "tenant_id": tenant_id,
                },
            )
            return False
    with Session(engine) as session:
        deduplication_event = AlertDeduplicationEvent(
            tenant_id=tenant_id,
            deduplication_rule_id=deduplication_rule_id,
            deduplication_type=deduplication_type,
            provider_id=provider_id,
            provider_type=provider_type,
            timestamp=datetime.now(tz=timezone.utc),
            date_hour=datetime.now(tz=timezone.utc).replace(
                minute=0, second=0, microsecond=0
            ),
        )
        session.add(deduplication_event)
        session.commit()
        logger.debug(
            "Deduplication event added",
            extra={
                "deduplication_event_id": deduplication_event.id,
                "tenant_id": tenant_id,
            },
        )


def get_all_deduplication_stats(tenant_id):
    with Session(engine) as session:
        # Query to get all-time deduplication stats
        all_time_query = (
            select(
                AlertDeduplicationEvent.deduplication_rule_id,
                AlertDeduplicationEvent.provider_id,
                AlertDeduplicationEvent.provider_type,
                AlertDeduplicationEvent.deduplication_type,
                func.count(AlertDeduplicationEvent.id).label("dedup_count"),
            )
            .where(AlertDeduplicationEvent.tenant_id == tenant_id)
            .group_by(
                AlertDeduplicationEvent.deduplication_rule_id,
                AlertDeduplicationEvent.provider_id,
                AlertDeduplicationEvent.provider_type,
                AlertDeduplicationEvent.deduplication_type,
            )
        )

        all_time_results = session.exec(all_time_query).all()

        # Query to get alerts distribution in the last 24 hours
        twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
        alerts_last_24_hours_query = (
            select(
                AlertDeduplicationEvent.deduplication_rule_id,
                AlertDeduplicationEvent.provider_id,
                AlertDeduplicationEvent.provider_type,
                AlertDeduplicationEvent.date_hour,
                func.count(AlertDeduplicationEvent.id).label("hourly_count"),
            )
            .where(AlertDeduplicationEvent.tenant_id == tenant_id)
            .where(AlertDeduplicationEvent.date_hour >= twenty_four_hours_ago)
            .group_by(
                AlertDeduplicationEvent.deduplication_rule_id,
                AlertDeduplicationEvent.provider_id,
                AlertDeduplicationEvent.provider_type,
                AlertDeduplicationEvent.date_hour,
            )
        )

        alerts_last_24_hours_results = session.exec(alerts_last_24_hours_query).all()

        # Create a dictionary with deduplication stats for each rule
        stats = {}
        current_hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
        for result in all_time_results:
            provider_id = result.provider_id
            provider_type = result.provider_type
            dedup_count = result.dedup_count
            dedup_type = result.deduplication_type

            # alerts without provider_id and provider_type are considered as "keep"
            if not provider_type:
                provider_type = "keep"

            key = str(result.deduplication_rule_id)
            if key not in stats:
                # initialize the stats for the deduplication rule
                stats[key] = {
                    "full_dedup_count": 0,
                    "partial_dedup_count": 0,
                    "none_dedup_count": 0,
                    "alerts_last_24_hours": [
                        {"hour": (current_hour - timedelta(hours=i)).hour, "number": 0}
                        for i in range(0, 24)
                    ],
                    "provider_id": provider_id,
                    "provider_type": provider_type,
                }

            if dedup_type == "full":
                stats[key]["full_dedup_count"] += dedup_count
            elif dedup_type == "partial":
                stats[key]["partial_dedup_count"] += dedup_count
            elif dedup_type == "none":
                stats[key]["none_dedup_count"] += dedup_count

        # Add alerts distribution from the last 24 hours
        for result in alerts_last_24_hours_results:
            provider_id = result.provider_id
            provider_type = result.provider_type
            date_hour = result.date_hour
            hourly_count = result.hourly_count
            key = str(result.deduplication_rule_id)

            if not provider_type:
                provider_type = "keep"

            if key in stats:
                hours_ago = int((current_hour - date_hour).total_seconds() / 3600)
                if 0 <= hours_ago < 24:
                    stats[key]["alerts_last_24_hours"][23 - hours_ago]["number"] = (
                        hourly_count
                    )

    return stats


def get_last_alert_hashes_by_fingerprints(
    tenant_id, fingerprints: list[str]
) -> dict[str, str | None]:
    # get the last alert hashes for a list of fingerprints
    # to check deduplication
    with Session(engine) as session:
        query = (
            select(LastAlert.fingerprint, LastAlert.alert_hash)
            .where(LastAlert.tenant_id == tenant_id)
            .where(LastAlert.fingerprint.in_(fingerprints))
        )

        results = session.exec(query).all()

        # Create a dictionary from the results
        alert_hash_dict = {
            fingerprint: alert_hash
            for fingerprint, alert_hash in results
            if alert_hash is not None
        }
        return alert_hash_dict


def update_key_last_used(
    tenant_id: str,
    reference_id: str,
    max_retries=3,
) -> str:
    """
    Updates API key last used.

    Args:
        session (Session): _description_
        tenant_id (str): _description_
        reference_id (str): _description_

    Returns:
        str: _description_
    """
    with Session(engine) as session:
        # Get API Key from database
        statement = (
            select(TenantApiKey)
            .where(TenantApiKey.reference_id == reference_id)
            .where(TenantApiKey.tenant_id == tenant_id)
        )

        tenant_api_key_entry = session.exec(statement).first()

        # Update last used
        if not tenant_api_key_entry:
            # shouldn't happen but somehow happened to specific tenant so logging it
            logger.error(
                "API key not found",
                extra={"tenant_id": tenant_id, "unique_api_key_id": reference_id},
            )
            return
        tenant_api_key_entry.last_used = datetime.utcnow()

        for attempt in range(max_retries):
            try:
                session.add(tenant_api_key_entry)
                session.commit()
                break
            except StaleDataError as ex:
                if "expected to update" in ex.args[0]:
                    logger.info(
                        f"Phantom read detected while updating API key `{reference_id}`, retry #{attempt}"
                    )
                    session.rollback()
                    continue
                else:
                    raise


def get_linked_providers(tenant_id: str) -> List[Tuple[str, str, datetime]]:
    # Alert table may be too huge, so cutting the query without mercy
    LIMIT_BY_ALERTS = 10000

    with Session(engine) as session:
        alerts_subquery = (
            select(Alert)
            .filter(Alert.tenant_id == tenant_id, Alert.provider_type != "group")
            .limit(LIMIT_BY_ALERTS)
            .subquery()
        )

        providers = session.exec(
            select(
                alerts_subquery.c.provider_type,
                alerts_subquery.c.provider_id,
                func.max(alerts_subquery.c.timestamp).label("last_alert_timestamp"),
            )
            .select_from(alerts_subquery)
            .filter(~exists().where(Provider.id == alerts_subquery.c.provider_id))
            .group_by(alerts_subquery.c.provider_type, alerts_subquery.c.provider_id)
        ).all()

    return providers


def is_linked_provider(tenant_id: str, provider_id: str) -> bool:
    with Session(engine) as session:
        query = session.query(Alert.provider_id)

        # Add FORCE INDEX hint only for MySQL
        if engine.dialect.name == "mysql":
            query = query.with_hint(Alert, "FORCE INDEX (idx_alert_tenant_provider)")

        linked_provider = (
            query.outerjoin(Provider, Alert.provider_id == Provider.id)
            .filter(
                Alert.tenant_id == tenant_id,
                Alert.provider_id == provider_id,
                Provider.id == None,
            )
            .first()
        )

    return linked_provider is not None


def get_provider_distribution(
    tenant_id: str,
    aggregate_all: bool = False,
    timestamp_filter: TimeStampFilter = None,
) -> (
    list[dict[str, int | Any]]
    | dict[str, dict[str, datetime | list[dict[str, int]] | Any]]
):
    """
    Calculate the distribution of incidents created over time for a specific tenant.

    Args:
        tenant_id (str): ID of the tenant whose incidents are being queried.
        timestamp_filter (TimeStampFilter, optional): Filter to specify the time range.
            - lower_timestamp (datetime): Start of the time range.
            - upper_timestamp (datetime): End of the time range.

    Returns:
        List[dict]: A list of dictionaries representing the hourly distribution of incidents.
            Each dictionary contains:
            - 'timestamp' (str): Timestamp of the hour in "YYYY-MM-DD HH:00" format.
            - 'number' (int): Number of incidents created in that hour.

    Notes:
        - If no timestamp_filter is provided, defaults to the last 24 hours.
        - Supports MySQL, PostgreSQL, and SQLite for timestamp formatting.
    """
    with Session(engine) as session:
        twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
        time_format = "%Y-%m-%d %H"

        filters = [Alert.tenant_id == tenant_id]

        if timestamp_filter:
            if timestamp_filter.lower_timestamp:
                filters.append(Alert.timestamp >= timestamp_filter.lower_timestamp)
            if timestamp_filter.upper_timestamp:
                filters.append(Alert.timestamp <= timestamp_filter.upper_timestamp)
        else:
            filters.append(Alert.timestamp >= twenty_four_hours_ago)

        if session.bind.dialect.name == "mysql":
            timestamp_format = func.date_format(Alert.timestamp, time_format)
        elif session.bind.dialect.name == "postgresql":
            # PostgreSQL requires a different syntax for the timestamp format
            # cf: https://www.postgresql.org/docs/current/functions-formatting.html#FUNCTIONS-FORMATTING
            timestamp_format = func.to_char(Alert.timestamp, "YYYY-MM-DD HH")
        elif session.bind.dialect.name == "sqlite":
            timestamp_format = func.strftime(time_format, Alert.timestamp)

        if aggregate_all:
            # Query for combined alert distribution across all providers
            query = (
                session.query(
                    timestamp_format.label("time"), func.count().label("hits")
                )
                .filter(*filters)
                .group_by("time")
                .order_by("time")
            )

            results = query.all()

            results = {str(time): hits for time, hits in results}

            # Create a complete list of timestamps within the specified range
            distribution = []
            current_time = timestamp_filter.lower_timestamp.replace(
                minute=0, second=0, microsecond=0
            )
            while current_time <= timestamp_filter.upper_timestamp:
                timestamp_str = current_time.strftime(time_format)
                distribution.append(
                    {
                        "timestamp": timestamp_str + ":00",
                        "number": results.get(timestamp_str, 0),
                    }
                )
                current_time += timedelta(hours=1)
            return distribution

        else:
            # Query for alert distribution grouped by provider
            query = (
                session.query(
                    Alert.provider_id,
                    Alert.provider_type,
                    timestamp_format.label("time"),
                    func.count().label("hits"),
                    func.max(Alert.timestamp).label("last_alert_timestamp"),
                )
                .filter(*filters)
                .group_by(Alert.provider_id, Alert.provider_type, "time")
                .order_by(Alert.provider_id, Alert.provider_type, "time")
            )

            results = query.all()

            provider_distribution = {}

            for provider_id, provider_type, time, hits, last_alert_timestamp in results:
                provider_key = f"{provider_id}_{provider_type}"
                last_alert_timestamp = (
                    datetime.fromisoformat(last_alert_timestamp)
                    if isinstance(last_alert_timestamp, str)
                    else last_alert_timestamp
                )

                if provider_key not in provider_distribution:
                    provider_distribution[provider_key] = {
                        "provider_id": provider_id,
                        "provider_type": provider_type,
                        "alert_last_24_hours": [
                            {"hour": i, "number": 0} for i in range(24)
                        ],
                        "last_alert_received": last_alert_timestamp,
                    }
                else:
                    provider_distribution[provider_key]["last_alert_received"] = max(
                        provider_distribution[provider_key]["last_alert_received"],
                        last_alert_timestamp,
                    )

                time = datetime.strptime(time, time_format)
                index = int((time - twenty_four_hours_ago).total_seconds() // 3600)

                if 0 <= index < 24:
                    provider_distribution[provider_key]["alert_last_24_hours"][index][
                        "number"
                    ] += hits

            return provider_distribution




def get_incidents_created_distribution(
    tenant_id: str, timestamp_filter: TimeStampFilter = None
):
    """
    Calculate the distribution of incidents created over time for a specific tenant.

    Args:
        tenant_id (str): ID of the tenant whose incidents are being queried.
        timestamp_filter (TimeStampFilter, optional): Filter to specify the time range.
            - lower_timestamp (datetime): Start of the time range.
            - upper_timestamp (datetime): End of the time range.

    Returns:
        List[dict]: A list of dictionaries representing the hourly distribution of incidents.
            Each dictionary contains:
            - 'timestamp' (str): Timestamp of the hour in "YYYY-MM-DD HH:00" format.
            - 'number' (int): Number of incidents created in that hour.

    Notes:
        - If no timestamp_filter is provided, defaults to the last 24 hours.
        - Supports MySQL, PostgreSQL, and SQLite for timestamp formatting.
    """
    with Session(engine) as session:
        twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
        time_format = "%Y-%m-%d %H"

        filters = [Incident.tenant_id == tenant_id]

        if timestamp_filter:
            if timestamp_filter.lower_timestamp:
                filters.append(
                    Incident.creation_time >= timestamp_filter.lower_timestamp
                )
            if timestamp_filter.upper_timestamp:
                filters.append(
                    Incident.creation_time <= timestamp_filter.upper_timestamp
                )
        else:
            filters.append(Incident.creation_time >= twenty_four_hours_ago)

        # Database-specific timestamp formatting
        if session.bind.dialect.name == "mysql":
            timestamp_format = func.date_format(Incident.creation_time, time_format)
        elif session.bind.dialect.name == "postgresql":
            timestamp_format = func.to_char(Incident.creation_time, "YYYY-MM-DD HH")
        elif session.bind.dialect.name == "sqlite":
            timestamp_format = func.strftime(time_format, Incident.creation_time)

        query = (
            session.query(
                timestamp_format.label("time"), func.count().label("incidents")
            )
            .filter(*filters)
            .group_by("time")
            .order_by("time")
        )

        results = {str(time): incidents for time, incidents in query.all()}

        distribution = []
        current_time = timestamp_filter.lower_timestamp.replace(
            minute=0, second=0, microsecond=0
        )
        while current_time <= timestamp_filter.upper_timestamp:
            timestamp_str = current_time.strftime(time_format)
            distribution.append(
                {
                    "timestamp": timestamp_str + ":00",
                    "number": results.get(timestamp_str, 0),
                }
            )
            current_time += timedelta(hours=1)

        return distribution


def calc_incidents_mttr(tenant_id: str, timestamp_filter: TimeStampFilter = None):
    """
    Calculate the Mean Time to Resolve (MTTR) for incidents over time for a specific tenant.

    Args:
        tenant_id (str): ID of the tenant whose incidents are being analyzed.
        timestamp_filter (TimeStampFilter, optional): Filter to specify the time range.
            - lower_timestamp (datetime): Start of the time range.
            - upper_timestamp (datetime): End of the time range.

    Returns:
        List[dict]: A list of dictionaries representing the hourly MTTR of incidents.
            Each dictionary contains:
            - 'timestamp' (str): Timestamp of the hour in "YYYY-MM-DD HH:00" format.
            - 'mttr' (float): Mean Time to Resolve incidents in that hour (in hours).

    Notes:
        - If no timestamp_filter is provided, defaults to the last 24 hours.
        - Only includes resolved incidents.
        - Supports MySQL, PostgreSQL, and SQLite for timestamp formatting.
    """
    with Session(engine) as session:
        twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
        time_format = "%Y-%m-%d %H"

        filters = [
            Incident.tenant_id == tenant_id,
            Incident.status == IncidentStatus.RESOLVED.value,
        ]
        if timestamp_filter:
            if timestamp_filter.lower_timestamp:
                filters.append(
                    Incident.creation_time >= timestamp_filter.lower_timestamp
                )
            if timestamp_filter.upper_timestamp:
                filters.append(
                    Incident.creation_time <= timestamp_filter.upper_timestamp
                )
        else:
            filters.append(Incident.creation_time >= twenty_four_hours_ago)

        # Database-specific timestamp formatting
        if session.bind.dialect.name == "mysql":
            timestamp_format = func.date_format(Incident.creation_time, time_format)
        elif session.bind.dialect.name == "postgresql":
            timestamp_format = func.to_char(Incident.creation_time, "YYYY-MM-DD HH")
        elif session.bind.dialect.name == "sqlite":
            timestamp_format = func.strftime(time_format, Incident.creation_time)

        query = (
            session.query(
                timestamp_format.label("time"),
                Incident.start_time,
                Incident.end_time,
                func.count().label("incidents"),
            )
            .filter(*filters)
            .group_by("time", Incident.start_time, Incident.end_time)
            .order_by("time")
        )
        results = {}
        for time, start_time, end_time, incidents in query.all():
            if start_time and end_time:
                resolution_time = (
                    end_time - start_time
                ).total_seconds() / 3600  # in hours
                time_str = str(time)
                if time_str not in results:
                    results[time_str] = {"number": 0, "mttr": 0}

                results[time_str]["number"] += incidents
                results[time_str]["mttr"] += resolution_time * incidents

        distribution = []
        current_time = timestamp_filter.lower_timestamp.replace(
            minute=0, second=0, microsecond=0
        )
        while current_time <= timestamp_filter.upper_timestamp:
            timestamp_str = current_time.strftime(time_format)
            if timestamp_str in results and results[timestamp_str]["number"] > 0:
                avg_mttr = (
                    results[timestamp_str]["mttr"] / results[timestamp_str]["number"]
                )
            else:
                avg_mttr = 0

            distribution.append(
                {
                    "timestamp": timestamp_str + ":00",
                    "mttr": avg_mttr,
                }
            )
            current_time += timedelta(hours=1)

        return distribution


def get_presets(
    tenant_id: str, email, preset_ids: list[str] = None
) -> List[Dict[str, Any]]:
    with Session(engine) as session:
        # v2 with RBAC and roles
        if preset_ids:
            statement = (
                select(Preset)
                .where(Preset.tenant_id == tenant_id)
                .where(Preset.id.in_(preset_ids))
            )
        # v1, no RBAC and roles
        else:
            statement = (
                select(Preset)
                .where(Preset.tenant_id == tenant_id)
                .where(
                    or_(
                        Preset.is_private == False,
                        Preset.created_by == email,
                    )
                )
            )
        result = session.exec(statement)
        presets = result.unique().all()

    return presets


def get_db_preset_by_name(tenant_id: str, preset_name: str) -> Preset | None:
    with Session(engine) as session:
        preset = session.exec(
            select(Preset)
            .where(Preset.tenant_id == tenant_id)
            .where(Preset.name == preset_name)
        ).first()
    return preset




def get_dashboards(tenant_id: str, email=None) -> List[Dict[str, Any]]:
    with Session(engine) as session:
        statement = (
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .where(
                or_(
                    Dashboard.is_private == False,
                    Dashboard.created_by == email,
                )
            )
        )
        dashboards = session.exec(statement).all()

    # for postgres, the jsonb column is returned as a string
    # so we need to parse it
    for dashboard in dashboards:
        if isinstance(dashboard.dashboard_config, str):
            dashboard.dashboard_config = json.loads(dashboard.dashboard_config)
    return dashboards


def create_dashboard(
    tenant_id, dashboard_name, created_by, dashboard_config, is_private=False
):
    with Session(engine) as session:
        dashboard = Dashboard(
            tenant_id=tenant_id,
            dashboard_name=dashboard_name,
            dashboard_config=dashboard_config,
            created_by=created_by,
            is_private=is_private,
        )
        session.add(dashboard)
        session.commit()
        session.refresh(dashboard)
        return dashboard


def update_dashboard(
    tenant_id, dashboard_id, dashboard_name, dashboard_config, updated_by
):
    with Session(engine) as session:
        dashboard = session.exec(
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .where(Dashboard.id == dashboard_id)
        ).first()

        if not dashboard:
            return None

        if dashboard_name:
            dashboard.dashboard_name = dashboard_name

        if dashboard_config:
            dashboard.dashboard_config = dashboard_config

        dashboard.updated_by = updated_by
        dashboard.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(dashboard)
        return dashboard


def delete_dashboard(tenant_id, dashboard_id):
    with Session(engine) as session:
        dashboard = session.exec(
            select(Dashboard)
            .where(Dashboard.tenant_id == tenant_id)
            .where(Dashboard.id == dashboard_id)
        ).first()

        if dashboard:
            session.delete(dashboard)
            session.commit()
            return True
        return False




def get_tenants():
    with Session(engine) as session:
        tenants = session.exec(select(Tenant)).all()
        return tenants


def get_tenants_configurations(only_with_config=False) -> dict:
    with Session(engine) as session:
        try:
            tenants = session.exec(select(Tenant)).all()
        # except column configuration does not exist (new column added)
        except OperationalError as e:
            if "Unknown column" in str(e):
                logger.warning("Column configuration does not exist in the database")
                return {}
            else:
                logger.exception("Failed to get tenants configurations")
                return {}

    tenants_configurations = {}
    for tenant in tenants:
        if only_with_config and not tenant.configuration:
            continue
        tenants_configurations[tenant.id] = tenant.configuration or {}

    return tenants_configurations


def update_preset_options(tenant_id: str, preset_id: str, options: dict) -> Preset:
    if isinstance(preset_id, str):
        preset_id = __convert_to_uuid(preset_id)

    with Session(engine) as session:
        preset = session.exec(
            select(Preset)
            .where(Preset.tenant_id == tenant_id)
            .where(Preset.id == preset_id)
        ).first()

        stmt = (
            update(Preset)
            .where(Preset.id == preset_id)
            .where(Preset.tenant_id == tenant_id)
            .values(options=options)
        )
        session.execute(stmt)
        session.commit()
        session.refresh(preset)
    return preset


def assign_alert_to_incident(
    fingerprint: str,
    incident: Incident,
    tenant_id: str,
    session: Optional[Session] = None,
):
    return add_alerts_to_incident(tenant_id, incident, [fingerprint], session=session)




def get_alert_audit(
    tenant_id: str, fingerprint: str | list[str], limit: int = 50
) -> List[AlertAudit]:
    """
    Get the alert audit for the given fingerprint(s).

    Args:
        tenant_id (str): the tenant_id to filter the alert audit by
        fingerprint (str | list[str]): the fingerprint(s) to filter the alert audit by
        limit (int, optional): the maximum number of alert audits to return. Defaults to 50.

    Returns:
        List[AlertAudit]: the alert audit for the given fingerprint(s)
    """
    with Session(engine) as session:
        if isinstance(fingerprint, list):
            query = (
                select(AlertAudit)
                .where(AlertAudit.tenant_id == tenant_id)
                .where(AlertAudit.fingerprint.in_(fingerprint))
                .order_by(desc(AlertAudit.timestamp), AlertAudit.fingerprint)
            )
            if limit:
                query = query.limit(limit)
        else:
            query = (
                select(AlertAudit)
                .where(AlertAudit.tenant_id == tenant_id)
                .where(AlertAudit.fingerprint == fingerprint)
                .order_by(desc(AlertAudit.timestamp))
                .limit(limit)
            )

        # Execute the query and fetch all results
        result = session.execute(query).scalars().all()

    return result


def get_incidents_meta_for_tenant(tenant_id: str) -> dict:
    with Session(engine) as session:
        if session.bind.dialect.name == "sqlite":
            sources_join = func.json_each(Incident.sources).table_valued("value")
            affected_services_join = func.json_each(
                Incident.affected_services
            ).table_valued("value")

            query = (
                select(
                    func.json_group_array(col(Incident.assignee).distinct()).label(
                        "assignees"
                    ),
                    func.json_group_array(sources_join.c.value.distinct()).label(
                        "sources"
                    ),
                    func.json_group_array(
                        affected_services_join.c.value.distinct()
                    ).label("affected_services"),
                )
                .select_from(Incident)
                .outerjoin(sources_join, sources_join.c.value.isnot(None))
                .outerjoin(
                    affected_services_join, affected_services_join.c.value.isnot(None)
                )
                .filter(Incident.tenant_id == tenant_id, Incident.is_visible == True)
            )
            results = session.exec(query).one_or_none()

            if not results:
                return {}

            return {
                "assignees": list(filter(bool, json.loads(results.assignees))),
                "sources": list(filter(bool, json.loads(results.sources))),
                "services": list(filter(bool, json.loads(results.affected_services))),
            }

        elif session.bind.dialect.name == "mysql":
            sources_join = func.json_table(
                Incident.sources, Column("value", String(127))
            ).table_valued("value")
            affected_services_join = func.json_table(
                Incident.affected_services, Column("value", String(127))
            ).table_valued("value")

            query = (
                select(
                    func.group_concat(col(Incident.assignee).distinct()).label(
                        "assignees"
                    ),
                    func.group_concat(sources_join.c.value.distinct()).label("sources"),
                    func.group_concat(affected_services_join.c.value.distinct()).label(
                        "affected_services"
                    ),
                )
                .select_from(Incident)
                .outerjoin(sources_join, sources_join.c.value.isnot(None))
                .outerjoin(
                    affected_services_join, affected_services_join.c.value.isnot(None)
                )
                .filter(Incident.tenant_id == tenant_id, Incident.is_visible == True)
            )

            results = session.exec(query).one_or_none()

            if not results:
                return {}

            return {
                "assignees": results.assignees.split(",") if results.assignees else [],
                "sources": results.sources.split(",") if results.sources else [],
                "services": (
                    results.affected_services.split(",")
                    if results.affected_services
                    else []
                ),
            }
        elif session.bind.dialect.name == "postgresql":
            sources_join = func.json_array_elements_text(Incident.sources).table_valued(
                "value"
            )
            affected_services_join = func.json_array_elements_text(
                Incident.affected_services
            ).table_valued("value")

            query = (
                select(
                    func.json_agg(col(Incident.assignee).distinct()).label("assignees"),
                    func.json_agg(sources_join.c.value.distinct()).label("sources"),
                    func.json_agg(affected_services_join.c.value.distinct()).label(
                        "affected_services"
                    ),
                )
                .select_from(Incident)
                .outerjoin(sources_join, sources_join.c.value.isnot(None))
                .outerjoin(
                    affected_services_join, affected_services_join.c.value.isnot(None)
                )
                .filter(Incident.tenant_id == tenant_id, Incident.is_visible == True)
            )

            results = session.exec(query).one_or_none()
            if not results:
                return {}

            assignees, sources, affected_services = results

            return {
                "assignees": list(filter(bool, assignees)) if assignees else [],
                "sources": list(filter(bool, sources)) if sources else [],
                "services": (
                    list(filter(bool, affected_services)) if affected_services else []
                ),
            }
        return {}


def apply_incident_filters(session: Session, filters: dict, query):
    for field_name, value in filters.items():
        if field_name in ALLOWED_INCIDENT_FILTERS:
            if field_name in ["affected_services", "sources"]:
                field = getattr(Incident, field_name)

                # Rare case with empty values
                if isinstance(value, list) and not any(value):
                    continue

                query = filter_query(session, query, field, value)

            else:
                field = getattr(Incident, field_name)
                if isinstance(value, list):
                    query = query.filter(col(field).in_(value))
                else:
                    query = query.filter(col(field) == value)
    return query


def filter_query(session: Session, query, field, value):
    if session.bind.dialect.name in ["mysql", "postgresql"]:
        if isinstance(value, list):
            if session.bind.dialect.name == "mysql":
                query = query.filter(func.json_overlaps(field, func.json_array(value)))
            else:
                query = query.filter(col(field).op("?|")(func.array(value)))

        else:
            query = query.filter(func.json_contains(field, value))

    elif session.bind.dialect.name == "sqlite":
        json_each_alias = func.json_each(field).table_valued("value")
        subquery = select(1).select_from(json_each_alias)
        if isinstance(value, list):
            subquery = subquery.where(json_each_alias.c.value.in_(value))
        else:
            subquery = subquery.where(json_each_alias.c.value == value)

        query = query.filter(subquery.exists())
    return query


def enrich_incidents_with_alerts(
    tenant_id: str, incidents: List[Incident], session: Optional[Session] = None
):
    with existed_or_new_session(session) as session:
        incident_alerts = session.exec(
            select(LastAlertToIncident.incident_id, Alert)
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlertToIncident.tenant_id == LastAlert.tenant_id,
                    LastAlertToIncident.fingerprint == LastAlert.fingerprint,
                    LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .where(
                LastAlert.tenant_id == tenant_id,
                LastAlertToIncident.incident_id.in_(
                    [incident.id for incident in incidents]
                ),
            )
        ).all()

        alerts_per_incident = defaultdict(list)
        for incident_id, alert in incident_alerts:
            alerts_per_incident[incident_id].append(alert)

        for incident in incidents:
            incident._alerts = alerts_per_incident[incident.id]

        return incidents


def enrich_alerts_with_incidents(
    tenant_id: str, alerts: List[Alert], session: Optional[Session] = None
):
    with existed_or_new_session(session) as session:
        alert_incidents = session.exec(
            select(LastAlertToIncident.fingerprint, Incident)
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlertToIncident.tenant_id == LastAlert.tenant_id,
                    LastAlertToIncident.fingerprint == LastAlert.fingerprint,
                    LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                ),
            )
            .join(Incident, LastAlertToIncident.incident_id == Incident.id)
            .where(
                LastAlert.tenant_id == tenant_id,
                LastAlertToIncident.fingerprint.in_(
                    [alert.fingerprint for alert in alerts]
                ),
            )
        ).all()

        incidents_per_alert = defaultdict(list)
        for fingerprint, incident in alert_incidents:
            incidents_per_alert[fingerprint].append(incident)

        for alert in alerts:
            alert._incidents = incidents_per_alert[alert.fingerprint]

        return alerts


def get_incidents_by_alert_fingerprint(
    tenant_id: str, fingerprint: str, session: Optional[Session] = None
) -> List[Incident]:
    with existed_or_new_session(session) as session:
        alert_incidents = session.exec(
            select(Incident)
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlertToIncident.tenant_id == LastAlert.tenant_id,
                    LastAlertToIncident.fingerprint == LastAlert.fingerprint,
                    LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                ),
            )
            .join(Incident, LastAlertToIncident.incident_id == Incident.id)
            .where(
                LastAlert.tenant_id == tenant_id,
                LastAlertToIncident.fingerprint == fingerprint,
            )
        ).all()
        return alert_incidents


def get_last_incidents(
    tenant_id: str,
    limit: int = 25,
    offset: int = 0,
    timeframe: int = None,
    upper_timestamp: datetime = None,
    lower_timestamp: datetime = None,
    is_candidate: bool = False,
    sorting: Optional[IncidentSorting] = IncidentSorting.creation_time,
    with_alerts: bool = False,
    is_predicted: bool = None,
    filters: Optional[dict] = None,
    allowed_incident_ids: Optional[List[str]] = None,
) -> Tuple[list[Incident], int]:
    """
    Get the last incidents and total amount of incidents.

    Args:
        tenant_id (str): The tenant_id to filter the incidents by.
        limit (int): Amount of objects to return
        offset (int): Current offset for
        timeframe (int|null): Return incidents only for the last <N> days
        upper_timestamp: datetime = None,
        lower_timestamp: datetime = None,
        is_candidate (bool): filter incident candidates or real incidents
        sorting: Optional[IncidentSorting]: how to sort the data
        with_alerts (bool): Pre-load alerts or not
        is_predicted (bool): filter only incidents predicted by KeepAI
        filters (dict): dict of filters
    Returns:
        List[Incident]: A list of Incident objects.
    """
    with Session(engine) as session:
        query = session.query(
            Incident,
        ).filter(
            Incident.tenant_id == tenant_id,
            Incident.is_candidate == is_candidate,
            Incident.is_visible == True,
        )

        if allowed_incident_ids:
            query = query.filter(Incident.id.in_(allowed_incident_ids))

        if is_predicted is not None:
            query = query.filter(Incident.is_predicted == is_predicted)

        if timeframe:
            query = query.filter(
                Incident.start_time
                >= datetime.now(tz=timezone.utc) - timedelta(days=timeframe)
            )

        if upper_timestamp and lower_timestamp:
            query = query.filter(
                col(Incident.last_seen_time).between(lower_timestamp, upper_timestamp)
            )
        elif upper_timestamp:
            query = query.filter(Incident.last_seen_time <= upper_timestamp)
        elif lower_timestamp:
            query = query.filter(Incident.last_seen_time >= lower_timestamp)

        if filters:
            query = apply_incident_filters(session, filters, query)

        if sorting:
            query = query.order_by(sorting.get_order_by(Incident))

        total_count = query.count()

        # Order by start_time in descending order and limit the results
        query = query.limit(limit).offset(offset)

        # Execute the query
        incidents = query.all()

        if with_alerts:
            enrich_incidents_with_alerts(tenant_id, incidents, session)
        enrich_incidents_with_enrichments(tenant_id, incidents, session)

    return incidents, total_count


def get_incident_by_id(
    tenant_id: str,
    incident_id: str | UUID,
    with_alerts: bool = False,
    session: Optional[Session] = None,
) -> Optional[Incident]:
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id, should_raise=True)
    with existed_or_new_session(session) as session:
        query = (
            session.query(
                Incident,
                AlertEnrichment,
            )
            .outerjoin(
                AlertEnrichment,
                and_(
                    Incident.tenant_id == AlertEnrichment.tenant_id,
                    cast(col(Incident.id), String)
                    == foreign(AlertEnrichment.alert_fingerprint),
                ),
            )
            .filter(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        )
        incident_with_enrichments = query.first()
        if incident_with_enrichments:
            incident, enrichments = incident_with_enrichments
            if with_alerts:
                enrich_incidents_with_alerts(
                    tenant_id,
                    [incident],
                    session,
                )
            if enrichments:
                incident.set_enrichments(enrichments.enrichments)
        else:
            incident = None

    return incident


def create_incident_from_dto(
    tenant_id: str,
    incident_dto: IncidentDtoIn | IncidentDto,
    generated_from_ai: bool = False,
    session: Optional[Session] = None,
) -> Optional[Incident]:
    """
    Creates an incident for a specified tenant based on the provided incident data transfer object (DTO).

    Args:
        tenant_id (str): The unique identifier of the tenant for whom the incident is being created.
        incident_dto (IncidentDtoIn | IncidentDto): The data transfer object containing incident details.
            Can be an instance of `IncidentDtoIn` or `IncidentDto`.
        generated_from_ai (bool, optional): Specifies whether the incident was generated by Keep's AI. Defaults to False.

    Returns:
        Optional[Incident]: The newly created `Incident` object if successful, otherwise `None`.
    """

    if issubclass(type(incident_dto), IncidentDto) and generated_from_ai:
        # NOTE: we do not use dto's alerts, alert count, start time etc
        #       because we want to re-use the BL of creating incidents
        #       where all of these are calculated inside add_alerts_to_incident
        incident_dict = {
            "user_summary": incident_dto.user_summary,
            "generated_summary": incident_dto.description,
            "user_generated_name": incident_dto.user_generated_name,
            "ai_generated_name": incident_dto.dict().get("name"),
            "assignee": incident_dto.assignee,
            "is_predicted": False,  # its not a prediction, but an AI generation
            "is_candidate": False,  # confirmed by the user :)
            "is_visible": True,  # confirmed by the user :)
            "incident_type": IncidentType.AI.value,
        }

    elif issubclass(type(incident_dto), IncidentDto):
        # we will reach this block when incident is pulled from a provider
        incident_dict = incident_dto.to_db_incident().dict()
        if "incident_type" not in incident_dict:
            incident_dict["incident_type"] = IncidentType.MANUAL.value
    else:
        # We'll reach this block when a user creates an incident
        incident_dict = incident_dto.dict()
        # Keep existing incident_type if present, default to MANUAL if not
        if "incident_type" not in incident_dict:
            incident_dict["incident_type"] = IncidentType.MANUAL.value

    if incident_dto.severity is not None:
        incident_dict["severity"] = incident_dto.severity.order

    return create_incident_from_dict(tenant_id, incident_dict, session)


@retry_on_db_error
def create_incident_from_dict(
    tenant_id: str, incident_data: dict, session: Optional[Session] = None
) -> Optional[Incident]:
    is_predicted = incident_data.get("is_predicted", False)
    if "is_candidate" not in incident_data:
        incident_data["is_candidate"] = is_predicted
    with existed_or_new_session(session) as session:
        new_incident = Incident(**incident_data, tenant_id=tenant_id)
        session.add(new_incident)
        session.commit()
        session.refresh(new_incident)
    return new_incident


@retry_on_db_error
def update_incident_from_dto_by_id(
    tenant_id: str,
    incident_id: str | UUID,
    updated_incident_dto: IncidentDtoIn | IncidentDto,
    generated_by_ai: bool = False,
) -> Optional[Incident]:
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id)

    with Session(engine) as session:
        incident = session.exec(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        ).first()

        if not incident:
            return None

        if issubclass(type(updated_incident_dto), IncidentDto):
            # We execute this when we update an incident received from the provider
            updated_data = updated_incident_dto.to_db_incident().model_dump()
        else:
            # When a user updates an Incident
            updated_data = updated_incident_dto.dict()

        for key, value in updated_data.items():
            # Update only if the new value is different from the current one
            if hasattr(incident, key) and getattr(incident, key) != value:
                if isinstance(value, Enum):
                    setattr(incident, key, value.value)
                else:
                    if value is not None:
                        setattr(incident, key, value)

        if "same_incident_in_the_past_id" in updated_data:
            incident.same_incident_in_the_past_id = updated_data[
                "same_incident_in_the_past_id"
            ]

        if generated_by_ai:
            incident.generated_summary = updated_incident_dto.user_summary
        else:
            incident.user_summary = updated_incident_dto.user_summary

        session.commit()
        session.refresh(incident)

        return incident



def delete_incident_by_id(
    tenant_id: str, incident_id: UUID, session: Optional[Session] = None
) -> bool:
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id)
    with existed_or_new_session(session) as session:
        incident = session.exec(
            select(Incident).filter(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        ).first()

        session.execute(
            update(Incident)
            .where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident.id,
            )
            .values({"status": IncidentStatus.DELETED.value})
        )

        session.commit()
        return True


def get_incidents_count(
    tenant_id: str,
) -> int:
    with Session(engine) as session:
        return (
            session.query(Incident)
            .filter(
                Incident.tenant_id == tenant_id,
            )
            .count()
        )


def get_incident_alerts_and_links_by_incident_id(
    tenant_id: str,
    incident_id: UUID | str,
    limit: Optional[int] = None,
    offset: Optional[int] = 0,
    session: Optional[Session] = None,
    include_unlinked: bool = False,
) -> tuple[List[tuple[Alert, LastAlertToIncident]], int]:
    with existed_or_new_session(session) as session:
        query = (
            session.query(
                Alert,
                LastAlertToIncident,
            )
            .select_from(LastAlertToIncident)
            .join(
                LastAlert,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .filter(
                LastAlertToIncident.tenant_id == tenant_id,
                LastAlertToIncident.incident_id == incident_id,
            )
            .order_by(col(LastAlert.timestamp).desc())
            .options(joinedload(Alert.alert_enrichment))
        )
        if not include_unlinked:
            query = query.filter(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
            )

    total_count = query.count()

    if limit is not None and offset is not None:
        query = query.limit(limit).offset(offset)

    return query.all(), total_count


def get_incident_alerts_by_incident_id(*args, **kwargs) -> tuple[List[Alert], int]:
    """
    Unpacking (List[(Alert, LastAlertToIncident)], int) to (List[Alert], int).
    """
    alerts_and_links, total_alerts = get_incident_alerts_and_links_by_incident_id(
        *args, **kwargs
    )
    alerts = [alert_and_link[0] for alert_and_link in alerts_and_links]
    return alerts, total_alerts


def get_future_incidents_by_incident_id(
    incident_id: str,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> tuple[List[Incident], int]:
    with Session(engine) as session:
        query = session.query(
            Incident,
        ).filter(Incident.same_incident_in_the_past_id == incident_id)

        if limit:
            query = query.limit(limit)
        if offset:
            query = query.offset(offset)

    total_count = query.count()

    return query.all(), total_count




def get_alerts_data_for_incident(
    tenant_id: str,
    fingerprints: Optional[List[str]] = None,
    session: Optional[Session] = None,
):
    """
    Function to prepare aggregated data for incidents from the given list of alert_ids
    Logic is wrapped to the inner function for better usability with an optional database session

    Args:
        tenant_id (str): The tenant ID to filter alerts
        alert_ids (list[str | UUID]): list of alert ids for aggregation
        session (Optional[Session]): The database session or None

    Returns: dict {sources: list[str], services: list[str], count: int}
    """
    with existed_or_new_session(session) as session:
        fields = (
            get_json_extract_field(session, Alert.event, "service"),
            Alert.provider_type,
            Alert.fingerprint,
            get_json_extract_field(session, Alert.event, "severity"),
        )

        alerts_data = session.exec(
            select(*fields)
            .select_from(LastAlert)
            .join(
                Alert,
                and_(
                    LastAlert.tenant_id == Alert.tenant_id,
                    LastAlert.alert_id == Alert.id,
                ),
            )
            .where(
                LastAlert.tenant_id == tenant_id,
                col(LastAlert.fingerprint).in_(fingerprints),
            )
        ).all()

        sources = []
        services = []
        severities = []

        for service, source, fingerprint, severity in alerts_data:
            if source:
                sources.append(source)
            if service:
                services.append(service)
            if severity:
                if isinstance(severity, int):
                    severities.append(IncidentSeverity.from_number(severity))
                else:
                    severities.append(IncidentSeverity(severity))

        return {
            "sources": set(sources),
            "services": set(services),
            "max_severity": max(severities) if severities else IncidentSeverity.LOW,
        }


@retry_on_db_error
def add_alerts_to_incident(
    tenant_id: str,
    incident: Incident,
    fingerprints: List[str],
    is_created_by_ai: bool = False,
    session: Optional[Session] = None,
    override_count: bool = False,
    exclude_unlinked_alerts: bool = False,  # if True, do not add alerts to incident if they are manually unlinked
    max_retries=3,
) -> Optional[Incident]:
    logger.info(
        f"Adding alerts to incident {incident.id} in database, total {len(fingerprints)} alerts",
        extra={"tags": {"tenant_id": tenant_id, "incident_id": incident.id}},
    )

    with existed_or_new_session(session) as session:
        with session.no_autoflush:
            # Use a set for faster membership checks
            existing_fingerprints = set(
                session.exec(
                    select(LastAlert.fingerprint)
                    .join(
                        LastAlertToIncident,
                        and_(
                            LastAlertToIncident.tenant_id == LastAlert.tenant_id,
                            LastAlertToIncident.fingerprint == LastAlert.fingerprint,
                        ),
                    )
                    .where(
                        LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                        LastAlertToIncident.tenant_id == tenant_id,
                        LastAlertToIncident.incident_id == incident.id,
                    )
                ).all()
            )

            new_fingerprints = {
                fingerprint
                for fingerprint in fingerprints
                if fingerprint not in existing_fingerprints
            }

            # filter out unlinked alerts
            if exclude_unlinked_alerts:
                unlinked_alerts = set(
                    session.exec(
                        select(LastAlert.fingerprint)
                        .join(
                            LastAlertToIncident,
                            and_(
                                LastAlertToIncident.tenant_id == LastAlert.tenant_id,
                                LastAlertToIncident.fingerprint
                                == LastAlert.fingerprint,
                            ),
                        )
                        .where(
                            LastAlertToIncident.deleted_at != NULL_FOR_DELETED_AT,
                            LastAlertToIncident.tenant_id == tenant_id,
                            LastAlertToIncident.incident_id == incident.id,
                        )
                    ).all()
                )
                new_fingerprints = new_fingerprints - unlinked_alerts

            if not new_fingerprints:
                return incident

            alert_to_incident_entries = [
                LastAlertToIncident(
                    fingerprint=str(fingerprint),  # it may sometime be UUID...
                    incident_id=incident.id,
                    tenant_id=tenant_id,
                    is_created_by_ai=is_created_by_ai,
                )
                for fingerprint in new_fingerprints
            ]

            for idx, entry in enumerate(alert_to_incident_entries):
                session.add(entry)
                if (idx + 1) % 100 == 0:
                    logger.info(
                        f"Added {idx + 1}/{len(alert_to_incident_entries)} alerts to incident {incident.id} in database",
                        extra={
                            "tags": {"tenant_id": tenant_id, "incident_id": incident.id}
                        },
                    )
                    session.flush()
            session.commit()

            alerts_data_for_incident = get_alerts_data_for_incident(
                tenant_id, new_fingerprints, session
            )

            new_sources = list(
                set(incident.sources if incident.sources else [])
                | set(alerts_data_for_incident["sources"])
            )
            new_affected_services = list(
                set(incident.affected_services if incident.affected_services else [])
                | set(alerts_data_for_incident["services"])
            )
            if not incident.forced_severity:
                # If incident has alerts already, use the max severity between existing and new alerts,
                # otherwise use the new alerts max severity
                new_severity = (
                    max(
                        incident.severity,
                        alerts_data_for_incident["max_severity"].order,
                    )
                    if incident.alerts_count
                    else alerts_data_for_incident["max_severity"].order
                )
            else:
                new_severity = incident.severity

            if not override_count:
                alerts_count = (
                    select(count(LastAlertToIncident.fingerprint)).where(
                        LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                        LastAlertToIncident.tenant_id == tenant_id,
                        LastAlertToIncident.incident_id == incident.id,
                    )
                ).scalar_subquery()
            else:
                alerts_count = alerts_data_for_incident["count"]

            last_received_field = get_json_extract_field(
                session, Alert.event, "lastReceived"
            )

            started_at, last_seen_at = session.exec(
                select(func.min(last_received_field), func.max(last_received_field))
                .join(
                    LastAlertToIncident,
                    and_(
                        LastAlertToIncident.tenant_id == Alert.tenant_id,
                        LastAlertToIncident.fingerprint == Alert.fingerprint,
                    ),
                )
                .where(
                    LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                    LastAlertToIncident.tenant_id == tenant_id,
                    LastAlertToIncident.incident_id == incident.id,
                )
            ).one()

            if isinstance(started_at, str):
                started_at = parse(started_at)

            if isinstance(last_seen_at, str):
                last_seen_at = parse(last_seen_at)

            incident_id = incident.id

            for attempt in range(max_retries):
                try:
                    session.exec(
                        update(Incident)
                        .where(
                            Incident.id == incident_id,
                            Incident.tenant_id == tenant_id,
                        )
                        .values(
                            alerts_count=alerts_count,
                            last_seen_time=last_seen_at,
                            start_time=started_at,
                            affected_services=new_affected_services,
                            severity=new_severity,
                            sources=new_sources,
                        )
                    )
                    session.commit()
                    break
                except StaleDataError as ex:
                    if "expected to update" in ex.args[0]:
                        logger.info(
                            f"Phantom read detected while updating incident `{incident_id}`, retry #{attempt}"
                        )
                        session.rollback()
                        continue
                    else:
                        raise
            session.add(incident)
            session.refresh(incident)

            return incident


def get_incident_unique_fingerprint_count(
    tenant_id: str, incident_id: str | UUID
) -> int:
    with Session(engine) as session:
        return session.execute(
            select(func.count(1))
            .select_from(LastAlertToIncident)
            .where(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.tenant_id == tenant_id,
                LastAlertToIncident.incident_id == incident_id,
            )
        ).scalar()


def get_last_alerts_for_incidents(
    incident_ids: List[str | UUID],
) -> Dict[str, List[Alert]]:
    with Session(engine) as session:
        query = (
            session.query(
                Alert,
                LastAlertToIncident.incident_id,
            )
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .filter(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.incident_id.in_(incident_ids),
            )
            .order_by(Alert.timestamp.desc())
        )

        alerts = query.all()

    incidents_alerts = defaultdict(list)
    for alert, incident_id in alerts:
        incidents_alerts[str(incident_id)].append(alert)

    return incidents_alerts


def remove_alerts_to_incident_by_incident_id(
    tenant_id: str, incident_id: str | UUID, fingerprints: List[str]
) -> Optional[int]:
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id)
    with Session(engine) as session:
        incident = session.exec(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
        ).first()

        if not incident:
            return None

        # Removing alerts-to-incident relation for provided alerts_ids
        deleted = (
            session.query(LastAlertToIncident)
            .where(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.tenant_id == tenant_id,
                LastAlertToIncident.incident_id == incident.id,
                col(LastAlertToIncident.fingerprint).in_(fingerprints),
            )
            .update(
                {
                    "deleted_at": datetime.now(datetime.now().astimezone().tzinfo),
                }
            )
        )
        session.commit()

        # Getting aggregated data for incidents for alerts which just was removed
        alerts_data_for_incident = get_alerts_data_for_incident(
            tenant_id, fingerprints, session=session
        )

        service_field = get_json_extract_field(session, Alert.event, "service")

        # checking if services of removed alerts are still presented in alerts
        # which still assigned with the incident
        existed_services_query = (
            select(func.distinct(service_field))
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .filter(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.incident_id == incident_id,
                service_field.in_(alerts_data_for_incident["services"]),
            )
        )
        services_existed = session.exec(existed_services_query)

        # checking if sources (providers) of removed alerts are still presented in alerts
        # which still assigned with the incident
        existed_sources_query = (
            select(col(Alert.provider_type).distinct())
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .filter(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.incident_id == incident_id,
                col(Alert.provider_type).in_(alerts_data_for_incident["sources"]),
            )
        )
        sources_existed = session.exec(existed_sources_query)

        severity_field = get_json_extract_field(session, Alert.event, "severity")
        # checking if severities of removed alerts are still presented in alerts
        # which still assigned with the incident
        updated_severities_query = (
            select(severity_field)
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .filter(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.incident_id == incident_id,
            )
        )
        updated_severities_result = session.exec(updated_severities_query)
        updated_severities = [
            get_int_severity(severity) for severity in updated_severities_result
        ]

        # Making lists of services and sources to remove from the incident
        services_to_remove = [
            service
            for service in alerts_data_for_incident["services"]
            if service not in services_existed
        ]
        sources_to_remove = [
            source
            for source in alerts_data_for_incident["sources"]
            if source not in sources_existed
        ]

        last_received_field = get_json_extract_field(
            session, Alert.event, "lastReceived"
        )

        started_at, last_seen_at = session.exec(
            select(func.min(last_received_field), func.max(last_received_field))
            .select_from(LastAlert)
            .join(
                LastAlertToIncident,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .join(Alert, LastAlert.alert_id == Alert.id)
            .where(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.tenant_id == tenant_id,
                LastAlertToIncident.incident_id == incident.id,
            )
        ).one()

        # filtering removed entities from affected services and sources in the incident
        new_affected_services = [
            service
            for service in incident.affected_services
            if service not in services_to_remove
        ]
        new_sources = [
            source for source in incident.sources if source not in sources_to_remove
        ]

        if not incident.forced_severity:
            new_severity = (
                max(updated_severities)
                if updated_severities
                else IncidentSeverity.LOW.order
            )
        else:
            new_severity = incident.severity

        if isinstance(started_at, str):
            started_at = parse(started_at)

        if isinstance(last_seen_at, str):
            last_seen_at = parse(last_seen_at)

        alerts_count = (
            select(count(LastAlertToIncident.fingerprint)).where(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.tenant_id == tenant_id,
                LastAlertToIncident.incident_id == incident.id,
            )
        ).subquery()

        session.exec(
            update(Incident)
            .where(
                Incident.id == incident_id,
                Incident.tenant_id == tenant_id,
            )
            .values(
                alerts_count=alerts_count,
                last_seen_time=last_seen_at,
                start_time=started_at,
                affected_services=new_affected_services,
                severity=new_severity,
                sources=new_sources,
            )
        )
        session.commit()
        session.add(incident)
        session.refresh(incident)

        return deleted


class DestinationIncidentNotFound(Exception):
    pass


def merge_incidents_to_id(
    tenant_id: str,
    source_incident_ids: List[UUID],
    # Maybe to add optional destionation_incident_dto to merge to
    destination_incident_id: UUID,
    merged_by: str | None = None,
) -> Tuple[List[UUID], List[UUID], List[UUID]]:
    with Session(engine) as session:
        destination_incident = session.exec(
            select(Incident).where(
                Incident.tenant_id == tenant_id, Incident.id == destination_incident_id
            )
        ).first()

        if not destination_incident:
            raise DestinationIncidentNotFound(
                f"Destination incident with id {destination_incident_id} not found"
            )

        source_incidents = session.exec(
            select(Incident).filter(
                Incident.tenant_id == tenant_id,
                Incident.id.in_(source_incident_ids),
            )
        ).all()

        enrich_incidents_with_alerts(tenant_id, source_incidents, session=session)

        merged_incident_ids = []
        failed_incident_ids = []
        for source_incident in source_incidents:
            source_incident_alerts_fingerprints = [
                alert.fingerprint for alert in source_incident.alerts
            ]
            source_incident.merged_into_incident_id = destination_incident.id
            source_incident.merged_at = datetime.now(tz=timezone.utc)
            source_incident.status = IncidentStatus.MERGED.value
            source_incident.merged_by = merged_by
            try:
                remove_alerts_to_incident_by_incident_id(
                    tenant_id,
                    source_incident.id,
                    [alert.fingerprint for alert in source_incident.alerts],
                )
            except OperationalError as e:
                logger.error(
                    f"Error removing alerts from incident {source_incident.id}: {e}"
                )
            try:
                add_alerts_to_incident(
                    tenant_id,
                    destination_incident,
                    source_incident_alerts_fingerprints,
                    session=session,
                )
                merged_incident_ids.append(source_incident.id)
            except OperationalError as e:
                logger.error(
                    f"Error adding alerts to incident {destination_incident.id} from {source_incident.id}: {e}"
                )
                failed_incident_ids.append(source_incident.id)

        session.commit()
        session.refresh(destination_incident)
        return merged_incident_ids, failed_incident_ids


def get_alerts_count(
    tenant_id: str,
) -> int:
    with Session(engine) as session:
        return (
            session.query(Alert)
            .filter(
                Alert.tenant_id == tenant_id,
            )
            .count()
        )


def get_first_alert_datetime(
    tenant_id: str,
) -> datetime | None:
    with Session(engine) as session:
        first_alert = (
            session.query(Alert)
            .filter(
                Alert.tenant_id == tenant_id,
            )
            .first()
        )
        if first_alert:
            return first_alert.timestamp


def confirm_predicted_incident_by_id(
    tenant_id: str,
    incident_id: UUID | str,
):
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id)
    with Session(engine) as session:
        incident = session.exec(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
                Incident.is_candidate == expression.true(),
            )
        ).first()

        if not incident:
            return None

        session.query(Incident).filter(
            Incident.tenant_id == tenant_id,
            Incident.id == incident_id,
            Incident.is_candidate == expression.true(),
        ).update(
            {
                "is_visible": True,
                "is_candidate": True,
            }
        )

        session.commit()
        session.refresh(incident)

        return incident


def get_tenant_config(tenant_id: str) -> dict:
    with Session(engine) as session:
        tenant_data = session.exec(select(Tenant).where(Tenant.id == tenant_id)).first()
        return tenant_data.configuration if tenant_data else {}





def update_incident_severity(
    tenant_id: str, incident_id: UUID, severity: IncidentSeverity
) -> Optional[Incident]:
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id)
    with Session(engine) as session:
        incident = session.exec(
            select(Incident)
            .where(Incident.tenant_id == tenant_id)
            .where(Incident.id == incident_id)
        ).first()

        if not incident:
            logger.error(
                f"Incident not found for tenant {tenant_id} and incident {incident_id}",
                extra={"tenant_id": tenant_id},
            )
            return

        incident.severity = severity.order
        incident.forced_severity = True
        session.add(incident)
        session.commit()
        session.refresh(incident)

        return incident





def get_tags(tenant_id):
    with Session(engine) as session:
        tags = session.exec(select(Tag).where(Tag.tenant_id == tenant_id)).all()
    return tags




def get_provider_by_name(tenant_id: str, provider_name: str) -> Provider:
    with Session(engine) as session:
        provider = session.exec(
            select(Provider)
            .where(Provider.tenant_id == tenant_id)
            .where(Provider.name == provider_name)
        ).first()
    return provider


def get_provider_by_type_and_id(
    tenant_id: str, provider_type: str, provider_id: Optional[str]
) -> Provider:
    with Session(engine) as session:
        query = select(Provider).where(
            Provider.tenant_id == tenant_id,
            Provider.type == provider_type,
            Provider.id == provider_id,
        )
        provider = session.exec(query).first()
    return provider


def bulk_upsert_alert_fields(
    tenant_id: str,
    fields: List[str],
    provider_id: str,
    provider_type: str,
    session: Optional[Session] = None,
    max_retries=3,
):
    with existed_or_new_session(session) as session:
        for attempt in range(max_retries):
            try:
                # Prepare the data for bulk insert
                data = [
                    {
                        "tenant_id": tenant_id,
                        "field_name": field,
                        "provider_id": provider_id,
                        "provider_type": provider_type,
                    }
                    for field in fields
                ]

                if engine.dialect.name == "postgresql":
                    stmt = pg_insert(AlertField).values(data)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[
                            "tenant_id",
                            "field_name",
                        ],  # Unique constraint columns
                        set_={
                            "provider_id": stmt.excluded.provider_id,
                            "provider_type": stmt.excluded.provider_type,
                        },
                    )
                elif engine.dialect.name == "mysql":
                    stmt = mysql_insert(AlertField).values(data)
                    stmt = stmt.on_duplicate_key_update(
                        provider_id=stmt.inserted.provider_id,
                        provider_type=stmt.inserted.provider_type,
                    )
                elif engine.dialect.name == "sqlite":
                    stmt = sqlite_insert(AlertField).values(data)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[
                            "tenant_id",
                            "field_name",
                        ],  # Unique constraint columns
                        set_={
                            "provider_id": stmt.excluded.provider_id,
                            "provider_type": stmt.excluded.provider_type,
                        },
                    )
                elif engine.dialect.name == "mssql":
                    # SQL Server requires a raw query with a MERGE statement
                    values = ", ".join(
                        f"('{tenant_id}', '{field}', '{provider_id}', '{provider_type}')"
                        for field in fields
                    )

                    merge_query = text(
                        f"""
                        MERGE INTO AlertField AS target
                        USING (VALUES {values}) AS source (tenant_id, field_name, provider_id, provider_type)
                        ON target.tenant_id = source.tenant_id AND target.field_name = source.field_name
                        WHEN MATCHED THEN
                            UPDATE SET provider_id = source.provider_id, provider_type = source.provider_type
                        WHEN NOT MATCHED THEN
                            INSERT (tenant_id, field_name, provider_id, provider_type)
                            VALUES (source.tenant_id, source.field_name, source.provider_id, source.provider_type)
                    """
                    )

                    session.execute(merge_query)
                else:
                    raise NotImplementedError(
                        f"Upsert not supported for {engine.dialect.name}"
                    )

                # Execute the statement
                if engine.dialect.name != "mssql":  # Already executed for SQL Server
                    session.execute(stmt)
                session.commit()

                break

            except OperationalError as e:
                # Handle any potential race conditions
                session.rollback()
                if "Deadlock found" in str(e):
                    logger.info(
                        f"Deadlock found during bulk_upsert_alert_fields `{e}`, retry #{attempt}"
                    )
                    if attempt >= max_retries:
                        raise e
                    continue
                else:
                    raise e


def get_alerts_fields(tenant_id: str) -> List[AlertField]:
    with Session(engine) as session:
        fields = session.exec(
            select(AlertField).where(AlertField.tenant_id == tenant_id)
        ).all()
    return fields


def change_incident_status_by_id(
    tenant_id: str,
    incident_id: UUID | str,
    status: IncidentStatus,
    end_time: datetime | None = None,
) -> bool:
    if isinstance(incident_id, str):
        incident_id = __convert_to_uuid(incident_id)
    with Session(engine) as session:
        stmt = (
            update(Incident)
            .where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
            .values(
                status=status.value,
                end_time=end_time,
            )
        )
        session.exec(stmt)
        session.commit()





def is_all_alerts_resolved(
    fingerprints: Optional[List[str]] = None,
    incident: Optional[Incident] = None,
    session: Optional[Session] = None,
):
    return is_all_alerts_in_status(
        fingerprints, incident, AlertStatus.RESOLVED, session
    )


def is_all_alerts_in_status(
    fingerprints: Optional[List[str]] = None,
    incident: Optional[Incident] = None,
    status: AlertStatus = AlertStatus.RESOLVED,
    session: Optional[Session] = None,
):
    if incident and incident.alerts_count == 0:
        return False

    with existed_or_new_session(session) as session:
        enriched_status_field = get_json_extract_field(
            session, AlertEnrichment.enrichments, "status"
        )
        status_field = get_json_extract_field(session, Alert.event, "status")

        subquery = (
            select(
                enriched_status_field.label("enriched_status"),
                status_field.label("status"),
            )
            .select_from(LastAlert)
            .join(Alert, LastAlert.alert_id == Alert.id)
            .outerjoin(
                AlertEnrichment,
                and_(
                    Alert.tenant_id == AlertEnrichment.tenant_id,
                    Alert.fingerprint == AlertEnrichment.alert_fingerprint,
                ),
            )
        )

        if fingerprints:
            subquery = subquery.where(LastAlert.fingerprint.in_(fingerprints))

        if incident:
            subquery = subquery.join(
                LastAlertToIncident,
                and_(
                    LastAlertToIncident.tenant_id == LastAlert.tenant_id,
                    LastAlertToIncident.fingerprint == LastAlert.fingerprint,
                ),
            ).where(
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
                LastAlertToIncident.incident_id == incident.id,
            )

        subquery = subquery.subquery()

        not_in_status_exists = session.query(
            exists(
                select(
                    subquery.c.enriched_status,
                    subquery.c.status,
                )
                .select_from(subquery)
                .where(
                    or_(
                        subquery.c.enriched_status != status.value,
                        and_(
                            subquery.c.enriched_status.is_(None),
                            subquery.c.status != status.value,
                        ),
                    )
                )
            )
        ).scalar()

        return not not_in_status_exists


def is_last_incident_alert_resolved(
    incident: Incident, session: Optional[Session] = None
) -> bool:
    return is_edge_incident_alert_resolved(incident, func.max, session)


def is_first_incident_alert_resolved(
    incident: Incident, session: Optional[Session] = None
) -> bool:
    return is_edge_incident_alert_resolved(incident, func.min, session)


def is_edge_incident_alert_resolved(
    incident: Incident, direction: Callable, session: Optional[Session] = None
) -> bool:
    if incident.alerts_count == 0:
        return False

    with existed_or_new_session(session) as session:
        enriched_status_field = get_json_extract_field(
            session, AlertEnrichment.enrichments, "status"
        )
        status_field = get_json_extract_field(session, Alert.event, "status")

        finerprint, enriched_status, status = session.exec(
            select(Alert.fingerprint, enriched_status_field, status_field)
            .select_from(Alert)
            .outerjoin(
                AlertEnrichment,
                and_(
                    Alert.tenant_id == AlertEnrichment.tenant_id,
                    Alert.fingerprint == AlertEnrichment.alert_fingerprint,
                ),
            )
            .join(
                LastAlertToIncident,
                and_(
                    LastAlertToIncident.tenant_id == Alert.tenant_id,
                    LastAlertToIncident.fingerprint == Alert.fingerprint,
                ),
            )
            .where(LastAlertToIncident.incident_id == incident.id)
            .group_by(Alert.fingerprint)
            .having(func.max(Alert.timestamp))
            .order_by(direction(Alert.timestamp))
        ).first()

        return enriched_status == AlertStatus.RESOLVED.value or (
            enriched_status is None and status == AlertStatus.RESOLVED.value
        )


def get_alerts_metrics_by_provider(
    tenant_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    fields: Optional[List[str]] = [],
) -> Dict[str, Dict[str, Any]]:
    dynamic_field_sums = [
        func.sum(
            case(
                (
                    get_json_extract_field(session, Alert.event, field).isnot(None)
                    & (get_json_extract_field(session, Alert.event, field) != "false"),
                    1,
                ),
                else_=0,
            )
        ).label(f"{field}_count")
        for field in fields
    ]

    with Session(engine) as session:
        query = (
            session.query(
                Alert.provider_type,
                Alert.provider_id,
                func.count(Alert.id).label("total_alerts"),
                func.sum(
                    case((LastAlertToIncident.fingerprint.isnot(None), 1), else_=0)
                ).label("correlated_alerts"),
                *dynamic_field_sums,
            )
            .join(LastAlert, Alert.id == LastAlert.alert_id)
            .outerjoin(
                LastAlertToIncident,
                and_(
                    LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                    LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                ),
            )
            .filter(
                Alert.tenant_id == tenant_id,
            )
        )

        # Add timestamp filter only if both start_date and end_date are provided
        if start_date and end_date:
            query = query.filter(
                Alert.timestamp >= start_date, Alert.timestamp <= end_date
            )

        results = query.group_by(Alert.provider_id, Alert.provider_type).all()

    metrics = {}
    for row in results:
        key = f"{row.provider_id}_{row.provider_type}"
        metrics[key] = {
            "total_alerts": row.total_alerts,
            "correlated_alerts": row.correlated_alerts,
            "provider_type": row.provider_type,
        }
        for field in fields:
            metrics[key][f"{field}_count"] = getattr(row, f"{field}_count", 0)

    return metrics


def get_or_create_external_ai_settings(
    tenant_id: str,
) -> List[ExternalAIConfigAndMetadataDto]:
    with Session(engine) as session:
        algorithm_configs = session.exec(
            select(ExternalAIConfigAndMetadata).where(
                ExternalAIConfigAndMetadata.tenant_id == tenant_id
            )
        ).all()
        if len(algorithm_configs) == 0:
            if os.environ.get("KEEP_EXTERNAL_AI_TRANSFORMERS_URL") is not None:
                algorithm_config = ExternalAIConfigAndMetadata.from_external_ai(
                    tenant_id=tenant_id, algorithm=external_ai_transformers
                )
                session.add(algorithm_config)
                session.commit()
                algorithm_configs = [algorithm_config]
        return [
            ExternalAIConfigAndMetadataDto.from_orm(algorithm_config)
            for algorithm_config in algorithm_configs
        ]


def update_extrnal_ai_settings(
    tenant_id: str, ai_settings: ExternalAIConfigAndMetadata
) -> ExternalAIConfigAndMetadataDto:
    with Session(engine) as session:
        setting = (
            session.query(ExternalAIConfigAndMetadata)
            .filter(
                ExternalAIConfigAndMetadata.tenant_id == tenant_id,
                ExternalAIConfigAndMetadata.id == ai_settings.id,
            )
            .first()
        )
        setting.settings = json.dumps(ai_settings.settings)
        setting.feedback_logs = ai_settings.feedback_logs
        if ai_settings.settings_proposed_by_algorithm is not None:
            setting.settings_proposed_by_algorithm = json.dumps(
                ai_settings.settings_proposed_by_algorithm
            )
        else:
            setting.settings_proposed_by_algorithm = None
        session.add(setting)
        session.commit()
    return setting


def get_table_class(table_name: str) -> Type[SQLModel]:
    """
    Get the SQLModel table class dynamically based on table name.
    Assumes table classes follow PascalCase naming convention.

    Args:
        table_name (str): Name of the table in snake_case (e.g. "alerts", "rules")

    Returns:
        Type[SQLModel]: The corresponding SQLModel table class
    """
    # Convert snake_case to PascalCase and remove trailing 's' if exists
    class_name = "".join(
        word.capitalize() for word in table_name.rstrip("s").split("_")
    )

    # Get all SQLModel subclasses from the imported modules
    model_classes = {
        cls.__name__: cls
        for cls in SQLModel.__subclasses__()
        if hasattr(cls, "__tablename__")
    }

    if class_name not in model_classes:
        raise ValueError(f"No table class found for table name: {table_name}")

    return model_classes[class_name]


def get_resource_ids_by_resource_type(
    tenant_id: str, table_name: str, uid: str, session: Optional[Session] = None
) -> List[str]:
    """
    Get all unique IDs from a table grouped by a specified UID column.

    Args:
        tenant_id (str): The tenant ID to filter by
        table_name (str): Name of the table (e.g. "alerts", "rules")
        uid (str): Name of the column to group by
        session (Optional[Session]): SQLModel session

    Returns:
        List[str]: List of unique IDs

    Example:
        >>> get_resource_ids_by_resource_type("tenant123", "alerts", "alert_id")
        ['id1', 'id2', 'id3']
    """
    with existed_or_new_session(session) as session:
        # Get the table class dynamically
        table_class = get_table_class(table_name)

        # Create the query using SQLModel's select
        query = (
            select(getattr(table_class, uid))
            .distinct()
            .where(getattr(table_class, "tenant_id") == tenant_id)
        )

        # Execute the query and return results
        result = session.exec(query)
        return result.all()





def get_last_alerts_by_fingerprints(
    tenant_id: str,
    fingerprint: List[str],
    session: Optional[Session] = None,
) -> List[LastAlert]:
    with existed_or_new_session(session) as session:
        query = select(LastAlert).where(
            and_(
                LastAlert.tenant_id == tenant_id,
                LastAlert.fingerprint.in_(fingerprint),
            )
        )
        return session.exec(query).all()


def get_last_alert_by_fingerprint(
    tenant_id: str,
    fingerprint: str,
    session: Optional[Session] = None,
    for_update: bool = False,
) -> Optional[LastAlert]:
    with existed_or_new_session(session) as session:
        query = select(LastAlert).where(
            and_(
                LastAlert.tenant_id == tenant_id,
                LastAlert.fingerprint == fingerprint,
            )
        )
        if for_update:
            query = query.with_for_update()
        return session.exec(query).first()


def set_last_alert(
    tenant_id: str, alert: Alert, session: Optional[Session] = None, max_retries=3
) -> None:
    fingerprint = alert.fingerprint
    logger.info(f"Setting last alert for `{fingerprint}`")
    with existed_or_new_session(session) as session:
        for attempt in range(1, max_retries + 1):
            logger.info(
                f"Attempt {attempt} to set last alert for `{fingerprint}`",
                extra={
                    "alert_id": alert.id,
                    "tenant_id": tenant_id,
                    "fingerprint": fingerprint,
                },
            )
            try:
                last_alert = get_last_alert_by_fingerprint(
                    tenant_id, fingerprint, session, for_update=True
                )

                # To prevent rare, but possible race condition
                # For example if older alert failed to process
                # and retried after new one
                if last_alert and last_alert.timestamp.replace(
                    tzinfo=tz.UTC
                ) < alert.timestamp.replace(tzinfo=tz.UTC):
                    logger.info(
                        f"Update last alert for `{fingerprint}`: {last_alert.alert_id} -> {alert.id}",
                        extra={
                            "alert_id": alert.id,
                            "tenant_id": tenant_id,
                            "fingerprint": fingerprint,
                        },
                    )
                    last_alert.timestamp = alert.timestamp
                    last_alert.alert_id = alert.id
                    last_alert.alert_hash = alert.alert_hash
                    session.add(last_alert)

                elif not last_alert:
                    logger.info(f"No last alert for `{fingerprint}`, creating new")
                    last_alert = LastAlert(
                        tenant_id=tenant_id,
                        fingerprint=alert.fingerprint,
                        timestamp=alert.timestamp,
                        first_timestamp=alert.timestamp,
                        alert_id=alert.id,
                        alert_hash=alert.alert_hash,
                    )
                    session.add(last_alert)

                session.commit()
            except IntegrityError as ex:
                session.rollback()
                logger.warning(
                    f"Integrity error while updating lastalert for `{fingerprint}`, retry #{attempt}",
                    extra={
                        "alert_id": alert.id,
                        "tenant_id": tenant_id,
                        "fingerprint": fingerprint,
                        "error": str(ex),
                    },
                )
                if attempt == max_retries:
                    raise
                # Small delay before retry to avoid hammering the database
                time.sleep(0.1 * attempt)
                continue
            except OperationalError as ex:
                session.rollback()
                message = ex.args[0] if ex.args else ""
                if "no such savepoint" in message:
                    logger.info(
                        f"No such savepoint while updating lastalert for `{fingerprint}`, retry #{attempt}"
                    )
                elif "Deadlock found" in message:
                    logger.info(
                        f"Deadlock found while updating lastalert for `{fingerprint}`, retry #{attempt}"
                    )
                else:
                    logger.exception(
                        f"Operational error while updating lastalert for `{fingerprint}`",
                        extra={
                            "alert_id": alert.id,
                            "tenant_id": tenant_id,
                            "fingerprint": fingerprint,
                        },
                    )
                    raise

                if attempt == max_retries:
                    raise
                # Small delay before retry to avoid hammering the database
                time.sleep(0.1 * attempt)
                continue
            except NoActiveSqlTransaction as ex:
                session.rollback()
                logger.exception(
                    f"No active sql transaction while updating lastalert for `{fingerprint}`, retry #{attempt}",
                    extra={
                        "alert_id": alert.id,
                        "tenant_id": tenant_id,
                        "fingerprint": fingerprint,
                    },
                )
                if attempt == max_retries:
                    raise ex
                # Small delay before retry to avoid hammering the database
                time.sleep(0.1 * attempt)
                continue
            else:
                logger.debug(
                    f"Successfully updated lastalert for `{fingerprint}`",
                    extra={
                        "alert_id": alert.id,
                        "tenant_id": tenant_id,
                        "fingerprint": fingerprint,
                    },
                )
                break
        else:
            raise RuntimeError(
                f"Failed to set last alert for `{fingerprint}` after {max_retries} attempts"
            )


def set_maintenance_windows_trace(
    alert: Alert,
    maintenance_w: MaintenanceWindowRule,
    session: Optional[Session] = None,
):
    mw_id = str(maintenance_w.id)
    if mw_id in alert.event.get("maintenance_windows_trace", []):
        return
    with existed_or_new_session(session) as session:
        if "maintenance_windows_trace" in alert.event:
            if mw_id not in alert.event["maintenance_windows_trace"]:
                alert.event["maintenance_windows_trace"].append(mw_id)
        else:
            alert.event["maintenance_windows_trace"] = [mw_id]
        flag_modified(alert, "event")
        session.add(alert)
        session.commit()


def get_provider_logs(
    tenant_id: str, provider_id: str, limit: int = 100
) -> List[ProviderExecutionLog]:
    with Session(engine) as session:
        logs = (
            session.query(ProviderExecutionLog)
            .filter(
                ProviderExecutionLog.tenant_id == tenant_id,
                ProviderExecutionLog.provider_id == provider_id,
            )
            .order_by(desc(ProviderExecutionLog.timestamp))
            .limit(limit)
            .all()
        )
    return logs



def get_error_alerts(tenant_id: str, limit: int = 100) -> List[AlertRaw]:
    with Session(engine) as session:
        return (
            session.query(AlertRaw)
            .filter(
                AlertRaw.tenant_id == tenant_id,
                AlertRaw.error == True,
                AlertRaw.dismissed == False,
            )
            .limit(limit)
            .all()
        )


def dismiss_error_alerts(tenant_id: str, alert_id=None, dismissed_by=None) -> None:
    with Session(engine) as session:
        stmt = (
            update(AlertRaw)
            .where(
                AlertRaw.tenant_id == tenant_id,
            )
            .values(
                dismissed=True,
                dismissed_by=dismissed_by,
                dismissed_at=datetime.now(tz=timezone.utc),
            )
        )
        if alert_id:
            if isinstance(alert_id, str):
                alert_id_uuid = uuid.UUID(alert_id)
                stmt = stmt.where(AlertRaw.id == alert_id_uuid)
            else:
                stmt = stmt.where(AlertRaw.id == alert_id)
        session.exec(stmt)
        session.commit()


def create_tenant(tenant_name: str) -> str:
    with Session(engine) as session:
        try:
            # check if the tenant exist:
            logger.info("Checking if tenant exists")
            tenant = session.exec(
                select(Tenant).where(Tenant.name == tenant_name)
            ).first()
            if not tenant:
                # Do everything related with single tenant creation in here
                tenant_id = str(uuid4())
                logger.info(
                    "Creating tenant",
                    extra={"tenant_id": tenant_id, "tenant_name": tenant_name},
                )
                session.add(Tenant(id=tenant_id, name=tenant_name))
            else:
                logger.warning("Tenant already exists")

            # commit the changes
            session.commit()
            logger.info(
                "Tenant created",
                extra={"tenant_id": tenant_id, "tenant_name": tenant_name},
            )
            return tenant_id
        except IntegrityError:
            # Tenant already exists
            logger.exception("Failed to create tenant")
            raise
        except Exception:
            logger.exception("Failed to create tenant")
            pass


def create_single_tenant_for_e2e(tenant_id: str) -> None:
    """
    Creates the single tenant and the default user if they don't exist.
    """
    with Session(engine) as session:
        try:
            # check if the tenant exist:
            logger.info("Checking if single tenant exists")
            tenant = session.exec(select(Tenant).where(Tenant.id == tenant_id)).first()
            if not tenant:
                # Do everything related with single tenant creation in here
                logger.info("Creating single tenant", extra={"tenant_id": tenant_id})
                session.add(Tenant(id=tenant_id, name="Single Tenant"))
            else:
                logger.info("Single tenant already exists")

            # commit the changes
            session.commit()
            logger.info("Single tenant created", extra={"tenant_id": tenant_id})
        except IntegrityError:
            # Tenant already exists
            logger.exception("Failed to provision single tenant")
            raise
        except Exception:
            logger.exception("Failed to create single tenant")
            pass

