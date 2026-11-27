"""ASSISTANT_DISABLED_KINDS and kinds.registry.disable_kinds: a draft kind held back.

Its tool is not offered and its Confirm answers 409, while its drafts still settle and
can be discarded. The setting and the calls share one set of switched-off kinds.
"""
import ast
import dataclasses
import logging
import re
from pathlib import Path
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient

from src.services.assistant import lifecycle
from src.services.assistant.config import ENABLED_ENV_VAR
from src.services.assistant.core import metrics
from src.services.assistant.kinds import alert_status, registry
from src.services.assistant.models.db import Draft
from src.services.assistant.tools.change_alert_status import SPEC
from src.services.assistant.tools.registry import TOOLS, offered_tools
from tests.assistant.fakes import TOKEN, FakeGateway, whoami
from tests.assistant.rows import add_conversation, add_draft, add_message, ago, read
from tests.assistant.running import (
    Exchange,
    FakeLlm,
    Keep,
    calls,
    change,
    equip,
    post_chat,
    post_confirm,
    says,
)

NAME = alert_status.NAME
SETTING = "ASSISTANT_DISABLED_KINDS"
HOWS = ("setting", "call")


@pytest.fixture(autouse=True)
def nothing_switched_off(monkeypatch):
    """Each test starts with every kind on; what it switches off ends with it."""
    monkeypatch.setattr(registry, "_disabled_by_code", {})
    registry._kinds_in_setting.cache_clear()
    yield
    registry._kinds_in_setting.cache_clear()
    metrics.kind_enabled.clear()


def switch_off(how: str, set_env, *names: str) -> None:
    names = names or (NAME,)
    if how == "setting":
        set_env(SETTING, ",".join(names))
    else:
        registry.disable_kinds(names, "not_on_this_deployment")


def gauge(kind: str = NAME) -> float:
    return metrics.kind_enabled.labels(kind=kind)._value.get()


def counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def errors_about(caplog, text: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and getattr(r, "kind", None) == text
    ]


# --- which kinds are off ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["", " ", " , ,", "\t\n"])
def test_nothing_is_off_by_default_or_when_blank(set_env, value):
    set_env(SETTING, value)
    assert registry.disabled_kinds() == frozenset()
    assert not registry.is_disabled(NAME)
    assert offered_tools() == TOOLS


@pytest.mark.parametrize(
    "value", [NAME, f" {NAME} ", f"{NAME},", f",{NAME}, {NAME}", f"other {NAME}"]
)
def test_the_setting_names_kinds(set_env, value):
    set_env(SETTING, value)
    assert registry.disabled_kinds() == {NAME}
    assert registry.is_disabled(NAME)
    assert NAME not in offered_tools()
    # Still a kind: its drafts settle, expire and can be discarded.
    assert registry.kind_of(NAME) is alert_status.KIND
    assert NAME in registry.KINDS and NAME in registry.PAYLOAD_MODELS


def test_a_typo_is_logged_once_and_ignored(set_env, caplog):
    set_env(SETTING, "change_alert_statsu, change_alert_statsu,save_everything")
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            assert registry.disabled_kinds() == frozenset()
    for typo in ("change_alert_statsu", "save_everything"):
        [record] = errors_about(caplog, typo)
        assert record.setting == SETTING


def test_the_setting_is_read_when_used(set_env):
    assert registry.disabled_kinds() == frozenset()
    set_env(SETTING, NAME)  # and the settings are read again, as after a restart
    assert registry.disabled_kinds() == {NAME}


@pytest.mark.parametrize("how", HOWS)
def test_the_gauge(set_env, how):
    lifecycle._start_series()
    assert gauge() == 1
    switch_off(how, set_env)
    lifecycle._start_series()
    assert gauge() == 0


def test_the_series_of_a_refusal_exist_from_the_start():
    metrics.drafts_total.clear()
    lifecycle._start_series()
    for kind in (*registry.KINDS, "unknown"):
        assert counter(metrics.drafts_total, kind=kind, event="unsupported") == 0


# --- disable_kinds ------------------------------------------------------------------------


def test_a_call_switches_a_kind_off(caplog):
    with caplog.at_level(logging.DEBUG):
        assert registry.disable_kinds([NAME], "no_capp_here") is None
    assert registry.disabled_kinds() == {NAME}
    assert NAME not in offered_tools()
    assert gauge() == 0
    [record] = [r for r in caplog.records if getattr(r, "reason", None)]
    assert (record.levelname, record.kind, record.reason) == (
        "WARNING",
        NAME,
        "no_capp_here",
    )


def test_the_reason_is_logged_once_per_kind(caplog):
    with caplog.at_level(logging.DEBUG):
        registry.disable_kinds([NAME, NAME], "first_reason")
        registry.disable_kinds([NAME], "second_reason")
        registry.disable_kinds([], "third_reason")
    reasons = [r.reason for r in caplog.records if getattr(r, "reason", None)]
    assert reasons == ["first_reason"]
    assert registry._disabled_by_code == {NAME: "first_reason"}


@pytest.mark.parametrize(
    "names", [["save_everything"], [NAME, "save_everything"], [""], [NAME.upper()]]
)
def test_an_unknown_kind_raises_and_changes_nothing(names):
    with pytest.raises(ValueError):
        registry.disable_kinds(names, "not_on_this_deployment")
    assert registry.disabled_kinds() == frozenset()


def test_one_name_is_not_a_list_of_names():
    with pytest.raises(TypeError):
        registry.disable_kinds(NAME, "not_on_this_deployment")
    assert registry.disabled_kinds() == frozenset()


@pytest.mark.parametrize(
    "reason",
    ["Bad Reason", "", "a" * 41, "with-dash", "digits_1", "line\n", "über", None, 7],
)
def test_a_reason_is_a_fixed_identifier(reason):
    with pytest.raises(ValueError):
        registry.disable_kinds([NAME], reason)
    assert registry.disabled_kinds() == frozenset()


def test_the_longest_reason():
    registry.disable_kinds([NAME], "a" * 40)
    assert registry._disabled_by_code == {NAME: "a" * 40}


def test_the_setting_and_a_call_switch_off_together(
    monkeypatch, set_env, no_read_tools
):
    other = "other_kind"
    monkeypatch.setattr(
        registry,
        "KINDS",
        MappingProxyType({**registry.KINDS, other: alert_status.KIND}),
    )
    monkeypatch.setitem(
        TOOLS, other, dataclasses.replace(SPEC, name=other, draft_kind=other)
    )
    set_env(SETTING, other)
    assert registry.disabled_kinds() == {other}
    registry.disable_kinds([NAME], "not_on_this_deployment")
    assert registry.disabled_kinds() == {NAME, other}
    assert offered_tools() == {}
    lifecycle._start_series()
    assert gauge(NAME) == gauge(other) == 0


def test_a_tool_goes_with_its_draft_kind_and_not_its_name(
    monkeypatch, set_env, no_read_tools
):
    for spec in TOOLS.values():
        assert spec.draft_kind is None or spec.draft_kind in registry.KINDS
    reads = dataclasses.replace(SPEC, name="read_alerts", draft_kind=None)
    renamed = dataclasses.replace(SPEC, name="status_change")
    for spec in (reads, renamed):
        monkeypatch.setitem(TOOLS, spec.name, spec)
    set_env(SETTING, NAME)
    assert set(offered_tools()) == {"read_alerts"}


def test_there_is_no_way_back_on():
    assert [n for n in vars(registry) if re.search(r"(?<!dis)enabl", n.lower())] == []
    tree = ast.parse(Path(registry.__file__).read_text("utf-8"))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Delete)
        if isinstance(node, ast.Attribute):
            assert getattr(node.value, "id", "") != "_disabled_by_code", node.attr


# --- through the app ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def keep(app):
    keep = Keep({"fp-1": "firing", "fp-2": "firing"})
    equip(app, gateway=keep)
    return keep


@pytest.fixture()
def conversation(db):
    return add_conversation(db, updated_at=ago(days=3))


@pytest.mark.asyncio
@pytest.mark.parametrize("how", HOWS)
async def test_its_tool_and_guidance_are_not_offered(
    app, db, set_env, how, no_read_tools
):
    switch_off(how, set_env)
    gateway = FakeGateway().on_json("GET", "/whoami", whoami())
    gateway.on_json("GET", "/alerts/facets/fields", ["name"])
    llm = FakeLlm(calls(change("fp-1")), says("I cannot."))
    equip(app, llm, gateway)
    exchange = await post_chat(app, "acknowledge fp-1").finish()
    assert exchange.status == 200
    first = llm.requests[0]
    assert first.tools == []
    assert SPEC.prompt_guidance not in first.messages[0]["content"]
    # A model that calls it anyway gets what an unknown tool gets.
    assert '"code":"unknown_tool"' in llm.requests[1].messages[-1]["content"]
    assert "draft" not in exchange.names and "tool_status" not in exchange.names
    assert "POST /alerts/query" not in gateway.paths()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", HOWS)
async def test_its_confirm_is_refused_before_the_claim(
    app, db, keep, conversation, set_env, how
):
    draft = add_draft(db, conversation, "fp-1", "fp-2")
    switch_off(how, set_env)
    before = read(db, Draft, draft)
    count = counter(metrics.drafts_total, kind=NAME, event="unsupported")
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (409, "draft_unsupported")
    assert "draft" not in exchange.json()
    assert read(db, Draft, draft) == before
    assert keep.looks == keep.writes == []
    assert counter(metrics.drafts_total, kind=NAME, event="unsupported") == count + 1


@pytest.mark.asyncio
async def test_a_kind_this_version_does_not_know_counts_as_unknown(
    app, db, keep, conversation
):
    draft = add_draft(db, conversation, kind="from_the_future")
    count = counter(metrics.drafts_total, kind="unknown", event="unsupported")
    exchange = await post_confirm(app, draft).finish()
    assert (exchange.status, exchange.code) == (409, "draft_unsupported")
    assert counter(metrics.drafts_total, kind="unknown", event="unsupported") == count + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", HOWS)
async def test_its_applying_draft_still_settles(
    app, db, keep, conversation, set_env, how
):
    switch_off(how, set_env)
    keep.statuses = {"fp-1": "acknowledged", "fp-2": "acknowledged"}
    draft = add_draft(
        db, conversation, "fp-1", "fp-2", state="applying", updated_at=ago(seconds=31)
    )
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    exchange = await Exchange(
        app, "GET", f"/assistant/conversations/{conversation}"
    ).finish()
    assert exchange.status == 200
    [view] = [i["draft"] for i in exchange.json()["items"] if i["type"] == "draft"]
    assert (view["state"], view["outcome"]["code"]) == ("applied", "applied")
    assert read(db, Draft, draft)["state"] == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", HOWS)
async def test_its_draft_can_still_be_discarded(
    app, db, keep, conversation, set_env, how
):
    draft = add_draft(db, conversation, "fp-1")
    switch_off(how, set_env)
    exchange = await Exchange(app, "POST", f"/assistant/drafts/{draft}/discard").finish()
    assert exchange.status == 200
    assert exchange.json()["state"] == "discarded"
    assert keep.writes == []


# --- from the lifespan --------------------------------------------------------------------


@pytest.fixture()
def main(gateway_main, monkeypatch):
    """src.main whose lifespan starts the assistant only, and no loops."""

    async def nothing():
        pass

    monkeypatch.setattr(gateway_main, "startup", nothing)
    monkeypatch.setattr(gateway_main, "shutdown", nothing)
    for job in ("KEEP_DEBUG_TASKS", "KEEP_ACTIVE_USERS_JOB", "KEEP_INCIDENT_METRICS_JOB"):
        monkeypatch.setattr(gateway_main, job, False)
    monkeypatch.setattr(lifecycle, "_start_loops", lambda work: None)
    return gateway_main


@pytest.mark.parametrize("when", ["before_the_series", "after_the_rest"])
def test_a_kind_switched_off_in_the_lifespan_is_off_for_the_first_request(
    main, make_app, set_env, monkeypatch, db, when
):
    set_env(ENABLED_ENV_VAR, "true")
    app = make_app()
    start = lifecycle.start
    series = lifecycle._start_series

    def switching_off_first():
        registry.disable_kinds([NAME], "not_on_this_deployment")
        series()

    def start_then_switch_off(app):
        start(app)
        registry.disable_kinds([NAME], "not_on_this_deployment")

    if when == "before_the_series":
        monkeypatch.setattr(lifecycle, "_start_series", switching_off_first)
    else:
        monkeypatch.setattr(lifecycle, "start", start_then_switch_off)
    draft = add_draft(db, add_conversation(db), "fp-1")
    keep = Keep({"fp-1": "firing"})
    with TestClient(app, headers={"Authorization": TOKEN}) as client:
        assert gauge() == 0
        app.state.assistant_gateway = keep.client()
        response = client.post(f"/assistant/drafts/{draft}/confirm")
        assert response.status_code == 409
        assert response.json()["errors"][0]["code"] == "draft_unsupported"
        assert read(db, Draft, draft)["state"] == "pending"
    assert keep.writes == []
