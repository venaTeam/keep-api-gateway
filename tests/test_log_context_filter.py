"""
Tests for the automatic log context (tenant_id / trace_id on every record).
"""

import logging

import jwt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from src.middlewares import LoggingMiddleware, catch_exception
from src.utils.logging import LogContextFilter, update_log_context

TEST_LOGGER = "tests.log_context"


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured():
    handler = _ListHandler()
    handler.addFilter(LogContextFilter())
    test_logger = logging.getLogger(TEST_LOGGER)
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    yield handler.records
    test_logger.removeHandler(handler)


def _authenticate(request: Request):
    # sync, like AuthVerifierBase.__call__: runs in the threadpool
    update_log_context(tenant_id="tenant-from-auth")


@pytest.fixture
def client():
    app = FastAPI()
    log = logging.getLogger(TEST_LOGGER)

    @app.get("/plain")
    async def plain():
        log.info("inside request")
        return {}

    @app.get("/authed", dependencies=[Depends(_authenticate)])
    async def authed():
        log.info("after auth")
        return {}

    @app.get("/explicit")
    async def explicit():
        log.info(
            "explicit extra",
            extra={"tenant_id": "explicit-tenant", "trace_id": "explicit-trace"},
        )
        return {}

    app.add_middleware(LoggingMiddleware)

    # stands in for TraceIDMiddleware, which is outermost
    @app.middleware("http")
    async def set_trace_id(request: Request, call_next):
        request.state.trace_id = "trace-123"
        return await call_next(request)

    return TestClient(app)


def _token(tenant_id: str) -> str:
    return jwt.encode(
        {"keep_tenant_id": tenant_id}, "test-secret-" + "x" * 32, algorithm="HS256"
    )


def test_log_inside_request_gets_tenant_and_trace(client, captured):
    client.get("/plain", headers={"Authorization": f"Bearer {_token('tenant-a')}"})

    record = next(r for r in captured if r.getMessage() == "inside request")
    assert record.tenant_id == "tenant-a"
    assert record.trace_id == "trace-123"


def test_tenant_set_by_auth_is_seen_by_the_endpoint(client, captured):
    client.get("/authed")

    record = next(r for r in captured if r.getMessage() == "after auth")
    assert record.tenant_id == "tenant-from-auth"
    assert record.trace_id == "trace-123"


def test_explicit_extra_wins(client, captured):
    client.get("/explicit", headers={"Authorization": f"Bearer {_token('tenant-a')}"})

    record = next(r for r in captured if r.getMessage() == "explicit extra")
    assert record.tenant_id == "explicit-tenant"
    assert record.trace_id == "explicit-trace"


def test_context_does_not_leak_after_request(client, captured):
    client.get("/plain", headers={"Authorization": f"Bearer {_token('tenant-a')}"})
    logging.getLogger(TEST_LOGGER).info("after request")

    record = next(r for r in captured if r.getMessage() == "after request")
    assert not hasattr(record, "tenant_id")
    assert not hasattr(record, "trace_id")


def test_logging_outside_request_does_not_crash(captured):
    update_log_context(tenant_id="ignored")  # no-op outside a request
    logging.getLogger(TEST_LOGGER).info("outside request")

    record = next(r for r in captured if r.getMessage() == "outside request")
    assert not hasattr(record, "tenant_id")
    assert not hasattr(record, "trace_id")


def test_unhandled_exception_logs_request_line_and_one_traceback():
    handler = _ListHandler()
    middleware_logger = logging.getLogger("src.middlewares")
    middleware_logger.addHandler(handler)
    middleware_logger.setLevel(logging.INFO)

    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    app.add_exception_handler(Exception, catch_exception)
    app.add_middleware(LoggingMiddleware)

    @app.middleware("http")
    async def set_trace_id(request: Request, call_next):
        request.state.trace_id = "trace-123"
        return await call_next(request)

    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/boom", headers={"Authorization": f"Bearer {_token('tenant-a')}"}
        )
    finally:
        middleware_logger.removeHandler(handler)

    assert response.status_code == 500
    finished = [r for r in handler.records if r.getMessage() == "Request finished"]
    assert len(finished) == 1
    assert finished[0].levelno == logging.ERROR
    assert finished[0].status_code == 500
    assert finished[0].exc_info is None

    unhandled = [r for r in handler.records if r.getMessage() == "Unhandled exception"]
    assert len(unhandled) == 1
    assert unhandled[0].exc_info is not None
    assert unhandled[0].method == "GET"
    assert unhandled[0].path == "/boom"
    assert unhandled[0].tenant_id == "tenant-a"
    assert unhandled[0].trace_id == "trace-123"


def test_redact_headers_hides_secret_headers_any_case():
    from src.middlewares import _redact_headers

    headers = {
        "Authorization": "Bearer x",
        "Proxy-Authorization": "Basic x",
        "Cookie": "s=1",
        "X-Api-Key": "k",
        "X-Keep-Notify-Token": "t",
        "X-Forwarded-Access-Token": "t",
        "X-Auth-Request-Access-Token": "t",
        "Content-Type": "application/json",
    }
    redacted = _redact_headers(headers)
    assert redacted.pop("Content-Type") == "application/json"
    assert set(redacted.values()) == {"[REDACTED]"}
