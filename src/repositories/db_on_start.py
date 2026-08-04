"""
This module is responsible for creating the database and tables when the application starts.

The reason to split this code from db.py is that the functions here are invoked from the master process
when the application starts, while the functions in db.py are invoked from the worker processes.

This is important because if the master process init the engine, it will be forked to the worker processes,
and the engine will be shared among all the processes, causing issues with the connections.

** This happens because the engine is not fork-safe, and the connections are not thread-safe. **

The mitigation is to create different engines for each process, and the master process should only be responsible
for creating the database and tables, while the worker processes should only be responsible for creating the sessions.
"""

import hashlib
import logging
import os
from contextlib import contextmanager
from functools import lru_cache

import alembic.command
import alembic.config
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


# Arbitrary, but must be identical in every process that migrates.
_MIGRATION_LOCK_KEY = int(
    os.environ.get("KEEP_MIGRATION_ADVISORY_LOCK_KEY", "8274419300112233")
)
# How long to wait for the migrating replica before giving up on the lock.
_MIGRATION_LOCK_TIMEOUT = int(
    os.environ.get("KEEP_MIGRATION_LOCK_TIMEOUT_SECONDS", "600")
)
_MIGRATION_LOCK_POLL_SECONDS = float(
    os.environ.get("KEEP_MIGRATION_LOCK_POLL_SECONDS", "2")
)


def get_alembic_config() -> "alembic.config.Config":
    """Alembic config with an absolute script_location.

    alembic.ini uses relative paths, which break when the app runs as an
    installed package from an arbitrary working directory.
    """
    config_path = os.path.dirname(os.path.abspath(__file__)) + "/../../" + "alembic.ini"
    cfg = alembic.config.Config(file_=config_path)
    cfg.set_main_option(
        "script_location",
        os.path.dirname(os.path.abspath(__file__)) + "/../models/db/migrations",
    )
    return cfg


@lru_cache(maxsize=1)
def get_script_head() -> str | None:
    """The head revision shipped in this image's migration scripts.

    Cached because the scripts never change at runtime and /readyz asks for this
    on every probe.
    """
    from alembic.script import ScriptDirectory

    try:
        return ScriptDirectory.from_config(get_alembic_config()).get_current_head()
    except Exception:
        logger.exception("Failed to read the alembic script head")
        return None


def get_db_revision() -> str | None:
    """The revision currently stamped in the database, or None if the
    `alembic_version` table doesn't exist / is empty."""
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def schema_at_head() -> tuple[bool, str | None, str | None]:
    """(at_head, db_revision, script_head) — /readyz must not advertise a pod
    whose DB is behind the migrations in its own image."""
    db_revision = get_db_revision()
    script_head = get_script_head()
    at_head = bool(db_revision) and bool(script_head) and db_revision == script_head
    return at_head, db_revision, script_head


@contextmanager
def _migration_lock():
    """Serialize `alembic upgrade head` across replicas with a Postgres advisory
    lock.

    Migrations run in-process on every pod's startup, so on a deploy with a
    pending migration all replicas attempt the same DDL; the losers fail with
    "already exists", which kills gunicorn's master and CrashLoopBackOffs the
    pod. With the lock exactly one replica migrates and the rest no-op.

    No-op on non-Postgres dialects (SQLite in tests) — no concurrent replicas.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return

    import time

    from sqlalchemy import text

    conn = engine.connect()
    try:
        # try + poll rather than the blocking pg_advisory_lock: `lock_timeout`
        # does not reliably bound advisory-lock waits, and an unbounded wait
        # here would hang pod startup.
        deadline = time.monotonic() + _MIGRATION_LOCK_TIMEOUT
        acquired = False
        while True:
            try:
                acquired = bool(
                    conn.execute(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": _MIGRATION_LOCK_KEY},
                    ).scalar()
                )
                conn.commit()
            except Exception:
                logger.warning(
                    "Error while acquiring the migration advisory lock; "
                    "proceeding without it",
                    exc_info=True,
                )
                yield False
                return
            if acquired:
                break
            if time.monotonic() >= deadline:
                logger.warning(
                    "Could not acquire the migration advisory lock within %ss "
                    "(another replica is migrating); proceeding without it",
                    _MIGRATION_LOCK_TIMEOUT,
                )
                yield False
                return
            logger.info(
                "Another replica holds the migration lock; waiting for it to "
                "finish migrating"
            )
            time.sleep(_MIGRATION_LOCK_POLL_SECONDS)

        logger.info("Acquired migration advisory lock %s", _MIGRATION_LOCK_KEY)
        try:
            yield True
        finally:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _MIGRATION_LOCK_KEY},
                )
                conn.commit()
                logger.info("Released migration advisory lock %s", _MIGRATION_LOCK_KEY)
            except Exception:
                # Session-scoped, so closing the connection releases it anyway.
                logger.warning("Failed to release migration advisory lock", exc_info=True)
    finally:
        conn.close()


def migrate_db():
    """
    Run migrations to make sure the DB is up-to-date.

    Serialized behind an advisory lock so only one replica applies a pending
    migration (see `_migration_lock`).

    Runs in gunicorn's `on_starting`, i.e. before the socket binds, so the
    Deployment needs a `startupProbe` sized to the worst-case migration or the
    liveness probe kills the pod mid-migration.
    """
    if os.environ.get("SKIP_DB_CREATION", "false") == "true":
        logger.info("Skipping running migrations...")
        return None

    logger.info("Running migrations...")
    config = get_alembic_config()
    with _migration_lock():
        alembic.command.upgrade(config, "head")
    logger.info("Finished migrations")


