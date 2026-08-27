"""The migration entrypoint the Argo PreSync Job runs.

Deliberately NOT a subcommand of `keep` (`src/utils/cli/cli.py`), which imports
`ProvidersFactory` at module scope and would drag the whole provider stack into
a container whose only job is DDL. Standalone also means moving it to
`keep-migrations` later is a file copy, not an untangling.

Direction is derived, never commanded: the target is compared to the revision
stamped in `alembic_version` and the walk goes whichever way the graph says.
The default target is `head` and nothing exists past head, so a normal release
can never downgrade.

Exit codes: 0 converged (including nothing to do), 1 refused, 2 failed.
"""

import logging
import os
import sys
from io import StringIO

import alembic.command
import click
from alembic.script import ScriptDirectory

from src.repositories.db_on_start import (
    _migration_lock,
    _script_directory,
    get_alembic_config,
    get_db_revision,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 1

# A downgrade path that only restamps is one whose migrations never wrote a real
# `downgrade()` -- it reports success while changing nothing. Matched against the
# SQL upper-cased.
_NO_OP_MARKER = "UPDATE ALEMBIC_VERSION"
_DESTRUCTIVE_MARKERS = ("DROP TABLE", "DROP COLUMN")


def _resolve(script: ScriptDirectory, target: str) -> str:
    """The concrete revision `target` names ('head' -> a hash)."""
    try:
        revision = script.get_revision(target)
    except Exception as exc:
        raise click.ClickException(f"Cannot resolve target '{target}': {exc}")
    if revision is None:
        raise click.ClickException(f"Target revision '{target}' is not in this image")
    return revision.revision


def _is_ancestor(script: ScriptDirectory, candidate: str, descendant: str) -> bool:
    """True if `candidate` is `descendant` or one of its ancestors."""
    try:
        return any(
            r.revision == candidate for r in script.iterate_revisions(descendant, "base")
        )
    except Exception:
        return False


def _direction(script: ScriptDirectory, db_revision: str | None, target: str) -> str:
    """One of 'upgrade', 'downgrade', 'converged'.

    A database stamped with a revision this image has never heard of is not a
    direction -- it is a target the image cannot reason about, and guessing would
    walk the schema somewhere nobody asked for.
    """
    if db_revision is None:
        # Fresh database: no alembic_version, nothing to compare.
        return "upgrade"
    if db_revision == target:
        return "converged"
    if _is_ancestor(script, db_revision, target):
        return "upgrade"
    if _is_ancestor(script, target, db_revision):
        return "downgrade"
    raise click.ClickException(
        f"Database revision '{db_revision}' and target '{target}' are on divergent "
        "branches, or the database was stamped by an image newer than this one. "
        "Refusing to guess a direction."
    )


def _offline_sql(config, db_revision: str | None, target: str, direction: str) -> str:
    """The SQL the run would emit, without touching the database.

    Alembic writes offline SQL to stdout, so capture it rather than plumb a buffer
    through the config.
    """
    buffer = StringIO()
    original = sys.stdout
    # `--sql` needs an explicit start:end range; an empty start means base.
    span = f"{db_revision or ''}:{target}"
    try:
        sys.stdout = buffer
        if direction == "downgrade":
            alembic.command.downgrade(config, span, sql=True)
        else:
            alembic.command.upgrade(config, span, sql=True)
    finally:
        sys.stdout = original
    return buffer.getvalue()


def _destructive_reason(sql: str) -> str | None:
    """Why this path should not run unattended, or None if it is safe."""
    upper = sql.upper()
    hits = [m for m in _DESTRUCTIVE_MARKERS if m in upper]
    if hits:
        return f"the path emits {', '.join(hits)} -- data leaves the database"

    statements = [
        line.strip()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    if statements and all(_NO_OP_MARKER in s.upper() for s in statements):
        return (
            "the path only restamps alembic_version -- every migration crossed has "
            "an empty downgrade(), so this reports success while changing nothing"
        )
    return None


@click.command()
@click.option(
    "--target", default="head", show_default=True, help="Revision to converge on."
)
@click.option(
    "--sql", "as_sql", is_flag=True, help="Print the SQL and exit; touches nothing."
)
@click.option(
    "--check", is_flag=True, help="Resolve the path and refuse if destructive."
)
@click.option(
    "--allow-destructive",
    is_flag=True,
    help="Run a path --check would refuse. Required for any downgrade.",
)
def main(target: str, as_sql: bool, check: bool, allow_destructive: bool) -> None:
    """Converge the database on TARGET, in whichever direction the graph says."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = get_alembic_config()
    script = _script_directory()
    if script is None:
        raise click.ClickException("Could not load this image's migration scripts")

    target_revision = _resolve(script, target)
    db_revision = get_db_revision()
    direction = _direction(script, db_revision, target_revision)

    logger.info(
        "database=%s target=%s (%s) direction=%s",
        db_revision or "<empty>",
        target,
        target_revision,
        direction,
    )

    # The hook fires on every sync, not every release, so the common case is
    # nothing pending: exit without invoking alembic at all.
    if direction == "converged":
        logger.info("Database is already at the target; nothing to do")
        sys.exit(EXIT_OK)

    if as_sql or check:
        sql = _offline_sql(config, db_revision, target_revision, direction)
        if as_sql:
            click.echo(sql)
        if check:
            reason = _destructive_reason(sql)
            if reason:
                logger.error("Refusing the %s: %s", direction, reason)
                sys.exit(EXIT_OK if allow_destructive else EXIT_REFUSED)
            logger.info("Path is safe to run unattended")
        sys.exit(EXIT_OK)

    if direction == "downgrade" and not allow_destructive:
        raise click.ClickException(
            "Refusing to downgrade without --allow-destructive. Downgrade one "
            "release, not many: long paths break the single-transaction guarantee "
            "and most downgrade() functions have never run anywhere."
        )

    with _migration_lock():
        # Re-read under the lock: a concurrent run may have converged already.
        db_revision = get_db_revision()
        if _direction(script, db_revision, target_revision) == "converged":
            logger.info("Another run reached the target while we waited; nothing to do")
            sys.exit(EXIT_OK)
        if direction == "downgrade":
            alembic.command.downgrade(config, target_revision)
        else:
            alembic.command.upgrade(config, target_revision)

    logger.info("Database converged on %s", target_revision)
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
