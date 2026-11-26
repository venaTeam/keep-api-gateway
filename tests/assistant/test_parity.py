"""The chat can do exactly what its user can do: the gateway's own routes decide, with
its own verifier, through the whole app."""
import ast
import re
import typing
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import src.services.assistant
from src.models.alert import AlertStatus
from src.models.db.alert import AlertAudit, LastAlert
from src.services.assistant.config import ENABLED_ENV_VAR
from src.services.assistant.gateway.client import WRITE_ROUTES, GatewayClient
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.gateway.transport import IN_PROCESS_URL, InProcessTransport
from src.services.assistant.kinds.base import ConfirmContext
from src.services.assistant.models.db import Draft
from src.services.assistant.tools.base import ToolContext
from src.services.identity_manager import authverifierbase, rbac
from src.services.identity_manager.identity_managers.db import db_authverifier
from tests.assistant.rows import add_conversation, add_draft, read
from tests.assistant.running import (
    FakeLlm,
    calls,
    change,
    equip,
    post_chat,
    post_confirm,
    says,
)
from tests.fixtures.identity import TEST_JWT_SECRET, bearer_headers, user_email

AUTH = {
    "AUTH_TYPE": "SINGLE_TENANT",
    "KEEP_JWT_SECRET": TEST_JWT_SECRET,
    ENABLED_ENV_VAR: "true",
}
READ_ONLY = {
    **AUTH,
    "KEEP_READ_ONLY": "true",
    "KEEP_READ_ONLY_BYPASS_KEY": "bypass",
}
FINGERPRINTS = ("fp-1", "fp-2")
ALERT_NAME = "payments outage 4f21"

with_auth = pytest.mark.parametrize("test_app", [AUTH], indirect=True)


class SettingsReader(rbac.Role):
    """May ask who they are, and nothing else. No real role lacks read:alert and can
    still do that."""

    SCOPES = ["read:settings"]


class Recording(InProcessTransport):
    def __init__(self, app):
        super().__init__(app)
        self.answers: list[tuple[str, str, int]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        self.answers.append((request.method, request.url.path, response.status_code))
        return response

    def statuses(self, path: str) -> list[int]:
        return [status for _, asked, status in self.answers if asked == path]


@pytest.fixture()
def settings_reader(monkeypatch):
    real = rbac.get_role_by_role_name

    def with_settings_reader(name):
        return SettingsReader if name == "settings_reader" else real(name)

    for module in (rbac, authverifierbase, db_authverifier):
        monkeypatch.setattr(module, "get_role_by_role_name", with_settings_reader)
    return "settings_reader"


@pytest.fixture()
def alerts(db_session, create_alert):
    for fingerprint in FINGERPRINTS:
        create_alert(
            fingerprint,
            AlertStatus.FIRING,
            datetime.now(timezone.utc),
            {"name": ALERT_NAME},
        )


@pytest.fixture()
def state(db_session, alerts):
    """Everything a refused write must leave alone."""

    def read_state():
        db_session.expire_all()
        rows = db_session.query(LastAlert).order_by(LastAlert.fingerprint).all()
        return (
            [(r.fingerprint, r.status, r.note, r.dismiss_mode) for r in rows],
            db_session.query(AlertAudit).count(),
        )

    return read_state


@pytest.fixture()
def transport(test_app):
    return Recording(test_app)


def prepare(app, transport, llm=None) -> None:
    equip(app, llm, GatewayClient(IN_PROCESS_URL, transport))


def authorization(role: str) -> str:
    return bearer_headers(role)["Authorization"]


def draft_of(db, role: str):
    conversation = add_conversation(db, created_by=user_email(role))
    return add_draft(db, conversation, *FINGERPRINTS, created_by=user_email(role))


# --- reads -------------------------------------------------------------------------


@pytest.mark.asyncio
@with_auth
async def test_who_may_not_read_alerts_cannot_do_it_through_the_chat(
    db_session, test_app, alerts, clean_db, transport, settings_reader
):
    """The nested route itself says no. Nothing before it does."""
    llm = FakeLlm(calls(change("fp-1")), says("I cannot see that."))
    prepare(test_app, transport, llm)
    exchange = await post_chat(
        test_app, "ack fp-1", token=authorization(settings_reader)
    ).finish()
    assert exchange.status == 200, exchange.text
    assert transport.statuses("/whoami") == [200]
    assert transport.statuses("/alerts/query") == [403]
    assert "draft" not in exchange.names
    [reply] = [m for m in llm.requests[1].messages if m["role"] == "tool"]
    assert '"forbidden"' in reply["content"]
    assert ALERT_NAME not in str(llm.requests[1].messages)


@pytest.mark.asyncio
@with_auth
async def test_who_may_not_read_alerts_cannot_confirm(
    db_session, test_app, alerts, clean_db, state, transport, settings_reader
):
    draft = draft_of(clean_db, settings_reader)
    prepare(test_app, transport)
    before = state()
    exchange = await post_confirm(
        test_app, draft, token=authorization(settings_reader)
    ).finish()
    assert (exchange.status, exchange.code) == (403, "forbidden")
    assert transport.statuses("/alerts/query") == [403]
    assert transport.statuses("/alerts/batch_enrich") == []
    assert read(clean_db, Draft, draft)["state"] == "pending"
    assert state() == before


@pytest.mark.asyncio
@with_auth
async def test_who_may_read_alerts_gets_a_draft(
    db_session, test_app, alerts, clean_db, state, transport
):
    """The same chat as above, so that its refusal means something. Drafting writes
    nothing."""
    llm = FakeLlm(calls(change("fp-1")), says("Please confirm."))
    prepare(test_app, transport, llm)
    before = state()
    exchange = await post_chat(
        test_app, "ack fp-1", token=authorization("viewer")
    ).finish()
    draft = exchange.event("draft")["draft"]
    [alert] = draft["payload"]["alerts"]
    assert (alert["fingerprint"], alert["name"]) == ("fp-1", ALERT_NAME)
    # The card says in advance what the gateway will say.
    assert draft["permitted"] is False
    assert transport.statuses("/alerts/query") == [200]
    assert transport.statuses("/alerts/batch_enrich") == []
    assert state() == before


# --- writes ------------------------------------------------------------------------


@pytest.mark.asyncio
@with_auth
async def test_a_viewer_is_not_permitted(
    db_session, test_app, clean_db, state, transport
):
    draft = draft_of(clean_db, "viewer")
    prepare(test_app, transport)
    before = state()
    exchange = await post_confirm(
        test_app, draft, token=authorization("viewer")
    ).finish()
    assert exchange.status == 200, exchange.text
    assert transport.statuses("/alerts/batch_enrich") == [403]
    body = exchange.json()
    assert (body["state"], body["outcome"]["code"]) == ("failed", "not_permitted")
    assert body["outcome"]["applied_count"] == 0
    assert state() == before


@pytest.mark.asyncio
@with_auth
@pytest.mark.parametrize("role", ["noc", "editor", "admin"])
async def test_who_may_change_alerts_changes_them(
    db_session, test_app, clean_db, state, transport, role
):
    draft = draft_of(clean_db, role)
    prepare(test_app, transport)
    _, audits = state()
    exchange = await post_confirm(test_app, draft, token=authorization(role)).finish()
    body = exchange.json()
    assert (body["state"], body["outcome"]["code"]) == ("applied", "applied")
    assert transport.statuses("/alerts/batch_enrich") == [200]
    rows, audits_after = state()
    assert [(fp, status) for fp, status, *_ in rows] == [
        (fp, "acknowledged") for fp in FINGERPRINTS
    ]
    assert audits_after == audits + len(FINGERPRINTS)
    db_session.expire_all()
    for audit in db_session.query(AlertAudit).all():
        assert audit.user_id == user_email(role)
        assert "via Keep Assistant" in audit.description


@pytest.mark.asyncio
@pytest.mark.parametrize("test_app", [READ_ONLY], indirect=True)
@pytest.mark.parametrize("role", ["viewer", "noc", "admin"])
async def test_a_read_only_instance(
    db_session, test_app, clean_db, state, transport, role
):
    """Set before the verifier is built, as in a deployment."""
    draft = draft_of(clean_db, role)
    prepare(test_app, transport)
    before = state()
    exchange = await post_confirm(test_app, draft, token=authorization(role)).finish()
    body = exchange.json()
    assert (body["state"], body["outcome"]["code"]) == ("failed", "read_only")
    assert transport.statuses("/alerts/query") == [200]
    assert transport.statuses("/alerts/batch_enrich") == [403]
    assert state() == before


@pytest.mark.asyncio
@with_auth
async def test_a_token_of_someone_else_cannot_confirm(
    db_session, test_app, clean_db, state, transport
):
    draft = draft_of(clean_db, "noc")
    prepare(test_app, transport)
    before = state()
    exchange = await post_confirm(
        test_app, draft, token=authorization("admin")
    ).finish()
    assert (exchange.status, exchange.code) == (404, "draft_not_found")
    assert transport.statuses("/alerts/batch_enrich") == []
    assert state() == before


@pytest.mark.asyncio
@with_auth
@pytest.mark.parametrize("token", ["Bearer nonsense", "Bearer a.b.c"])
async def test_a_token_nobody_issued(
    db_session, test_app, clean_db, state, transport, token
):
    draft = draft_of(clean_db, "noc")
    prepare(test_app, transport)
    before = state()
    exchange = await post_confirm(test_app, draft, token=token).finish()
    assert (exchange.status, exchange.code) == (401, "unauthenticated")
    assert transport.answers == [("GET", "/whoami", 401)]
    assert state() == before


# --- the only way to a write ------------------------------------------------------------

PACKAGE = Path(src.services.assistant.__file__).parent
ROUTES = PACKAGE.parent.parent / "routes" / "assistant.py"
CONFIRM = PACKAGE / "bl" / "confirm_bl.py"
CLIENT = PACKAGE / "gateway" / "client.py"
TRANSPORT = PACKAGE / "gateway" / "transport.py"


def sources() -> dict[Path, ast.Module]:
    paths = [*PACKAGE.rglob("*.py"), ROUTES]
    assert CONFIRM in paths and CLIENT in paths and len(paths) > 30
    return {path: ast.parse(path.read_text("utf-8")) for path in paths}


def named(node: ast.AST, name: str) -> bool:
    """As an attribute, a plain name, a part of an import, a definition or a string
    that could be handed to getattr."""
    values = [
        getattr(node, field, None)
        for field in ("attr", "id", "name", "asname", "value", "module")
    ]
    return any(isinstance(v, str) and name in v.split(".") for v in values)


def mentions(tree: ast.AST, name: str) -> bool:
    return any(named(node, name) for node in ast.walk(tree))


def users(tree: ast.Module, name: str) -> set[str]:
    """The functions that name it, and "" for anything outside of a function."""
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    inside = {id(child) for node in functions for child in ast.walk(node)}
    outside = any(named(node, name) for node in ast.walk(tree) if id(node) not in inside)
    return {f.name for f in functions if mentions(f, name)} | ({""} if outside else set())


WRITE_NAMES = ("write", "_send", "_request")
LOOKUPS_BY_NAME = ("getattr", "hasattr", "setattr", "delattr")


def code_names(node: ast.AST) -> set[str]:
    """As an attribute, a plain name, a definition or a part of an import."""
    values = [getattr(node, field, None) for field in ("attr", "id", "name", "asname")]
    return {part for v in values if isinstance(v, str) for part in v.split(".")}


def test_only_confirm_can_write():
    """`GatewayClient.write` is the one way to change anything, and every write goes
    through a row of WRITE_ROUTES. Nobody but Confirm's `_send` asks for one."""
    trees = sources()
    for path, tree in trees.items():
        if path in (CLIENT, CONFIRM):
            continue
        used = {
            name
            for node in ast.walk(tree)
            for name in code_names(node) & set(WRITE_NAMES)
        }
        assert not used, f"{path.name} uses {used}"
    # (b) Nor can it be reached by a name in a string.
    for path, tree in trees.items():
        assert not mentions(tree, "attrgetter"), path.name
        assert not mentions(tree, "methodcaller"), path.name
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and getattr(call.func, "id", getattr(call.func, "attr", ""))
                in LOOKUPS_BY_NAME
            ):
                continue
            [name] = call.args[1:2] or [None]
            assert isinstance(name, ast.Constant) and isinstance(name.value, str), (
                f"{path.name}: a name that is not a literal"
            )
            assert "write" not in name.value.lower(), path.name

    # Confirm asks for it once, from `_send`.
    confirm = trees[CONFIRM]
    assert users(confirm, "write") == {"_send"}
    assert users(confirm, "_request") == set()
    writes = [
        node
        for node in ast.walk(confirm)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "write"
    ]
    assert [ast.unparse(call.func) for call in writes] == ["self._gateway.write"]

    # The client: only `write` reads the table and hands a route to `_send`, which
    # is what gives a call the reserved place and a place among the writes.
    client = trees[CLIENT]
    assert [p for p, tree in trees.items() if mentions(tree, "WRITE_ROUTES")] == [CLIENT]
    assert users(client, "WRITE_ROUTES") == {"", "write"}
    functions = [
        node
        for node in ast.walk(client)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    with_route = {
        function.name
        for function in functions
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and getattr(call.func, "attr", "") == "_send"
        and any(keyword.arg == "route" for keyword in call.keywords)
    }
    assert with_route == {"write"}
    # The method of the MVP is gone, not kept next to it.
    assert not [p for p, tree in trees.items() if mentions(tree, "batch_enrich_status")]


def test_only_the_client_talks_to_the_gateway():
    """Otherwise the test above proves nothing."""
    for path, tree in sources().items():
        strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        if path != CLIENT:
            assert not [s for s in strings if s.startswith(("/alerts", "/incidents"))]
            # Anywhere in a string: an address of its own.
            assert not [s for s in strings if "/alerts/" in s]
            assert not mentions(tree, "_send") or path == CONFIRM
            assert not mentions(tree, "_http")
            assert "gateway._request" not in path.read_text("utf-8")
        if path != TRANSPORT:
            assert not mentions(tree, "ASGITransport"), path.name
    # Confirm's own method of that name.
    assert "gateway._send" not in CONFIRM.read_text("utf-8")


def test_the_client_uses_httpx_in_one_place():
    assert users(sources()[CLIENT], "_http") == {"__init__", "aclose", "_request"}


TABLE_MAKERS = ("table", "Table", "reflect", "automap_base")


def test_the_assistants_sql_changes_only_its_own_tables():
    """Its sessions are on the database of the alerts: no statement that is only text,
    and no table but those of its models (the imports below)."""
    write = re.compile(r"(?i)\b(update\s+\w+\s+set|insert\s+into|delete\s+from)\b")
    for path, tree in sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not write.search(node.value), f"{path.name}: {node.value}"
            # sa.table("..."), and the like
            assert getattr(node, "attr", "") not in TABLE_MAKERS, path.name
            assert getattr(node, "name", "") not in TABLE_MAKERS, path.name


def test_the_client_calls_exactly_these_routes():
    calls = sorted(
        tuple(ast.unparse(arg) for arg in call.args[2:4])
        for call in ast.walk(sources()[CLIENT])
        if isinstance(call, ast.Call)
        and getattr(call.func, "attr", "") in ("_send", "_read", "_request", "request")
    )
    assert calls == sorted(
        [
            ("'GET'", "'/whoami'"),
            ("'POST'", "'/alerts/query'"),
            ("'POST'", "'/alerts/query/count'"),
            ("'GET'", "'/alerts/facets/fields'"),
            ("'GET'", "f\"/alerts/{quote(fingerprint, safe='')}\""),
            # What these go through.
            ("method", "path"),  # _read to _send
            ("route.method", "path"),  # write to _send
            ("path", "timeout"),  # _send to _request
            (),  # _request to httpx
        ]
    )
    # Every write there is, and all it may carry.
    assert {
        name: (
            route.method,
            route.path,
            route.path_params,
            dict(route.query),
            route.allowed_keys,
            route.check_body.__name__,
            route.classify.__name__,
            route.object_id.__name__,
            route.timeout_seconds,
        )
        for name, route in WRITE_ROUTES.items()
    } == {
        "batch_enrich": (
            "POST",
            "/alerts/batch_enrich",
            (),
            {"dispose_on_new_alert": frozenset({"true", "false"})},
            frozenset({"fingerprints", "enrichments"}),
            "_check_batch_enrich",
            "classify_batch_enrich",
            "_no_object",
            None,
        ),
    }


def test_nothing_is_imported_by_name():
    """The checks here read import statements."""
    for path, tree in sources().items():
        for name in ("importlib", "import_module", "__import__", "exec", "eval"):
            assert not mentions(tree, name), f"{path.name} uses {name}"


def test_nothing_of_the_chat_reaches_confirm():
    """Neither the turn nor a tool imports it."""
    importers = [
        path
        for path, tree in sources().items()
        if path != CONFIRM and mentions(tree, "confirm_bl")
    ]
    assert importers == [ROUTES]


def test_only_confirm_holds_the_client():
    """Kinds, tools, the turn and the look at a conversation get a ReadGateway, which
    has no write. Besides the client and the reader, only Confirm, the identity check
    and the lifespan that makes it name the client."""
    holders = {
        path.relative_to(PACKAGE.parent.parent).as_posix()
        for path, tree in sources().items()
        if any("GatewayClient" in code_names(node) for node in ast.walk(tree))
    }
    assert holders == {
        "services/assistant/gateway/client.py",
        "services/assistant/gateway/reader.py",
        "services/assistant/bl/confirm_bl.py",
        "services/assistant/api/deps.py",
        "services/assistant/lifecycle.py",
    }
    assert typing.get_type_hints(ToolContext)["gateway"] is ReadGateway
    assert typing.get_type_hints(ConfirmContext)["reader"] is ReadGateway


def test_the_rest_of_the_gateway_is_not_used_for_writes():
    """No service or repository that could change an alert is imported."""
    allowed = {
        "src.config",
        "src.repositories.prometheus_multiproc",
        "src.utils.request_id",
        "src.services.assistant",
    }
    for path, tree in sources().items():
        for node in ast.walk(tree):
            assert not getattr(node, "level", 0), f"{path.name}: a relative import"
            modules = (
                # `from src.repositories import db` names a module after `import`.
                [f"{node.module}.{alias.name}" for alias in node.names]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                assert not module.startswith("src") or any(
                    module == name or module.startswith(f"{name}.") for name in allowed
                ), f"{path.name} imports {module}"
