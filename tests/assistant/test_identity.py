import logging
import uuid

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette_context import plugins
from starlette_context.middleware import RawContextMiddleware

from src.services.assistant.api.deps import get_identity
from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.exceptions import AssistantError, error_body
from src.services.assistant.models.identity import Identity
from tests.assistant.fakes import TOKEN, FakeGateway, whoami

URL = "/scratch/me"


@pytest.fixture()
def app():
    app = FastAPI()
    app.add_middleware(RawContextMiddleware, plugins=(plugins.RequestIdPlugin(),))

    @app.exception_handler(AssistantError)
    async def handle(request, exc):
        return JSONResponse(error_body(exc.code, exc.message), exc.status, exc.headers)

    return app


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def gateway(app, client):
    @app.get(URL)
    async def me(identity: Identity = Depends(get_identity)):
        return {
            "tenant_id": identity.tenant_id,
            "email": identity.email,
            "scopes": sorted(identity.scopes) if identity.scopes is not None else None,
            "token_forwarded": identity.authorization == TOKEN,
            "repr": repr(identity) + repr(identity.caller(uuid.uuid4())),
        }

    fake = FakeGateway().on_json("GET", "/whoami", whoami())
    app.state.assistant_gateway = fake.client()
    return fake


def call(client, *headers):
    return client.get(URL, headers=list(headers) or [("Authorization", TOKEN)])


def code(response):
    return response.json()["errors"][0]["code"]


def test_identity_of_a_valid_caller(client, gateway):
    body = call(client).json()
    assert body["tenant_id"] == "keep"
    assert body["email"] == "ops@example.com"
    assert body["scopes"] == ["read:*", "write:alert"]
    assert body["token_forwarded"] is True
    # Neither object may carry the token into a log line.
    assert "signature" not in body["repr"]


def test_whoami_carries_only_the_callers_token_and_a_uuid(client, gateway):
    response = call(
        client,
        ("Authorization", TOKEN),
        ("X-Keep-Source", "ui"),
        ("X-API-KEY", "someones-key"),
        ("Cookie", "session=1"),
    )
    assert response.status_code == 200
    sent = gateway.requests[0].headers
    assert sent["Authorization"] == TOKEN
    assert sent["X-Request-ID"] == response.headers["X-Request-ID"]
    uuid.UUID(sent["X-Request-ID"])
    for name in ("x-keep-source", "x-api-key", "cookie"):
        assert name not in sent


def test_whoami_is_asked_on_every_request(client, gateway):
    call(client)
    gateway.on_json("GET", "/whoami", {}, status=401)
    assert call(client).status_code == 401
    assert len(gateway.requests) == 2


# --- the caller's header -----------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        [("X-Nothing", "1")],
        [("Authorization", "")],
        [("Authorization", "Bearer")],
        [("Authorization", "Bearer  ")],
        [("Authorization", "Basic dXNlcjprZXk=")],
        [("Authorization", "Digest some-api-key")],
        [("Authorization", "header.payload.signature")],
        [("Authorization", TOKEN), ("Authorization", "Basic dXNlcjprZXk=")],
        [("Authorization", TOKEN), ("Authorization", TOKEN)],
        [("X-API-KEY", "some-api-key")],
    ],
)
def test_anything_but_one_bearer_header_is_refused(client, gateway, headers):
    response = call(client, *headers)
    assert response.status_code == 401
    assert code(response) == "unauthenticated"
    assert response.json()["detail"]
    assert gateway.requests == []


def test_the_scheme_is_case_insensitive_and_forwarded_verbatim(client, gateway):
    assert call(client, ("Authorization", "bearer a.b.c")).status_code == 200
    assert gateway.requests[0].headers["Authorization"] == "bearer a.b.c"


# --- what the gateway answers ------------------------------------------------


@pytest.mark.parametrize(
    "answer, status, expected",
    [
        (httpx.Response(401, json={"detail": "x"}), 401, "unauthenticated"),
        (httpx.Response(403, json={"detail": "x"}), 403, "forbidden"),
        (httpx.Response(500, text="boom"), 502, "gateway_unavailable"),
        (httpx.Response(503), 502, "gateway_unavailable"),
        (httpx.Response(404), 502, "gateway_unavailable"),
        (httpx.Response(302, headers={"Location": "/x"}), 502, "gateway_unavailable"),
        (httpx.ConnectError("secret host"), 502, "gateway_unavailable"),
        (httpx.ReadTimeout("secret host"), 502, "gateway_unavailable"),
        (httpx.Response(200, text="<html>"), 503, "gateway_incompatible"),
        (httpx.Response(200, json=[1]), 503, "gateway_incompatible"),
    ],
)
def test_gateway_failures(client, gateway, answer, status, expected):
    gateway.on("GET", "/whoami", answer)
    response = call(client)
    assert response.status_code == status
    assert code(response) == expected
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        # What a gateway from before this feature answers.
        {"tenant_id": "keep", "role": "admin"},
        {"email": "a@example.com", "scopes": None},
        {"tenant_id": "", "email": "a@example.com"},
        {"tenant_id": 5, "email": "a@example.com"},
    ],
)
def test_incompatible_bodies_fail_closed(client, gateway, body, caplog):
    gateway.on_json("GET", "/whoami", body)
    counter = metrics.internal_errors_total.labels(site="gateway_incompatible")
    before = counter._value.get()
    response = call(client)
    assert response.status_code == 503
    assert code(response) == "gateway_incompatible"
    assert counter._value.get() == before + 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("email", [None, "", "  ", 7])
def test_no_email_is_not_an_old_gateway(client, gateway, email):
    gateway.on_json("GET", "/whoami", whoami(email=email))
    counter = metrics.internal_errors_total.labels(site="identity_unavailable")
    before = counter._value.get()
    response = call(client)
    assert response.status_code == 403
    assert code(response) == "identity_unavailable"
    assert counter._value.get() == before + 1


@pytest.mark.parametrize(
    "scopes, expected",
    [(None, None), ([], []), (["b", "a", 3], ["a", "b"]), ("write:alert", None)],
)
def test_scopes_are_a_hint(client, gateway, scopes, expected):
    gateway.on_json("GET", "/whoami", whoami(scopes=scopes))
    assert call(client).json()["scopes"] == expected


def test_absent_scopes_mean_unknown(client, gateway):
    body = whoami()
    del body["scopes"]
    gateway.on_json("GET", "/whoami", body)
    assert call(client).json()["scopes"] is None


def test_whoami_has_its_own_timeout():
    assert get_settings().gateway_whoami_timeout_seconds == 3
