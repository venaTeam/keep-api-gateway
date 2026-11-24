"""Where the assistant's settings come from, and what must not reach them."""
import dataclasses
import logging
import os

import pytest

from src.config import consts
from src.services.assistant import config
from src.services.assistant.config import Settings, get_settings, is_enabled
from src.services.assistant.core import db as db_core
from tests.assistant.conftest import NO_DATABASE_DSN, TEST_OPENAI_KEY


def test_tests_never_use_the_developer_environment():
    assert consts.DB_CONNECTION_STRING == NO_DATABASE_DSN
    assert get_settings().openai_api_key == TEST_OPENAI_KEY
    for setting in dataclasses.fields(Settings):
        if setting.name != "openai_api_key":
            assert os.environ[setting.metadata["env"]] == ""


def test_the_environment_is_read_on_first_use_not_at_import(monkeypatch):
    monkeypatch.setenv("ASSISTANT_DRAFT_TTL_SECONDS", "77")
    config.reset_settings()
    assert get_settings().draft_ttl_seconds == 77
    monkeypatch.setenv("ASSISTANT_DRAFT_TTL_SECONDS", "78")
    assert get_settings().draft_ttl_seconds == 77


def test_every_name_is_the_assistants_own_or_a_shared_openai_one():
    names = {f.metadata["env"] for f in dataclasses.fields(Settings)}
    shared = {"OPENAI_API_KEY", "OPENAI_MODEL_NAME", "OPENAI_BASE_URL"}
    assert shared <= names
    assert all(name.startswith("ASSISTANT_") for name in names - shared)


def test_an_unusable_value_falls_back_to_the_default(set_env, caplog):
    set_env("ASSISTANT_DB_POOL_SIZE", "three-secret")
    with caplog.at_level(logging.ERROR):
        assert get_settings().db_pool_size == 3
    assert [r.setting for r in caplog.records] == ["ASSISTANT_DB_POOL_SIZE"]
    assert "three-secret" not in str([vars(r) for r in caplog.records])


def test_repr_hides_the_key_and_the_url(set_env):
    set_env("OPENAI_BASE_URL", "https://user:pw@proxy.test")
    text = repr(get_settings())
    assert TEST_OPENAI_KEY not in text
    assert "pw@" not in text


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        ("", False),
        ("false", False),
        ("0", False),
        ("off", False),
        ("anything", False),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        (" on ", True),
    ],
)
def test_the_switch(set_env, value, enabled):
    set_env("KEEP_ASSISTANT_ENABLED", value)
    assert is_enabled() is enabled


def test_the_switch_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("KEEP_ASSISTANT_ENABLED")
    assert is_enabled() is False


# --- the database comes from the gateway, its pool does not ------------------------


def test_the_gateways_pool_settings_and_database_url_do_not_apply(set_env):
    for name in (
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT",
    ):
        set_env(name, 31)
    set_env("DATABASE_URL", "postgresql://elsewhere.test/db")
    engine = db_core.get_engine()
    assert engine.url.render_as_string(hide_password=False) == NO_DATABASE_DSN
    assert (engine.pool.size(), engine.pool._max_overflow) == (3, 2)
    assert engine.pool._timeout == 3


def test_the_pool_follows_the_assistants_settings(set_env):
    set_env("ASSISTANT_DB_POOL_SIZE", 1)
    set_env("ASSISTANT_DB_MAX_OVERFLOW", 0)
    set_env("ASSISTANT_DB_POOL_TIMEOUT", "0.5")
    pool = db_core.get_engine().pool
    assert (pool.size(), pool._max_overflow, pool._timeout) == (1, 0, 0.5)


@pytest.mark.parametrize(
    "dsn", [None, "", "impersonate", "sqlite:///./keep.db", "mysql+pymysql://"]
)
def test_any_other_kind_of_database_is_not_configured(monkeypatch, dsn):
    monkeypatch.setattr(consts, "DB_CONNECTION_STRING", dsn)
    with pytest.raises(db_core.DatabaseNotConfiguredError) as raised:
        db_core.get_engine()
    assert not str(raised.value)
