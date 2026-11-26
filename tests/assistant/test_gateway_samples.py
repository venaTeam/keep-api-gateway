"""What the client makes of real answers, captured by
`tests/test_assistant_gateway_samples.py` from the routes it calls.
"""
import json
from pathlib import Path

import httpx
import pytest

from src.services.assistant.bl.projection import (
    first_source,
    has_incident,
    has_note,
    last_received,
    project_alert,
)
from src.services.assistant.bl.scopes import has_scope
from src.services.assistant.gateway.client import FetchedAlert, GatewayClient
from src.services.assistant.gateway.errors import (
    GatewayForbidden,
    GatewayUnauthorized,
    GatewayUnavailable,
    InvalidCel,
)
from tests.assistant.fakes import caller, change_status

_FIXTURES = Path(__file__).parents[1] / "fixtures"
SAMPLES = json.loads((_FIXTURES / "assistant_gateway_samples.json").read_bytes())
READ_WRITE, READ_ONLY = SAMPLES["read_write"], SAMPLES["read_only"]


class Replay:
    """Answers with captured samples, and only to what they were captured for."""

    def __init__(self, *samples: dict):
        self._left = list(samples)

    async def _answer(self, request: httpx.Request) -> httpx.Response:
        sample = self._left.pop(0)
        sent = {
            "method": request.method,
            "path": request.url.raw_path.decode(),
            "body": json.loads(request.content) if request.content else None,
        }
        assert sent == sample["request"]
        return as_response(sample)

    def client(self) -> GatewayClient:
        return GatewayClient("http://gateway.test", httpx.MockTransport(self._answer))

    def all_used(self) -> bool:
        return not self._left


def as_response(sample: dict) -> httpx.Response:
    body = sample["body"]
    content = body if (sample["content_type"] or "").startswith("text") else None
    if content is None:
        content = json.dumps(body)
    headers = {"Content-Type": sample["content_type"]} if sample["content_type"] else {}
    return httpx.Response(sample["status"], headers=headers, content=content.encode())


def read_write(name: str) -> dict:
    return READ_WRITE[name]


# --- whoami -----------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["noc", "viewer", "editor", "admin"])
@pytest.mark.asyncio
async def test_whoami(role):
    body = await Replay(read_write(f"whoami_{role}")).client().whoami(caller())
    assert (body["tenant_id"], body["email"]) == ("keep", f"{role}@example.com")
    # The card's hint: the write itself is the authority.
    assert has_scope(body["scopes"], "write:alert") is (role != "viewer")


@pytest.mark.asyncio
async def test_whoami_says_nothing_about_read_only_mode():
    """It still names write scopes: only the write's own answer tells."""
    body = await Replay(READ_ONLY["whoami"]).client().whoami(caller())
    assert has_scope(body["scopes"], "write:alert") is True


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("whoami_no_credentials", GatewayUnauthorized),
        ("whoami_invalid_token", GatewayUnauthorized),
        ("whoami_forbidden", GatewayForbidden),
    ],
)
@pytest.mark.asyncio
async def test_whoami_refused(name, error):
    with pytest.raises(error):
        await Replay(read_write(name)).client().whoami(caller())


# --- queries ----------------------------------------------------------------------------


async def query(name: str) -> list[FetchedAlert]:
    body = read_write(name)["request"]["body"]
    client = Replay(read_write(name)).client()
    return await client.query_alerts(caller(), body["cel"], body["limit"])


@pytest.mark.parametrize(
    ("name", "fingerprints"),
    [
        ("query_all", ["fp-rich", "fp: שלום 2", "fp-bare", "fp-suppressed"]),
        ("query_by_fingerprints", ["fp-rich"]),
        ("query_after_write", ["fp-rich"]),
        ("query_no_match", []),
    ],
)
@pytest.mark.asyncio
async def test_query(name, fingerprints):
    assert [alert.fingerprint for alert in await query(name)] == fingerprints


@pytest.mark.parametrize(
    "name", ["query_syntax_error", "query_unknown_field", "query_not_a_filter"]
)
@pytest.mark.asyncio
async def test_a_filter_the_gateway_refuses(name):
    """The model is told why, to repair it."""
    with pytest.raises(InvalidCel) as raised:
        await query(name)
    diagnostics = read_write(name)["body"]["detail"]["diagnostics"]
    assert raised.value.diagnostics == diagnostics
    assert {"code", "message", "range"} <= set(diagnostics[0])


@pytest.mark.asyncio
async def test_a_request_the_gateway_refuses():
    body = read_write("query_bad_request")["request"]["body"]
    client = Replay(read_write("query_bad_request")).client()
    with pytest.raises(GatewayUnavailable) as raised:
        await client.query_alerts(caller(), body["cel"], body["limit"])
    assert raised.value.reason == "status_422"


@pytest.mark.parametrize(("name", "count"), [("count", 1), ("count_invalid_cel", None)])
@pytest.mark.asyncio
async def test_count(name, count):
    cel = read_write(name)["request"]["body"]["cel"]
    assert await Replay(read_write(name)).client().count_alerts(caller(), cel) == count


@pytest.mark.asyncio
async def test_fields():
    fields = await Replay(read_write("facet_fields")).client().alert_fields(caller())
    assert fields == read_write("facet_fields")["body"]
    assert {"name", "status", "severity", "last_received"} <= set(fields)


# --- single alerts ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fingerprint_that_no_filter_can_carry():
    replay = Replay(read_write("alert_encoded_fingerprint"))
    found = await replay.client().fetch_alerts_by_fingerprints(caller(), ["fp: שלום 2"])
    assert list(found.found) == ["fp: שלום 2"]
    assert replay.all_used()


@pytest.mark.parametrize(
    ("name", "fingerprint"), [("alert", "fp-rich"), ("alert_bare", "fp-bare")]
)
@pytest.mark.asyncio
async def test_what_the_query_leaves_out_is_read_alone(name, fingerprint):
    """A window that hides an alert from queries does not hide it from this."""
    empty = {
        **read_write("query_no_match"),
        "request": {
            "method": "POST",
            "path": "/alerts/query",
            "body": {"cel": f'fingerprint in ["{fingerprint}"]', "limit": 1},
        },
    }
    replay = Replay(empty, read_write(name))
    result = await replay.client().fetch_alerts_by_fingerprints(caller(), [fingerprint])
    alert = result.found[fingerprint]
    assert alert.data == read_write(name)["body"]
    assert has_incident(alert) is None
    assert replay.all_used()


@pytest.mark.asyncio
async def test_an_alert_that_is_gone():
    empty = {
        **read_write("query_no_match"),
        "request": {
            "method": "POST",
            "path": "/alerts/query",
            "body": {"cel": 'fingerprint in ["fp-missing"]', "limit": 1},
        },
    }
    replay = Replay(empty, read_write("alert_not_found"))
    result = await replay.client().fetch_alerts_by_fingerprints(
        caller(), ["fp-missing"]
    )
    assert (result.found, result.not_found) == ({}, ["fp-missing"])
    assert replay.all_used()


@pytest.mark.parametrize("name", ["alert_slash_not_routed", "alert_slash_other_route"])
@pytest.mark.asyncio
async def test_why_a_slash_is_never_sent(name):
    """The gateway decodes it before routing: the answer is not about that alert."""
    sample = read_write(name)
    fingerprint = sample["request"]["path"].removeprefix("/alerts/").replace("%2F", "/")
    replay = Replay()
    result = await replay.client().fetch_alerts_by_fingerprints(
        caller(), [f"{fingerprint} "]  # the space keeps it out of a filter
    )
    assert result.unresolvable == [f"{fingerprint} "]
    body = sample["body"]
    assert body.get("fingerprint") != fingerprint


@pytest.mark.asyncio
async def test_a_single_read_without_a_session():
    sample = {
        **read_write("alert_unauthenticated"),
        "request": {"method": "GET", "path": "/alerts/fp%20rich", "body": None},
    }
    with pytest.raises(GatewayUnauthorized):
        await Replay(sample).client().fetch_alerts_by_fingerprints(
            caller(), ["fp rich"]
        )


# --- what the model and the card see of an alert ----------------------------------------


@pytest.mark.asyncio
async def test_an_alert_with_everything():
    [rich, *_] = await query("query_all")
    assert project_alert(rich)["name"] == "Database ‹down›"
    assert project_alert(rich)["service"] == "orders"
    assert has_note(rich.data) and has_incident(rich) is True
    assert last_received(rich.data) == rich.data["lastReceived"]


@pytest.mark.asyncio
async def test_an_alert_with_next_to_nothing():
    _, _, bare, suppressed = await query("query_all")
    assert (bare.fingerprint, suppressed.data["status"]) == ("fp-bare", "suppressed")
    assert first_source(bare.data) is None
    assert not has_note(bare.data) and has_incident(bare) is False
    assert json.dumps(project_alert(bare))


# --- the write --------------------------------------------------------------------------

WRITES = {
    "batch_enrich_ok": "ok",
    "batch_enrich_failed": "failed_response",
    "batch_enrich_not_permitted": "not_permitted",
    "batch_enrich_unauthenticated": "unauthenticated",
    "batch_enrich_invalid_token": "unauthenticated",
    "batch_enrich_bad_request_id": "rejected",
    "batch_enrich_unknown_fingerprint": "ok",
}


@pytest.mark.parametrize(("name", "result"), WRITES.items())
@pytest.mark.asyncio
async def test_a_change_as_confirm_sends_it(name, result):
    sample = read_write(name)
    fingerprints = sample["request"]["body"]["fingerprints"]
    client = Replay(sample).client()
    written = await change_status(
        client, caller(), fingerprints, "acknowledged", "on it", False
    )
    assert written == result


@pytest.mark.asyncio
async def test_suppressing():
    client = Replay(read_write("batch_enrich_suppress_ok")).client()
    written = await change_status(client, caller(), ["fp-bare"], "suppressed")
    assert written == "ok"


@pytest.mark.parametrize(
    ("sample", "result"),
    [
        # Requests that Confirm never sends: what their answers would mean.
        (READ_WRITE["batch_enrich_malformed"], "rejected"),
        (READ_WRITE["batch_enrich_no_selector"], "rejected"),
        (READ_WRITE["batch_enrich_unknown_key"], "rejected_maybe_written"),
        (READ_WRITE["batch_enrich_unsafe_ticket_url"], "rejected_maybe_written"),
        (READ_WRITE["batch_enrich_delete_not_permitted"], "not_permitted"),
        (READ_ONLY["batch_enrich"], "read_only"),
    ],
    ids=lambda value: None if isinstance(value, dict) else value,
)
@pytest.mark.asyncio
async def test_other_answers_to_a_change(sample, result):
    body = {
        "fingerprints": ["fp"],
        "enrichments": {
            "status": "acknowledged",
            "dismiss_mode": None,
            "dismissed_until": None,
        },
    }
    sent = {
        "method": "POST",
        "path": "/alerts/batch_enrich?dispose_on_new_alert=false",
        "body": body,
    }
    client = Replay({**sample, "request": sent}).client()
    written = await change_status(client, caller(), ["fp"], "acknowledged")
    assert written == result


# --- the file ---------------------------------------------------------------------------


def test_every_sample_is_used_here():
    source = Path(__file__).read_text(encoding="utf-8")
    assert [name for name in READ_WRITE if not _is_named(name, source)] == []
    assert sorted(READ_ONLY) == ["batch_enrich", "whoami"]


def _is_named(name: str, source: str) -> bool:
    if name.startswith("whoami_") and f'"{name[7:]}"' in source:
        return True
    return f'"{name}"' in source
