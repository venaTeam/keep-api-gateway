"""KEEP_ASSISTANT_ENABLED: nothing of the assistant exists without it."""
import asyncio
import logging
import random
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.services.assistant import lifecycle
from src.services.assistant.api.guard import AssistantGuardMiddleware
from src.services.assistant.bl import retention, sweeps
from src.services.assistant.config import ENABLED_ENV_VAR
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from src.services.assistant.core.loops import Loops
from tests.assistant.conftest import TEST_OPENAI_KEY
from tests.assistant.fakes import TOKEN, FakeGateway, caller, whoami
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


def loops_started(monkeypatch) -> list:
    """(name, what it runs, seconds between two runs) of every loop from now on."""
    started = []
    start = Loops.start

    def recording(self, name, run, *, seconds):
        started.append((name, run, seconds))
        start(self, name, run, seconds=seconds)

    monkeypatch.setattr(Loops, "start", recording)
    return started


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
    monkeypatch.setattr(lifecycle, "settle", lambda app: pytest.fail("settled"))
    monkeypatch.setattr(lifecycle, "stop", lambda app: pytest.fail("stopped"))
    app = main.get_app()
    with TestClient(app) as client:
        assert client.get("/assistant/chat", headers=HEADERS).status_code == 404
        for name in ("llm", "gateway", "work"):
            assert not hasattr(app.state, f"assistant_{name}")
    assert main.stopped == [True]


def test_off_starts_no_loops(main, monkeypatch):
    # Not through `start` either: nothing may begin to delete or expire rows.
    started = loops_started(monkeypatch)
    with TestClient(main.get_app()) as client:
        assert client.get("/").status_code == 200
    assert started == []


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


def test_chat_without_a_key_is_the_only_thing_refused(app, main, set_env, schema_ready):
    set_env("OPENAI_API_KEY", "")
    with TestClient(app, headers=HEADERS) as client:
        app.state.assistant_gateway = (
            FakeGateway().on_json("GET", "/whoami", whoami()).client()
        )
        response = client.post("/assistant/chat", json={"message": "hi"})
        assert response.status_code == 503
        assert response.json()["errors"][0]["code"] == "llm_not_configured"
        assert client.get("/").status_code == 200


def test_on_starts_the_loops_and_ends_them_with_the_worker(app, main, monkeypatch):
    started = loops_started(monkeypatch)
    ended = []
    monkeypatch.setattr(random, "uniform", lambda low, high: 0.0)
    entered = threading.Event()

    def run(stop):
        entered.set()
        ended.append(stop.wait(10))

    monkeypatch.setattr(retention, "run", run)
    with TestClient(app):
        assert sorted(started) == [
            ("draft_sweeps", sweeps.run, 3600.0),
            ("retention", run, 86400),
        ]
        assert entered.wait(5)
        stopping = time.monotonic()
    assert time.monotonic() - stopping < 5
    deadline = time.monotonic() + 5
    while not ended and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ended == [True]


def test_retention_can_be_switched_off(app, main, monkeypatch, set_env, caplog):
    set_env("ASSISTANT_RETENTION_ENABLED", "false")
    started = loops_started(monkeypatch)
    with TestClient(app):
        assert [name for name, _, _ in started] == ["draft_sweeps"]
    assert "retention is switched off" in caplog.text


def test_loops_that_cannot_start_do_not_stop_the_gateway(
    app, main, monkeypatch, caplog
):
    def fail(self, name, run, *, seconds):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(Loops, "start", fail)
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        assert client.get("/").status_code == 200
    assert main.stopped == [True]
    assert not [r for r in caplog.records if CANARY in rendered(r)]


# --- shutdown --------------------------------------------------------------------


def test_the_worker_waits_for_writes_next_to_its_own_shutdown(app, main, monkeypatch):
    """With the LLM client still open: what is waited for may need it."""
    seen = {}

    async def shutdown():
        seen["shutdown started"] = time.monotonic()
        await asyncio.sleep(0.5)
        seen["shutdown ended"] = time.monotonic()

    monkeypatch.setattr(main, "shutdown", shutdown)

    async def write(request):
        await asyncio.sleep(0.5)
        seen["write ended"] = time.monotonic()
        seen["llm open"] = not app.state.assistant_llm._client.is_closed()
        seen["draining"] = app.state.assistant_work.draining
        return httpx.Response(200, json={"status": "ok"})

    gateway = FakeGateway().on("POST", "/alerts/batch_enrich", write).client()
    with TestClient(app) as client:
        app.state.assistant_gateway = gateway

        @app.get("/scratch/abandon")
        async def abandon():
            result = await gateway.batch_enrich_status(
                caller(), ["a"], "acknowledged", None, False, timeout=0.01
            )
            return {"result": result}

        assert client.get("/scratch/abandon").json() == {"result": "unknown"}
    assert seen["llm open"] and seen["draining"]
    assert seen["write ended"] < seen["shutdown ended"] + 0.4
    assert seen["shutdown started"] < seen["write ended"]
    assert gateway._http.is_closed


def test_a_write_that_does_not_end_is_not_waited_for_for_long(
    app, main, monkeypatch, caplog
):
    monkeypatch.setattr(lifecycle, "SHUTDOWN_GRACE_SECONDS", 0.2)

    async def write(request):
        await asyncio.sleep(30)

    gateway = FakeGateway().on("POST", "/alerts/batch_enrich", write).client()
    with TestClient(app) as client:
        app.state.assistant_gateway = gateway

        @app.get("/scratch/abandon")
        async def abandon():
            await gateway.batch_enrich_status(
                caller(), ["a"], "acknowledged", None, False, timeout=0.01
            )

        client.get("/scratch/abandon")
        started = time.monotonic()
    assert time.monotonic() - started < 5
    assert main.stopped == [True]
    [line] = [r for r in caplog.records if "shutting down with work left" in r.message]
    assert line.writes == 1


def test_a_failure_to_settle_does_not_cut_the_shutdown_short(
    app, main, monkeypatch, caplog
):
    async def fail(timeout):
        raise RuntimeError(CANARY)

    with caplog.at_level(logging.DEBUG), TestClient(app):
        monkeypatch.setattr(app.state.assistant_work, "settle", fail)
        llm = app.state.assistant_llm
    assert main.stopped == [True]
    assert llm._client.is_closed()
    assert not [r for r in caplog.records if CANARY in rendered(r)]


def test_the_engine_is_disposed_with_the_worker(app, db_engine):
    with TestClient(app):
        engine = db_core.get_engine()
    assert db_core.get_engine() is not engine
