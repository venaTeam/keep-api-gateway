"""KEEP_ASSISTANT_ENABLED: nothing of the assistant exists without it."""
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from src.services.assistant import lifecycle
from src.services.assistant.api.guard import AssistantGuardMiddleware
from src.services.assistant.config import ENABLED_ENV_VAR
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from tests.assistant.conftest import TEST_OPENAI_KEY
from tests.assistant.fakes import TOKEN
from tests.assistant.logs import rendered

HEADERS = {"Authorization": TOKEN}
CANARY = "canary-51be07"


@pytest.fixture()
def main(gateway_main, monkeypatch):
    """src.main whose lifespan starts nothing but the assistant."""
    stopped = []

    async def startup():
        pass

    async def shutdown():
        stopped.append(True)

    monkeypatch.setattr(gateway_main, "startup", startup)
    monkeypatch.setattr(gateway_main, "shutdown", shutdown)
    for job in (
        "KEEP_DEBUG_TASKS",
        "KEEP_ACTIVE_USERS_JOB",
        "KEEP_INCIDENT_METRICS_JOB",
    ):
        monkeypatch.setattr(gateway_main, job, False)
    gateway_main.stopped = stopped
    return gateway_main


def has_guard(app) -> bool:
    return any(m.cls is AssistantGuardMiddleware for m in app.user_middleware)


def assistant_paths(app) -> list[str]:
    return [r.path for r in app.routes if r.path.startswith("/assistant")]


def configured_gauge() -> float:
    return metrics.llm_configured._value.get()


# --- off ---------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "false", "0"])
def test_off_is_the_default_and_mounts_nothing(
    main, assistant_router, monkeypatch, value
):
    @assistant_router.get("/scratch/off")
    async def scratch():
        return {}

    if value is None:
        monkeypatch.delenv(ENABLED_ENV_VAR)
    else:
        monkeypatch.setenv(ENABLED_ENV_VAR, value)
    app = main.get_app()
    assert assistant_paths(app) == []
    assert not has_guard(app)
    client = TestClient(app)
    for path in ("/assistant/chat", "/assistant/scratch/off", "/assistant"):
        for headers in ({}, HEADERS):
            for response in (
                client.get(path, headers=headers),
                client.post(path, headers=headers, json={"message": "hi"}),
            ):
                assert response.status_code == 404
                assert response.json() == {"detail": "Not Found"}


def test_off_does_not_even_import_the_routes(main, monkeypatch):
    monkeypatch.delitem(sys.modules, "src.routes.assistant", raising=False)
    main.get_app()
    assert "src.routes.assistant" not in sys.modules


def test_off_builds_no_openai_client_even_with_a_key(main, monkeypatch):
    monkeypatch.setattr(lifecycle, "build_llm_client", lambda: pytest.fail("built"))
    monkeypatch.setattr(lifecycle, "start", lambda app: pytest.fail("started"))
    monkeypatch.setattr(lifecycle, "stop", lambda app: pytest.fail("stopped"))
    app = main.get_app()
    with TestClient(app) as client:
        assert client.get("/assistant/chat", headers=HEADERS).status_code == 404
        assert not hasattr(app.state, "assistant_llm")
    assert main.stopped == [True]


# --- on ------------------------------------------------------------------------


@pytest.fixture()
def app(main, make_app, set_env):
    set_env(ENABLED_ENV_VAR, "true")  # the lifespan reads it too
    return make_app()


def test_on_mounts_the_router_behind_the_guard(
    main, assistant_router, make_app, schema_ready
):
    @assistant_router.get("/scratch/on")
    async def scratch():
        return {"ok": True}

    app = make_app()
    assert has_guard(app)
    client = TestClient(app)
    assert client.get("/assistant/scratch/on").status_code == 401
    response = client.get("/assistant/scratch/on", headers=HEADERS)
    assert (response.status_code, response.json()) == (200, {"ok": True})


def test_the_worker_holds_one_llm_client(app, main, set_env):
    set_env("OPENAI_MODEL_NAME", "some-model")
    with TestClient(app):
        llm = app.state.assistant_llm
        assert llm.model == "some-model"
        assert llm._client.api_key == TEST_OPENAI_KEY
        assert configured_gauge() == 1
        assert not llm._client.is_closed()
    assert llm._client.is_closed()
    assert main.stopped == [True]


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("OPENAI_API_KEY", "", "OPENAI_API_KEY is not set"),
        ("OPENAI_BASE_URL", "http://user:pw@proxy.test", "OPENAI_BASE_URL must be https"),
    ],
)
def test_the_app_starts_without_a_usable_client(
    app, main, set_env, caplog, setting, value, message
):
    set_env(setting, value)
    with TestClient(app) as client:
        assert app.state.assistant_llm is None
        assert configured_gauge() == 0
        assert client.get("/").status_code == 200
    assert main.stopped == [True]
    assert any(r.levelname == "ERROR" and message in r.message for r in caplog.records)
    assert "pw@" not in caplog.text


def test_a_client_that_cannot_be_built_does_not_stop_the_gateway(
    app, main, monkeypatch, caplog
):
    def fail():
        raise RuntimeError(CANARY)

    monkeypatch.setattr(lifecycle, "build_llm_client", fail)
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        assert app.state.assistant_llm is None
        assert configured_gauge() == 0
        assert client.get("/").status_code == 200
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert any(getattr(r, "error_type", "") == "RuntimeError" for r in caplog.records)


def test_a_failing_stop_is_logged_without_its_text(app, main, monkeypatch, caplog):
    async def fail():
        raise RuntimeError(CANARY)

    with caplog.at_level(logging.DEBUG), TestClient(app):
        monkeypatch.setattr(app.state.assistant_llm, "aclose", fail)
    assert main.stopped == [True]
    assert not [r for r in caplog.records if CANARY in rendered(r)]
    assert any(getattr(r, "error_type", "") == "RuntimeError" for r in caplog.records)


def test_the_engine_is_disposed_with_the_worker(app, db_engine):
    with TestClient(app):
        engine = db_core.get_engine()
    assert db_core.get_engine() is not engine
