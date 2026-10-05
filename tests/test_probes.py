"""
Tests for the gateway probe endpoints.

`/healthcheck` returns `{}` unconditionally. That is correct for **liveness** —
if an HTTP server replies at all it can serve, and checking dependencies there
would restart every replica at once on a Postgres blip — but useless as
readiness: it stays green with the DB unreachable, the schema missing a table
this image's models declare. `/readyz` is the probe target that means
something.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import healthcheck


@pytest.fixture
def probe_client():
    app = FastAPI()
    app.include_router(healthcheck.router)
    return TestClient(app)


def test_healthcheck_is_liveness_and_dependency_free(probe_client):
    """A liveness probe that fails on a Postgres blip restarts every replica."""
    with patch.object(healthcheck, "_check_db", side_effect=AssertionError("no DB")):
        response = probe_client.get("/healthcheck")

    assert response.status_code == 200
    assert response.json() == {}  # unchanged contract; existing probes keep working


def test_readyz_ok_when_schema_satisfied(probe_client):
    with patch.object(
        healthcheck, "_check_db", return_value=(True, {"reachable": True, "satisfied": True})
    ):
        response = probe_client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_503_when_schema_does_not_satisfy_this_image(probe_client):
    """A pod whose database is missing a table or column its own models declare
    must not be advertised ready — that is what makes readiness trustworthy as a
    sync-wave gate for keep-event-handler."""
    with patch.object(
        healthcheck,
        "_check_db",
        return_value=(False, {"reachable": True, "satisfied": False}),
    ):
        response = probe_client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["database"]["satisfied"] is False


def test_readyz_503_when_database_unreachable(probe_client):
    with patch.object(
        healthcheck,
        "_check_db",
        return_value=(False, {"reachable": False}),
    ):
        response = probe_client.get("/readyz")

    assert response.status_code == 503


def test_readyz_does_not_block_the_event_loop_on_a_slow_db(probe_client):
    """The DB check must run off the loop: parking the event loop on every probe
    (~every 10 s) for as long as a sick Postgres takes to answer would stall live
    request handling in that worker."""
    import threading

    loop_thread_ids = []

    def slow_check():
        loop_thread_ids.append(threading.get_ident())
        return True, {"reachable": True, "satisfied": True}

    with patch.object(healthcheck, "_check_db", side_effect=slow_check):
        response = probe_client.get("/readyz")

    assert response.status_code == 200
    # It ran on a worker thread, not the thread running the event loop.
    assert loop_thread_ids and loop_thread_ids[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_readyz_reports_a_timeout_instead_of_hanging(monkeypatch):
    """A hung dependency must produce a prompt 503, not a probe that never
    answers.

    Exercised against the handler directly: a blocking check cannot be
    cancelled, so the orphaned worker thread is joined when the event loop of a
    TestClient portal is torn down — which would be charged to the request and
    hide the property under test. A long-lived server loop has no such teardown.
    """
    import time

    from fastapi import Response

    monkeypatch.setattr(healthcheck, "READYZ_CHECK_TIMEOUT", 0.1)

    def hanging_check():
        time.sleep(2)
        return True, {}

    with patch.object(healthcheck, "_check_db", side_effect=hanging_check):
        started = time.monotonic()
        body = await healthcheck.readyz(Response())
        elapsed = time.monotonic() - started

    assert elapsed < 1
    assert body["status"] == "unavailable"
    assert "timed out" in body["checks"]["database"]["error"]


def test_check_db_reports_unreachable_database():
    engine = MagicMock()
    engine.connect.side_effect = OSError("connection refused")
    with patch("src.repositories.db.engine", engine):
        ok, detail = healthcheck._check_db()

    assert ok is False
    assert detail["reachable"] is False
    assert "OSError" in detail["error"]


def test_check_db_reports_what_the_schema_is_missing():
    engine = MagicMock()
    missing = {"missing_tables": ["operator"], "missing_columns": {"alert": ["team"]}}
    with patch("src.repositories.db.engine", engine):
        with patch(
            "src.repositories.db_on_start.schema_drift",
            return_value=(False, missing),
        ):
            ok, detail = healthcheck._check_db()

    assert ok is False
    assert detail["satisfied"] is False
    assert detail["missing"] == missing


def test_check_db_passes_when_the_schema_satisfies_the_models():
    engine = MagicMock()
    with patch("src.repositories.db.engine", engine):
        with patch("src.repositories.db_on_start.schema_drift", return_value=(True, {})):
            ok, detail = healthcheck._check_db()

    assert ok is True
    assert detail == {"reachable": True, "satisfied": True, "missing": {}}
