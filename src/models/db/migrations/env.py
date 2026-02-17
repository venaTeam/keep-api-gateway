import asyncio
import os
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy.future import Connection
from sqlmodel import SQLModel

import src.utils.logging
from src.repositories.db_utils import create_db_engine
from src.models.db.action import *
from src.models.db.ai_suggestion import *
from src.models.db.alert import *
from src.models.db.dashboard import *
from src.models.db.extraction import *
from src.models.db.facet import *
from src.models.db.maintenance_window import *
from src.models.db.mapping import *
from src.models.db.preset import *
from src.models.db.provider import *
from src.models.db.rule import *
from src.models.db.secret import *
from src.models.db.statistics import *
from src.models.db.tenant import *
from src.models.db.topology import *
from src.models.db.user import *

target_metadata = SQLModel.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # backup the current config
    logging_config = config.get_section("loggers")
    fileConfig(config.config_file_name)


async def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    connectable = create_db_engine()
    context.configure(
        url=str(connectable.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Run actual sync migrations.

    :param connection: connection to the database.
    """
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = create_db_engine()
    try:
        do_run_migrations(connectable.connect())
    except Exception as e:
        # print all migrations so we will know what failed
        list_migrations(connectable)
        raise e


def list_migrations(connectable):
    """
    List all migrations and their status for debugging.
    """
    try:
        # Get the script directory from the alembic context
        script_directory = ScriptDirectory.from_config(config)
        current_rev = script_directory.get_current_head()
        # List all available migrations
        pid = os.getpid()
        print(f"[{pid}] Available migrations:")
        try:
            for script in script_directory.walk_revisions():
                status = (
                    "PENDING"
                    if current_rev and script.revision > current_rev
                    else "APPLIED"
                )
                print(f"  - {script.revision}: {script.doc} ({status})")
        except Exception as exc:
            logger.exception(f"Failed to list migrations: {exc}")
    except Exception as exc:
        logger.exception(f"Failed to process migration information: {exc}")


loop = asyncio.get_event_loop()
if context.is_offline_mode():
    task = run_migrations_offline()
else:
    task = run_migrations_online()

loop.run_until_complete(task)
# SHAHAR: set back the logs to the default after alembic is done
src.utils.logging.setup_logging()

