"""
This module is responsible for creating the database and tables when the application starts.

The reason to split this code from db.py is that the functions here are invoked from the master process
when the application starts, while the functions in db.py are invoked from the worker processes.

This is important because if the master process init the engine, it will be forked to the worker processes,
and the engine will be shared among all the processes, causing issues with the connections.

** This happens because the engine is not fork-safe, and the connections are not thread-safe. **

The mitigation is to create different engines for each process, and the master process should only be responsible
for creating the database and tables, while the worker processes should only be responsible for creating the sessions.

The schema check
----------------

This image does not migrate. The schema is owned by `keep-migrations`, whose
image Argo runs as a PreSync hook Job once per release, before any pod of the new
ReplicaSet exists. Nothing here reads a migration script, and there are none in
the image to read.

What is left is `schema_drift`, which backs `/readyz`: does the live schema
contain every table and column *this image's models* declare? That question is
answerable from the database alone, which is what lets the scripts leave. It is
also the better question — a revision comparison trusts `alembic_version`, so it
passes a stamped-but-half-applied migration and a hand-edited schema alike.
"""

import hashlib
import logging
import os

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.config.core import config
from src.repositories.db import engine
from src.models.db.alert import *  # pylint: disable=unused-wildcard-import
from src.models.db.dashboard import *  # pylint: disable=unused-wildcard-import
from src.models.db.extraction import *  # pylint: disable=unused-wildcard-import
from src.models.db.mapping import *  # pylint: disable=unused-wildcard-import
from src.models.db.preset import *  # pylint: disable=unused-wildcard-import
from src.models.db.provider import *  # pylint: disable=unused-wildcard-import
from src.models.db.rule import *  # pylint: disable=unused-wildcard-import
from src.models.db.statistics import *  # pylint: disable=unused-wildcard-import
from src.models.db.tenant import *  # pylint: disable=unused-wildcard-import

# The full, deterministic table set for `schema_drift` -- the wildcard imports
# above are whatever this module happens to need, not what the image declares.
from src.models.db.all_models import declared_tables

# This import is required to create the tables
from src.services.identity_manager.rbac import Admin as AdminRole

logger = logging.getLogger(__name__)

KEEP_FORCE_RESET_DEFAULT_PASSWORD = config(
    "KEEP_FORCE_RESET_DEFAULT_PASSWORD", default="false", cast=bool
)
DEFAULT_USERNAME = config("KEEP_DEFAULT_USERNAME", default="keep")
DEFAULT_PASSWORD = config("KEEP_DEFAULT_PASSWORD", default="keep")


def try_create_single_tenant(tenant_id: str, create_default_user=True) -> None:
    """
    Creates the single tenant and the default user if they don't exist.
    """
    # if Keep is not multitenant, let's import the User table too:
    from src.models.db.user import User  # pylint: disable=import-outside-toplevel

    with Session(engine) as session:
        try:
            # check if the tenant exist:
            tenant = session.exec(select(Tenant).where(Tenant.id == tenant_id)).first()
            if not tenant:
                # Do everything related with single tenant creation in here
                logger.info("Creating single tenant")
                session.add(Tenant(id=tenant_id, name="Single Tenant"))
            else:
                logger.info("Single tenant already exists")

            # now let's create the default user

            # check if at least one user exists:
            user: User | None = session.exec(select(User)).first()
            # if no users exist, let's create the default user
            if not user and create_default_user:
                logger.info("Creating default user")

                default_password = hashlib.sha256(DEFAULT_PASSWORD.encode()).hexdigest()
                default_user = User(
                    username=DEFAULT_USERNAME,
                    password_hash=default_password,
                    role=AdminRole.get_name(),
                )
                session.add(default_user)
                logger.info("Default user created")
            # else, if the user want to force the refresh of the default user password
            elif KEEP_FORCE_RESET_DEFAULT_PASSWORD and user:
                # update the password of the default user
                logger.info("Forcing reset of default user password")
                default_password = hashlib.sha256(DEFAULT_PASSWORD.encode()).hexdigest()
                user.password_hash = default_password
                if user.username != DEFAULT_USERNAME:
                    logger.info(
                        "Default user username updated",
                        extra={
                            "username": user.username,
                            "new_username": DEFAULT_USERNAME,
                        },
                    )
                    user.username = DEFAULT_USERNAME
                logger.info("Default user password updated")
            # provision default api keys
            if os.environ.get("KEEP_DEFAULT_API_KEYS", ""):
                logger.info("Provisioning default api keys")
                from contextmanager.contextmanager import ContextManager
                from secretmanager.secretmanagerfactory import SecretManagerFactory

                default_api_keys = os.environ.get("KEEP_DEFAULT_API_KEYS").split(",")
                for default_api_key in default_api_keys:
                    try:
                        api_key_name, api_key_role, api_key_secret = (
                            default_api_key.strip().split(":")
                        )
                    except ValueError:
                        logger.error(
                            "Invalid format for default api key. Expected format: name:role:secret"
                        )
                    # Create the default api key for the default user
                    api_key = session.exec(
                        select(TenantApiKey).where(
                            TenantApiKey.reference_id == api_key_name
                        )
                    ).first()
                    if api_key:
                        logger.info(f"Api key {api_key_name} already exists")
                        continue
                    logger.info(f"Provisioning api key {api_key_name}")
                    hashed_api_key = hashlib.sha256(
                        api_key_secret.encode("utf-8")
                    ).hexdigest()
                    new_installation_api_key = TenantApiKey(
                        tenant_id=tenant_id,
                        reference_id=api_key_name,
                        key_hash=hashed_api_key,
                        is_system=True,
                        created_by="system",
                        role=api_key_role,
                    )
                    session.add(new_installation_api_key)
                    # write to the secret manager
                    context_manager = ContextManager(tenant_id=tenant_id)
                    secret_manager = SecretManagerFactory.get_secret_manager(
                        context_manager
                    )
                    try:
                        secret_manager.write_secret(
                            secret_name=f"{tenant_id}-{api_key_name}",
                            secret_value=api_key_secret,
                        )
                    # probably 409 if the secret already exists, but we don't want to fail on that
                    except Exception:
                        logger.exception(
                            f"Failed to write secret for api key {api_key_name}"
                        )
                        pass
                    logger.info(f"Api key {api_key_name} provisioned")
                logger.info("Api keys provisioned")

            # commit the changes
            session.commit()
            logger.info("Single tenant created")
        except IntegrityError:
            # Tenant already exists
            logger.exception("Failed to provision single tenant")
            raise
        except Exception:
            logger.exception("Failed to create single tenant")
            pass



_schema_drift_ok_cache: bool = False


def live_tables() -> dict[str, set[str]]:
    """{table name -> column names} for the database this process is pointed at.

    One query rather than `inspect()`'s table-then-columns walk: on a 51-table
    database that walk is 52 round trips and ~260ms, against ~37ms here. The pool
    is 5 + 10 overflow per worker and `pool_timeout` (10s) exceeds the probe's own
    budget (2s), so a checkout this endpoint cannot cancel is worth avoiding.

    sqlite has no `information_schema`, so it takes the reflection path. Only
    tests and single-process dev run on sqlite, where the round trips are free.
    """
    if engine.dialect.name == "sqlite":
        inspector = sa_inspect(engine)
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
        ).fetchall()
    live: dict[str, set[str]] = {}
    for table, column in rows:
        live.setdefault(table, set()).add(column)
    return live


def schema_drift() -> tuple[bool, dict]:
    """(satisfied, missing) -- does the live schema contain everything this
    image's models declare?

    Deliberately one-directional. Tables and columns the database has and the
    models do not are IGNORED, which is what makes image rollback work: an older
    image declares fewer columns, finds them all, and is happy. It also means the
    automation tables -- migrated here but modelled in keep-automation-api -- need
    no exclusion list, because they only ever appear in the ignored direction.

    Compares names, never types. A type-level comparison is not portable across
    sqlite and Postgres (`sa.Enum` degrades to VARCHAR+CHECK, `timezone=True` is
    unrepresented, JSONB becomes JSON).

    Memoised on success only, like `_script_directory`: the schema only moves
    forward during a process's life, so once satisfied it stays satisfied. A
    transient failure must not be cached, or the probe never recovers.
    """
    global _schema_drift_ok_cache
    if _schema_drift_ok_cache:
        return True, {}

    declared = declared_tables()
    live = live_tables()

    missing_tables = sorted(name for name in declared if name not in live)
    missing_columns = {
        name: sorted(columns - live[name])
        for name, columns in declared.items()
        if name in live and columns - live[name]
    }

    satisfied = not missing_tables and not missing_columns
    if satisfied:
        _schema_drift_ok_cache = True
        logger.info(
            "Live schema satisfies all %s declared tables", len(declared)
        )
        return True, {}

    logger.error(
        "Live schema does not satisfy this image's models: "
        "missing_tables=%s missing_columns=%s",
        missing_tables,
        missing_columns,
    )
    return False, {"missing_tables": missing_tables, "missing_columns": missing_columns}
