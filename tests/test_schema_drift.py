"""Tests for the models-vs-live-schema drift check.

This is what replaces `--autogenerate` as a drift detector. Autogenerate helped
you *write* a migration; it never verified the ones already written. This does,
from two places: CI against a scratch database with every migration applied, and
/readyz against the live one.

The check is one-directional on purpose, so the test that matters most is the
one proving an EXTRA column still passes -- that is what makes image rollback
work.
"""

from unittest.mock import MagicMock, patch

from src.repositories import db_on_start


def _drift(declared, live):
    """Run the check against a fixed declared set and live schema."""
    with patch.object(db_on_start, "declared_tables", return_value=declared), patch.object(
        db_on_start, "live_tables", return_value=live
    ), patch.object(db_on_start, "_schema_drift_ok_cache", False):
        return db_on_start.schema_drift()


def test_exact_match_is_satisfied():
    ok, missing = _drift({"alert": {"id", "name"}}, {"alert": {"id", "name"}})
    assert ok is True
    assert missing == {}


def test_extra_column_in_the_database_is_ignored():
    """Image rollback: an older image declares fewer columns, finds them all,
    and must be happy. This is the whole reason the check is one-directional."""
    ok, missing = _drift({"alert": {"id"}}, {"alert": {"id", "added_later"}})
    assert ok is True
    assert missing == {}


def test_extra_table_in_the_database_is_ignored():
    """The automation tables are migrated here but modelled in
    keep-automation-api, so they only ever appear in this direction -- which is
    why the check needs no exclusion list."""
    ok, _ = _drift({"alert": {"id"}}, {"alert": {"id"}, "automations": {"id"}})
    assert ok is True


def test_missing_column_is_named():
    ok, missing = _drift({"alert": {"id", "ticket_url"}}, {"alert": {"id"}})
    assert ok is False
    assert missing["missing_columns"] == {"alert": ["ticket_url"]}
    assert missing["missing_tables"] == []


def test_missing_table_is_named():
    ok, missing = _drift({"alert": {"id"}, "preset": {"id"}}, {"alert": {"id"}})
    assert ok is False
    assert missing["missing_tables"] == ["preset"]


def test_success_is_cached_but_failure_is_not():
    """Mirrors `_script_directory`: caching a transient failure would leave the
    probe reporting not-ready for the life of the process."""
    with patch.object(db_on_start, "_schema_drift_ok_cache", False), patch.object(
        db_on_start, "declared_tables", return_value={"alert": {"id", "gone"}}
    ), patch.object(db_on_start, "live_tables", return_value={"alert": {"id"}}) as live:
        assert db_on_start.schema_drift()[0] is False
        assert db_on_start.schema_drift()[0] is False
        assert live.call_count == 2  # re-read, not cached

    with patch.object(db_on_start, "_schema_drift_ok_cache", False), patch.object(
        db_on_start, "declared_tables", return_value={"alert": {"id"}}
    ), patch.object(db_on_start, "live_tables", return_value={"alert": {"id"}}) as live:
        assert db_on_start.schema_drift()[0] is True
        assert db_on_start.schema_drift()[0] is True
        assert live.call_count == 1  # cached after the first success


def test_declared_tables_covers_every_model_module():
    """The point of all_models.py: the declared set must not depend on which code
    paths a given deployment happens to import. `user` and `secret` are the two
    that are otherwise conditional."""
    from src.models.db.all_models import declared_tables

    declared = declared_tables()
    assert "user" in declared
    assert "secret" in declared
    # The five env.py never imported, which a stray --autogenerate would have dropped.
    for table in (
        "enrichmentevent",
        "externalaiconfigandmetadata",
        "providerimage",
        "system",
    ):
        assert table in declared, table


def test_live_tables_uses_reflection_on_sqlite():
    """sqlite has no information_schema; only tests and single-process dev run
    there, where the extra round trips are free."""
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    inspector = MagicMock()
    inspector.get_table_names.return_value = ["alert"]
    inspector.get_columns.return_value = [{"name": "id"}, {"name": "name"}]
    with patch.object(db_on_start, "engine", engine), patch.object(
        db_on_start, "sa_inspect", return_value=inspector
    ):
        assert db_on_start.live_tables() == {"alert": {"id", "name"}}
    engine.connect.assert_not_called()
