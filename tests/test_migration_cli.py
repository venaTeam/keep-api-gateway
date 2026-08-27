"""Tests for the migration entrypoint the Argo PreSync Job runs.

The hook fires on every sync, not every release, so the case that matters most is
the boring one: nothing pending, exit 0 without invoking alembic.
"""

import os
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from src.utils.cli import migrations


def _script(ancestry):
    """A ScriptDirectory stand-in. `ancestry` maps a revision to those it
    descends from, itself included; an absent revision is one this image's
    scripts have never heard of, which is what alembic raises on."""
    script = MagicMock()

    def iterate_revisions(upper, lower):
        if upper not in ancestry:
            raise Exception(f"Can't locate revision identified by '{upper}'")
        return [MagicMock(revision=rev) for rev in ancestry[upper]]

    def get_revision(target):
        resolved = "rev3" if target == "head" else target
        if resolved not in ancestry:
            return None
        return MagicMock(revision=resolved)

    script.iterate_revisions.side_effect = iterate_revisions
    script.get_revision.side_effect = get_revision
    return script


LINE = {"rev3": ["rev3", "rev2", "rev1"], "rev2": ["rev2", "rev1"], "rev1": ["rev1"]}


def _run(args, db_revision, ancestry=None, sql=""):
    with patch.object(
        migrations, "_script_directory", return_value=_script(ancestry or LINE)
    ), patch.object(
        migrations, "get_db_revision", return_value=db_revision
    ), patch.object(
        migrations, "get_alembic_config", return_value=MagicMock()
    ), patch.object(
        migrations, "_offline_sql", return_value=sql
    ), patch.object(
        migrations, "_migration_lock"
    ), patch(
        "alembic.command.upgrade"
    ) as upgrade, patch(
        "alembic.command.downgrade"
    ) as downgrade:
        result = CliRunner().invoke(migrations.main, args)
    return result, upgrade, downgrade


def test_nothing_pending_exits_zero_without_invoking_alembic():
    """The hook fires on every sync. A release with no new migrations must exit
    in seconds."""
    result, upgrade, downgrade = _run([], db_revision="rev3")
    assert result.exit_code == 0
    upgrade.assert_not_called()
    downgrade.assert_not_called()


def test_pending_revisions_upgrade_to_head():
    result, upgrade, _ = _run([], db_revision="rev1")
    assert result.exit_code == 0
    upgrade.assert_called_once()
    assert upgrade.call_args.args[1] == "rev3"


def test_fresh_database_upgrades():
    """No alembic_version yet: nothing to compare against."""
    result, upgrade, _ = _run([], db_revision=None)
    assert result.exit_code == 0
    upgrade.assert_called_once()


def test_downgrade_is_refused_without_allow_destructive():
    result, _, downgrade = _run(["--target", "rev1"], db_revision="rev3")
    assert result.exit_code != 0
    downgrade.assert_not_called()


def test_downgrade_runs_with_allow_destructive():
    result, _, downgrade = _run(
        ["--target", "rev1", "--allow-destructive"], db_revision="rev3"
    )
    assert result.exit_code == 0
    downgrade.assert_called_once()
    assert downgrade.call_args.args[1] == "rev1"


def test_revision_unknown_to_this_image_refuses_rather_than_guessing():
    """An image older than the database cannot reason about the stamped revision,
    and walking the schema somewhere nobody asked for is worse than failing the
    sync."""
    result, upgrade, downgrade = _run([], db_revision="rev9")
    assert result.exit_code != 0
    upgrade.assert_not_called()
    downgrade.assert_not_called()


def test_unknown_target_is_an_error():
    result, upgrade, _ = _run(["--target", "nope"], db_revision="rev1")
    assert result.exit_code != 0
    upgrade.assert_not_called()


def test_sql_prints_without_touching_the_database():
    result, upgrade, _ = _run(["--sql"], db_revision="rev1", sql="BEGIN;\nSELECT 1;\n")
    assert result.exit_code == 0
    assert "SELECT 1" in result.output
    upgrade.assert_not_called()


def test_check_passes_a_safe_path():
    result, upgrade, _ = _run(
        ["--check"], db_revision="rev1", sql="ALTER TABLE t ADD COLUMN c INT;"
    )
    assert result.exit_code == 0
    upgrade.assert_not_called()


def test_check_refuses_a_path_that_drops_data():
    result, _, _ = _run(
        ["--check", "--target", "rev1"],
        db_revision="rev3",
        sql="DROP TABLE preset;\nUPDATE alembic_version SET version_num='rev1';",
    )
    assert result.exit_code == migrations.EXIT_REFUSED


def test_check_refuses_a_downgrade_that_only_restamps():
    """17 of 122 migrations have an empty downgrade(): the path reports success
    while changing nothing."""
    result, _, _ = _run(
        ["--check", "--target", "rev1"],
        db_revision="rev3",
        sql="UPDATE alembic_version SET version_num='rev2';\n"
        "UPDATE alembic_version SET version_num='rev1';",
    )
    assert result.exit_code == migrations.EXIT_REFUSED


def test_allow_destructive_overrides_check():
    result, _, _ = _run(
        ["--check", "--target", "rev1", "--allow-destructive"],
        db_revision="rev3",
        sql="DROP TABLE preset;",
    )
    assert result.exit_code == 0


def test_converged_is_rechecked_under_the_lock():
    """Concurrent runs serialize on the advisory lock; the loser must notice the
    winner already converged rather than re-running the path."""
    revisions = iter(["rev1", "rev3"])  # behind before the lock, at head under it
    with patch.object(
        migrations, "_script_directory", return_value=_script(LINE)
    ), patch.object(
        migrations, "get_db_revision", side_effect=lambda: next(revisions)
    ), patch.object(
        migrations, "get_alembic_config", return_value=MagicMock()
    ), patch.object(
        migrations, "_migration_lock"
    ), patch(
        "alembic.command.upgrade"
    ) as upgrade:
        result = CliRunner().invoke(migrations.main, [])
    assert result.exit_code == 0
    upgrade.assert_not_called()


def test_destructive_reason_ignores_sql_comments():
    """Alembic's offline output is mostly `-- Running upgrade ...` comment lines;
    a no-op downgrade must still be recognised through them."""
    sql = (
        "-- Running downgrade rev3 -> rev2\n"
        "UPDATE alembic_version SET version_num='rev2';\n"
        "-- Running downgrade rev2 -> rev1\n"
        "UPDATE alembic_version SET version_num='rev1';\n"
    )
    assert migrations._destructive_reason(sql) is not None
    assert migrations._destructive_reason("ALTER TABLE t ADD COLUMN c INT;") is None
