"""Every draft kind behind one protocol: what Confirm, the reconcile and the sweep may
rely on, whatever the kind."""
import ast
import inspect
from pathlib import Path

import pytest

import src.services.assistant.kinds
from src.services.assistant.bl import confirm_bl
from src.services.assistant.kinds import alert_status, base, registry
from src.services.assistant.kinds.base import DraftKind, WriteRequest
from src.services.assistant.kinds.registry import KINDS, PAYLOAD_MODELS, kind_of
from src.services.assistant.models.api.draft import ChangeAlertStatusPayload
from src.services.assistant.tools.registry import TOOLS
from tests.assistant.rows import draft_payload

KINDS_PACKAGE = Path(src.services.assistant.kinds.__file__).parent

# What a pure method must not name: reads, writes, the database.
IMPURE = frozenset(
    {
        "reader",
        "gateway",
        "GatewayClient",
        "ReadGateway",
        "fetch_alerts_by_fingerprints",
        "run_db",
        "get_session",
        "Session",
        "session",
        "sa",
        "sqlalchemy",
        "sqlmodel",
        "drafts_bl",
        "db",
    }
)
PURE_METHODS = ("close_unknown", "event_text", "discarded_text")


def kind_sources() -> dict[Path, ast.Module]:
    paths = sorted(KINDS_PACKAGE.glob("*.py"))
    assert {p.name for p in paths} >= {
        "__init__.py",
        "base.py",
        "registry.py",
        "alert_set.py",
        "alert_status.py",
    }
    return {path: ast.parse(path.read_text("utf-8")) for path in paths}


def names_in(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        for field in ("id", "attr", "name", "module"):
            value = getattr(child, field, None)
            if isinstance(value, str):
                found.update(value.split("."))
    return found


# --- the registry ---------------------------------------------------------------------


def test_the_kinds_and_their_payload_models_agree():
    assert list(KINDS) == list(PAYLOAD_MODELS) == ["change_alert_status"]
    for name, kind in KINDS.items():
        assert isinstance(kind, DraftKind)
        assert kind.name == name
        assert PAYLOAD_MODELS[name] is kind.payload_model
    assert PAYLOAD_MODELS["change_alert_status"] is ChangeAlertStatusPayload


def test_a_kind_is_named_after_the_tool_that_makes_it():
    assert set(KINDS) <= set(TOOLS)


def test_the_tables_cannot_be_changed():
    with pytest.raises(TypeError):
        KINDS["from_the_future"] = alert_status.KIND  # type: ignore[index]
    with pytest.raises(TypeError):
        PAYLOAD_MODELS["from_the_future"] = ChangeAlertStatusPayload  # type: ignore[index]


def test_a_kind_this_version_does_not_know():
    assert kind_of("change_alert_status") is alert_status.KIND
    assert kind_of("from_the_future") is None
    assert kind_of("") is None


def test_the_payload_models_live_only_in_the_registry():
    """No second table that could disagree with this one."""
    from src.services.assistant.models.api import draft

    assert not hasattr(draft, "PAYLOAD_MODELS")


def test_every_kind_implements_the_whole_protocol():
    abstract = {
        name
        for name, member in vars(DraftKind).items()
        if getattr(member, "__isabstractmethod__", False)
    }
    assert abstract == {
        "write_scope",
        "plan",
        "reconcile",
        "close_unknown",
        "event_text",
        "discarded_text",
    }
    for kind in KINDS.values():
        for name in abstract:
            assert not getattr(getattr(kind, name), "__isabstractmethod__", False)
        assert inspect.iscoroutinefunction(kind.reconcile)
        for name in ("write_scope", "plan", *PURE_METHODS):
            assert not inspect.iscoroutinefunction(getattr(kind, name)), name


# --- scopes ---------------------------------------------------------------------------


def test_the_scope_a_status_change_needs():
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1"))
    kind = alert_status.KIND
    assert kind.write_scope(payload) == "write:alert"
    assert kind.scope_hint == frozenset({"write:alert"})


def test_a_kinds_scope_is_among_its_hints():
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1"))
    for kind in KINDS.values():
        assert kind.write_scope(payload) in kind.scope_hint
        assert all(isinstance(scope, str) and scope for scope in kind.scope_hint)


# --- purity -----------------------------------------------------------------------------


def test_the_close_and_the_texts_are_synchronous_and_pure():
    """The sweep runs in a thread with no event loop, and the texts are built inside
    the transaction that stores them: neither may read or write anything."""
    checked = set()
    for path, tree in kind_sources().items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in PURE_METHODS:
                continue
            assert isinstance(node, ast.FunctionDef), f"{path.name}: {node.name}"
            assert not [n for n in ast.walk(node) if isinstance(n, ast.Await)]
            used = names_in(node) & IMPURE
            assert not used, f"{path.name}: {node.name} names {used}"
            checked.add((path.name, node.name))
    # Every kind's own, and the protocol's.
    for kind in KINDS.values():
        module = Path(inspect.getfile(type(kind))).name
        for name in PURE_METHODS:
            assert (module, name) in checked
    assert {("base.py", name) for name in PURE_METHODS} <= checked


def test_kinds_never_hold_the_client_nor_the_database():
    for path, tree in kind_sources().items():
        used = names_in(tree)
        for name in ("GatewayClient", "get_session", "run_db", "sqlalchemy", "sqlmodel"):
            assert name not in used, f"{path.name} names {name}"


# --- the shapes -------------------------------------------------------------------------


def test_a_write_request_looks_after_as_long_as_confirm_does():
    request = WriteRequest("batch_enrich", {"fingerprints": [], "enrichments": {}})
    assert request.read_after_seconds == base.READ_AFTER_SECONDS
    assert base.READ_AFTER_SECONDS == confirm_bl.READ_SECONDS
    assert dict(request.path_params) == dict(request.query) == {}


def test_the_status_change_plan_declares_its_one_write():
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1"))
    plan = alert_status.KIND.plan(
        base.ConfirmContext(
            reader=None,  # type: ignore[arg-type]  # nothing is read until first()
            caller=None,  # type: ignore[arg-type]
            payload=payload,
            confirm_input=None,
            read_seconds=lambda: 4.0,
        )
    )
    assert plan.rows == ("batch_enrich",)


def test_the_registry_imports_every_kind_module():
    """A kind module that is not registered is not a kind."""
    tree = kind_sources()[KINDS_PACKAGE / "registry.py"]
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "src.services.assistant.kinds"
        for alias in node.names
    }
    assert imported == {"alert_status"}
    assert registry.KINDS["change_alert_status"] is alert_status.KIND
