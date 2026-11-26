"""The error contract and the guard, on scratch routes of the real app."""
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.services.assistant.api.deps import parse_resource_id
from src.services.assistant.api.guard import MAX_BODY_BYTES
from src.services.assistant.exceptions import ERRORS, AssistantError
from tests.assistant.fakes import TOKEN

SECRET = "secret-user-text-9f2c"


class Body(BaseModel):
    message: str
    count: int


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    @assistant_router.post("/scratch/body")
    async def body(payload: Body):
        return {"ok": True}

    @assistant_router.get("/scratch/error/{code}")
    async def error(code: str):
        raise AssistantError(
            code, headers={"Retry-After": "7"}, extra={"draft": {"id": "d-1"}}
        )

    @assistant_router.get("/scratch/http-error/{status}")
    async def http_error(status: int):
        raise HTTPException(status, SECRET, {"X-Kept": "1"})

    @assistant_router.get("/scratch/conversations/{conversation_id}")
    async def conversation(conversation_id: str):
        return {"id": parse_resource_id(conversation_id, "conversation_not_found")}

    @assistant_router.get("/scratch/drafts/{draft_id}")
    async def draft(draft_id: str):
        return {"id": parse_resource_id(draft_id, "draft_not_found")}

    return make_app()


@pytest.fixture()
def client(app, schema_ready):
    return TestClient(app, headers={"Authorization": TOKEN})


# --- errors -----------------------------------------------------------------


def test_validation_errors_are_400_with_fixed_messages(client):
    response = client.post("/assistant/scratch/body", json={"count": SECRET})
    assert response.status_code == 400
    body = response.json()
    assert {(e["field"], e["code"]) for e in body["errors"]} == {
        ("message", "field_required"),
        ("count", "invalid_value"),
    }
    assert body["detail"] == body["errors"][0]["message"]
    assert SECRET not in response.text


def test_malformed_json_is_400(client):
    response = client.post(
        "/assistant/scratch/body",
        content="{nope",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "invalid_value"
    assert response.json()["detail"]


@pytest.mark.parametrize("code", sorted(ERRORS))
def test_every_error_code_has_the_contract_shape(client, code):
    response = client.get(f"/assistant/scratch/error/{code}")
    assert response.status_code == ERRORS[code].status
    body = response.json()
    assert body["errors"] == [
        {"field": None, "code": code, "message": ERRORS[code].message}
    ]
    assert body["detail"] == ERRORS[code].message
    assert body["draft"] == {"id": "d-1"}
    assert response.headers["Retry-After"] == "7"


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "not_found"), (405, "method_not_allowed"), (418, "http_error")],
)
def test_http_errors_have_the_contract_shape(client, status, code):
    response = client.get(f"/assistant/scratch/http-error/{status}")
    assert response.status_code == status
    assert response.json() == {
        "errors": [{"field": None, "code": code, "message": ERRORS[code].message}],
        "detail": ERRORS[code].message,
    }
    assert response.headers["X-Kept"] == "1"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/assistant/scratch/conversations/not-a-uuid", "conversation_not_found"),
        ("/assistant/scratch/drafts/not-a-uuid", "draft_not_found"),
    ],
)
def test_malformed_ids_are_404(client, path, code):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == code
    assert response.json()["detail"]


def test_request_id_is_a_fresh_uuid(client):
    def request_id(path):
        response = client.get(path, headers={"X-Request-ID": "caller-value"})
        (value,) = response.headers.get_list("x-request-id")
        assert str(uuid.UUID(value)) == value
        return value

    found = f"/assistant/scratch/conversations/{uuid.uuid4()}"
    assert client.get(found).status_code == 200
    ids = [request_id(found), request_id(found)]
    ids.append(request_id("/assistant/scratch/drafts/not-a-uuid"))
    assert len(set(ids)) == 3


# --- the guard: one Bearer header --------------------------------------------


@pytest.mark.parametrize("path", ["/assistant", "/assistant/", "/assistant/any/path"])
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "")],
        [("Authorization", "Bearer")],
        [("Authorization", "Basic dXNlcjprZXk=")],
        [("Authorization", "Digest some-api-key")],
        [("Authorization", TOKEN), ("Authorization", "Basic dXNlcjprZXk=")],
        [("Authorization", TOKEN), ("Authorization", TOKEN)],
        [("X-API-KEY", "some-api-key")],
    ],
)
def test_the_guard_wants_one_bearer_header_first(app, path, headers):
    # No `schema_ready`: nothing behind the guard may run.
    response = TestClient(app).post(path, headers=headers, content=b"{nope")
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "unauthenticated"
    assert response.json()["detail"]
    assert response.headers["WWW-Authenticate"] == "Bearer"
    uuid.UUID(response.headers["X-Request-ID"])


@pytest.mark.parametrize("path", ["/", "/healthcheck", "/assistants", "/x/assistant/y"])
def test_other_paths_are_not_guarded(app, path):
    response = TestClient(app).post(
        path, headers={"Content-Length": str(MAX_BODY_BYTES + 1)}
    )
    assert response.status_code in (404, 405)


def test_the_gateways_own_error_shape_is_untouched(app):
    response = TestClient(app).get("/no/such/route")
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})


def test_a_cors_preflight_needs_no_token(app):
    response = TestClient(app).options(
        "/assistant/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code != 401


# --- the guard: body size -----------------------------------------------------


async def call(app, headers, chunks, path="/assistant/scratch/body"):
    """Drive the raw ASGI app; returns (status, body, chunks consumed)."""
    sent, consumed = [], 0

    async def receive():
        nonlocal consumed
        if consumed >= len(chunks):
            return {"type": "http.disconnect"}
        consumed += 1
        return {
            "type": "http.request",
            "body": chunks[consumed - 1],
            "more_body": consumed < len(chunks),
        }

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", TOKEN.encode()),
            *headers,
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return starts[0]["status"], json.loads(body), consumed


@pytest.mark.asyncio
async def test_declared_body_over_the_limit_is_rejected_unread(app, schema_ready):
    size = MAX_BODY_BYTES + 1
    status, body, consumed = await call(
        app, [(b"content-length", str(size).encode())], [b"x" * size]
    )
    assert status == 413
    assert body["errors"][0]["code"] == "payload_too_large"
    assert body["detail"]
    assert consumed == 0


@pytest.mark.asyncio
async def test_streamed_body_over_the_limit_is_rejected(app, schema_ready):
    chunks = [b"x" * (64 * 1024)] * 10
    status, body, consumed = await call(app, [], chunks)
    assert status == 413
    assert body["errors"][0]["code"] == "payload_too_large"
    assert consumed == 5  # stopped at the first chunk over 256 KiB


@pytest.mark.asyncio
async def test_body_at_the_limit_is_accepted(app, schema_ready):
    overhead = len(json.dumps({"message": "", "count": 1}))
    payload = json.dumps(
        {"message": "x" * (MAX_BODY_BYTES - overhead), "count": 1}
    ).encode()
    assert len(payload) == MAX_BODY_BYTES
    status, body, _ = await call(
        app, [(b"content-length", str(len(payload)).encode())], [payload]
    )
    assert (status, body) == (200, {"ok": True})
