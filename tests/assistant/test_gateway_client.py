import asyncio
import logging

import httpx
import pytest

from src.services.assistant.core import metrics
from src.services.assistant.gateway.client import (
    CALL_CLASSES,
    Caller,
    GatewayClient,
    can_use_cel,
    can_use_path,
)
from src.services.assistant.gateway.errors import (
    GatewayForbidden,
    GatewayIncompatible,
    GatewayUnauthorized,
    GatewayUnavailable,
    InvalidCel,
    QueryFailed,
)
from tests.assistant.fakes import (
    REQUEST_ID,
    TOKEN,
    FakeGateway,
    alert,
    caller,
    change_status,
    query_result,
)
from tests.assistant.logs import rendered

ALERT_LIMIT = CALL_CLASSES["alert"].limit
QUERY = ("POST", "/alerts/query")
COUNT = ("POST", "/alerts/query/count")
FIELDS = ("GET", "/alerts/facets/fields")
WHOAMI = ("GET", "/whoami")
WRITE = ("POST", "/alerts/batch_enrich")


@pytest.fixture()
def gateway():
    return FakeGateway()


async def write(client, note="investigating", status="acknowledged", dispose=False):
    return await change_status(client, caller(), ["a", "b"], status, note, dispose)


# --- what is sent ------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_allowlisted_headers_are_sent(gateway):
    gateway.on_json(*WHOAMI, {"tenant_id": "keep"})
    gateway.on_json(*QUERY, query_result())
    gateway.on_json(*COUNT, 3)
    gateway.on_json(*FIELDS, ["name"])
    gateway.on_json("GET", "/alerts/a%20b", alert("a b"))
    gateway.on_json(*WRITE, {"status": "ok"})
    client = gateway.client()
    await client.whoami(caller())
    await client.query_alerts(caller(), "true", 21)
    await client.count_alerts(caller(), "true")
    await client.alert_fields(caller())
    await client.fetch_alerts_by_fingerprints(caller(), ["a b"])
    await write(client)
    assert len(gateway.requests) == 6

    transport = {"host", "accept", "accept-encoding", "connection", "user-agent"}
    for request in gateway.requests:
        names = set(request.headers) - transport
        if request.method == "POST":
            names -= {"content-type", "content-length"}
        expected = {"authorization", "x-request-id"}
        if request.url.path == "/alerts/batch_enrich":
            expected.add("x-keep-source")
            assert request.headers["X-Keep-Source"] == "assistant"
        assert names == expected
        assert request.headers["Authorization"] == TOKEN
        assert request.headers["X-Request-ID"] == REQUEST_ID
        assert request.headers["User-Agent"] == "keep-gateway-assistant"


@pytest.mark.asyncio
async def test_token_is_forwarded_byte_for_byte(gateway):
    gateway.on_json(*WHOAMI, {})
    value = 'Bearer {"tenant_id":"é"}&x'
    await gateway.client().whoami(Caller(authorization=value, request_id=REQUEST_ID))
    raw = dict(gateway.requests[0].headers.raw)
    assert raw[b"Authorization"] == value.encode("latin-1")


@pytest.mark.asyncio
async def test_gateway_cookies_never_reach_the_next_user(gateway):
    gateway.on(
        *WHOAMI,
        httpx.Response(200, json={}, headers={"Set-Cookie": "session=user1; Path=/"}),
    )
    client = gateway.client()
    await client.whoami(caller())
    await client.whoami(caller())
    assert "cookie" not in gateway.requests[1].headers


@pytest.mark.asyncio
async def test_environment_proxies_are_ignored(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:1")
    assert GatewayClient("http://gateway.test")._http._mounts == {}


def test_caller_repr_hides_the_token():
    assert TOKEN not in repr(caller())


# --- write body --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["firing", "acknowledged", "resolved"])
async def test_write_body_is_the_modal_shape(gateway, status):
    gateway.on_json(*WRITE, {"status": "ok"})
    assert await write(gateway.client(), status=status) == "ok"
    assert gateway.paths() == ["POST /alerts/batch_enrich?dispose_on_new_alert=false"]
    assert gateway.body() == {
        "fingerprints": ["a", "b"],
        "enrichments": {
            "status": status,
            "dismiss_mode": None,
            "dismissed_until": None,
            "note": "investigating",
        },
    }


@pytest.mark.asyncio
async def test_suppressed_omits_the_dismiss_keys_and_an_empty_note(gateway):
    gateway.on_json(*WRITE, {"status": "ok"})
    await write(gateway.client(), note=None, status="suppressed", dispose=True)
    assert gateway.paths() == ["POST /alerts/batch_enrich?dispose_on_new_alert=true"]
    assert gateway.body()["enrichments"] == {"status": "suppressed"}


# --- write classification ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"status": "ok"}), "ok"),
        (httpx.Response(200, json={"status": "ok", "message": "none"}), "ok"),
        (httpx.Response(200, json={"status": "failed"}), "failed_response"),
        (httpx.Response(200, json={}), "http_error"),
        (httpx.Response(200, json=["ok"]), "http_error"),
        (httpx.Response(200, text="<html>"), "http_error"),
        (httpx.Response(400), "rejected"),  # the empty-body request id rejection
        (httpx.Response(400, json={"detail": {"code": "INVALID_CEL"}}), "rejected"),
        (httpx.Response(401, json={"detail": "x"}), "unauthenticated"),
        (
            httpx.Response(
                403, json={"detail": "Read only instance, but non-read scopes requested"}
            ),
            "read_only",
        ),
        (httpx.Response(403, json={"detail": "missing scopes"}), "not_permitted"),
        (httpx.Response(403, json={"detail": ["Read only"]}), "not_permitted"),
        (httpx.Response(403, json={"detail": {"m": "Read only"}}), "not_permitted"),
        (httpx.Response(403), "not_permitted"),
        (httpx.Response(403, text="Read only"), "not_permitted"),
        (httpx.Response(422, json={"detail": [{"loc": ["body"]}]}), "rejected"),
        (httpx.Response(422, json={"detail": "bad key"}), "rejected_maybe_written"),
        (httpx.Response(422, json={"detail": {"a": 1}}), "rejected_maybe_written"),
        (httpx.Response(422, json={}), "rejected_maybe_written"),
        (httpx.Response(422, text="nope"), "rejected_maybe_written"),
        (httpx.Response(500, text="Internal Server Error"), "http_error"),
        (httpx.Response(302, headers={"Location": "/x"}), "http_error"),
        (httpx.Response(404, json={"detail": "Not Found"}), "http_error"),
        (httpx.Response(429), "http_error"),
        (httpx.Response(502), "unknown"),
        (httpx.Response(503), "unknown"),
        (httpx.Response(504, text="upstream timeout"), "unknown"),
    ],
)
async def test_write_answers(gateway, response, expected):
    gateway.on(*WRITE, response)
    assert await write(gateway.client()) == expected
    assert len(gateway.requests) == 1  # never retried, never redirected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("x"),
        httpx.WriteTimeout("x"),
        httpx.WriteError("x"),
        httpx.ReadError("x"),
        httpx.RemoteProtocolError("x"),
    ],
)
async def test_write_dropped_after_send_is_unknown(gateway, error):
    gateway.on(*WRITE, error)
    assert await write(gateway.client()) == "unknown"
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x")],
)
async def test_write_never_sent_is_told_apart(gateway, error):
    gateway.on(*WRITE, error)
    with pytest.raises(GatewayUnavailable) as raised:
        await write(gateway.client())
    assert raised.value.sent is False


@pytest.mark.asyncio
async def test_write_has_an_overall_deadline(gateway):
    async def hang(request):
        await asyncio.Event().wait()

    gateway.on(*WRITE, hang)
    result = await change_status(
        gateway.client(), caller(), ["a"], "resolved", None, False, timeout=0.05
    )
    assert result == "unknown"


@pytest.mark.asyncio
async def test_real_pool_timeout_is_never_sent():
    """Through the real transport: a full pool must not read as a written call."""
    release = asyncio.Event()

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await release.wait()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = GatewayClient(f"http://127.0.0.1:{port}")
    await client._http.aclose()
    client._http = httpx.AsyncClient(limits=httpx.Limits(max_connections=1))
    try:
        first = asyncio.create_task(client.whoami(caller()))
        await asyncio.sleep(0.2)
        with pytest.raises(GatewayUnavailable) as raised:
            await change_status(
                client, caller(), ["a"], "resolved", None, False, timeout=10
            )
        assert raised.value.sent is False
        release.set()
        with pytest.raises(GatewayUnavailable):
            await first
    finally:
        release.set()
        await client.aclose()
        server.close()


# --- reads: errors -----------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_returns_the_body(gateway):
    gateway.on_json(*WHOAMI, {"tenant_id": "keep", "email": "a@b", "scopes": None})
    assert (await gateway.client().whoami(caller()))["email"] == "a@b"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>"), httpx.Response(200, json=[])])  # fmt: skip
async def test_whoami_with_another_shape_is_incompatible(gateway, response):
    gateway.on(*WHOAMI, response)
    with pytest.raises(GatewayIncompatible):
        await gateway.client().whoami(caller())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401), GatewayUnauthorized),
        (httpx.Response(403, json={"detail": "x"}), GatewayForbidden),
        (httpx.Response(400), GatewayUnavailable),
        (httpx.Response(404), GatewayUnavailable),
        (httpx.Response(500, text="Internal Server Error"), GatewayUnavailable),
        (httpx.Response(503), GatewayUnavailable),
        (httpx.ConnectError("x"), GatewayUnavailable),
        (httpx.ReadTimeout("x"), GatewayUnavailable),
    ],
)
async def test_read_failures(gateway, response, error):
    gateway.on(*WHOAMI, response)
    with pytest.raises(error):
        await gateway.client().whoami(caller())


@pytest.mark.asyncio
async def test_redirects_are_not_followed(gateway):
    gateway.on(
        *WHOAMI, httpx.Response(302, headers={"Location": "http://elsewhere.test/"})
    )
    with pytest.raises(GatewayUnavailable):
        await gateway.client().whoami(caller())
    assert gateway.paths() == ["GET /whoami"]


@pytest.mark.asyncio
async def test_text_that_utf8_cannot_carry_is_never_sent(gateway):
    with pytest.raises(GatewayUnavailable) as raised:
        await gateway.client().query_alerts(caller(), "name == '\ud800'", 21)
    assert raised.value.sent is False
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_read_only_403_on_a_read(gateway):
    gateway.on_json(*WHOAMI, {"detail": "Read only instance"}, status=403)
    with pytest.raises(GatewayForbidden) as raised:
        await gateway.client().whoami(caller())
    assert raised.value.read_only is True


@pytest.mark.asyncio
async def test_whoami_has_its_own_timeout(gateway, set_env):
    set_env("ASSISTANT_GATEWAY_WHOAMI_TIMEOUT_SECONDS", 0.05)

    async def hang(request):
        await asyncio.Event().wait()

    gateway.on(*WHOAMI, hang)
    with pytest.raises(GatewayUnavailable):
        await gateway.client().whoami(caller())


@pytest.mark.asyncio
async def test_unset_gateway_url_is_unavailable():
    with pytest.raises(GatewayUnavailable) as raised:
        await GatewayClient("").whoami(caller())
    assert raised.value.sent is False


# --- reads: CEL queries --------------------------------------------------------


@pytest.mark.asyncio
async def test_query_body_and_result(gateway):
    gateway.on_json(*QUERY, query_result(alert("a"), alert("b")))
    alerts = await gateway.client().query_alerts(caller(), "status == 'firing'", 21)
    assert gateway.body() == {"cel": "status == 'firing'", "limit": 21}
    assert [a.fingerprint for a in alerts] == ["a", "b"]
    assert all(a.incident_known for a in alerts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"results": None},
        {"results": [1]},
        {"results": [{"name": "x"}]},
        {"results": [{"fingerprint": None}]},
    ],
)
async def test_query_with_another_shape_fails(gateway, body):
    gateway.on_json(*QUERY, body)
    with pytest.raises(GatewayUnavailable):
        await gateway.client().query_alerts(caller(), "true", 21)


@pytest.mark.asyncio
async def test_invalid_cel_carries_the_diagnostics(gateway):
    diagnostic = {"code": "UNKNOWN_FIELD", "message": "Unknown field", "range": None}
    gateway.on_json(
        *QUERY,
        {
            "detail": {
                "code": "INVALID_CEL",
                "message": "The CEL filter is invalid.",
                "diagnostics": [diagnostic, "junk"],
            }
        },
        status=400,
    )
    with pytest.raises(InvalidCel) as raised:
        await gateway.client().query_alerts(caller(), "nope", 21)
    assert raised.value.diagnostics == [diagnostic]


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["INVALID_CEL", ["INVALID_CEL"], {"code": "X"}, None])
async def test_other_400s_are_not_invalid_cel(gateway, detail):
    gateway.on_json(*QUERY, {"detail": detail}, status=400)
    with pytest.raises(GatewayUnavailable):
        await gateway.client().query_alerts(caller(), "true", 21)


@pytest.mark.asyncio
async def test_500_on_a_query_is_query_failed(gateway):
    gateway.on(*QUERY, httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(QueryFailed):
        await gateway.client().query_alerts(caller(), "name == 'x :word'", 21)


@pytest.mark.asyncio
async def test_count_is_a_bare_int(gateway):
    gateway.on_json(*COUNT, 57)
    assert await gateway.client().count_alerts(caller(), "true") == 57
    assert gateway.body() == {"cel": "true"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"count": 5}),
        httpx.Response(200, json=True),
        httpx.Response(200, json="5"),
        httpx.Response(200, json=None),
        httpx.Response(200, text="many"),
        httpx.Response(500),
        httpx.ReadTimeout("x"),
    ],
)
async def test_count_of_any_other_kind_is_unknown(gateway, response):
    gateway.on(*COUNT, response)
    assert await gateway.client().count_alerts(caller(), "true") is None


@pytest.mark.asyncio
async def test_alert_fields(gateway):
    gateway.on_json(*FIELDS, ["name", 1, None, "labels.team"])
    assert await gateway.client().alert_fields(caller()) == ["name", "labels.team"]


@pytest.mark.asyncio
async def test_alert_fields_with_another_shape_fail(gateway):
    gateway.on_json(*FIELDS, {"name": 1})
    with pytest.raises(GatewayUnavailable):
        await gateway.client().alert_fields(caller())


# --- fingerprints: which way is safe -----------------------------------------


@pytest.mark.parametrize(
    "fingerprint",
    ["abc", "A_b.c-d", "host:cpu", "a:b:c", "a/b", "20241-01-01", "x" * 512, "0" * 64],
)
def test_fingerprints_that_go_into_a_query(fingerprint):
    assert can_use_cel(fingerprint)


@pytest.mark.parametrize(
    "fingerprint",
    [
        "",
        "x" * 513,
        "a b",
        " :word",
        ":word",
        "a::b",
        "a-:b",
        'a"b',
        "a'b",
        "a\\b",
        "a\n",
        "abc\n",
        "é",
        "a%b",
        "2024-01-01",
        "2024-01-01T10:00:00",
        "2024-01-01-suffix",
    ],
)
def test_fingerprints_that_stay_out_of_a_query(fingerprint):
    assert not can_use_cel(fingerprint)


@pytest.mark.parametrize(
    "fingerprint",
    [".", "..", "a/b", "/", "a\\b", "", "../x", "a\x00", "a\nb", "a\x9f", "\ud800"],
)
def test_fingerprints_that_stay_out_of_a_path(fingerprint):
    assert not can_use_path(fingerprint)


@pytest.mark.parametrize("fingerprint", ["...", "a b", "é", "a?b", "a#b", "a%2Fb", ".a"])
def test_fingerprints_that_go_into_a_path(fingerprint):
    assert can_use_path(fingerprint)


# --- fingerprints: lookups ---------------------------------------------------


@pytest.mark.asyncio
async def test_safe_fingerprints_use_one_query(gateway):
    gateway.on_json(*QUERY, query_result(alert("b"), alert("host:cpu")))
    result = await gateway.client().fetch_alerts_by_fingerprints(
        caller(), ["host:cpu", "b", "b"]
    )
    assert gateway.paths() == ["POST /alerts/query"]
    assert gateway.body() == {"cel": 'fingerprint in ["host:cpu", "b"]', "limit": 2}
    assert list(result.found) == ["host:cpu", "b"]  # requested order
    assert (result.not_found, result.unresolvable) == ([], [])


@pytest.mark.asyncio
async def test_unsafe_and_unreturned_fingerprints_are_read_singly(gateway):
    gateway.on_json(*QUERY, query_result(alert("a"), alert("not-asked-for")))
    gateway.on_json("GET", "/alerts/%C3%A9%20%25%3F%23", alert("é %?#"))
    gateway.on_json("GET", "/alerts/2024-01-01", alert("2024-01-01"))
    gateway.on_json("GET", "/alerts/hidden", alert("hidden"))
    result = await gateway.client().fetch_alerts_by_fingerprints(
        caller(), ["a", "é %?#", "2024-01-01", "hidden", "gone"]
    )
    assert gateway.body(0)["cel"] == 'fingerprint in ["a", "hidden", "gone"]'
    assert sorted(gateway.paths()[1:]) == [
        "GET /alerts/%C3%A9%20%25%3F%23",
        "GET /alerts/2024-01-01",
        "GET /alerts/gone",
        "GET /alerts/hidden",
    ]
    assert list(result.found) == ["a", "é %?#", "2024-01-01", "hidden"]
    assert result.found["a"].incident_known is True
    assert result.found["hidden"].incident_known is False
    assert result.not_found == ["gone"]
    assert result.unresolvable == []


@pytest.mark.asyncio
async def test_fingerprints_that_could_reach_another_route_are_never_requested(gateway):
    gateway.on_json(*QUERY, query_result(alert("team/found")))
    result = await gateway.client().fetch_alerts_by_fingerprints(
        caller(),
        [".", "..", "x/history", "team/found", "a\\b", "é/../b", "b/é"],
    )
    assert gateway.paths() == ["POST /alerts/query"]
    assert list(result.found) == ["team/found"]
    assert result.unresolvable == [".", "..", "x/history", "a\\b", "é/../b", "b/é"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        [{"fingerprint": "q q"}],
        {"fingerprint": "other"},
        {"fingerprint": None},
        {"name": "x"},
        "q q",
        None,
    ],
)
async def test_a_foreign_answer_is_unresolvable(gateway, answer):
    gateway.on_json("GET", "/alerts/q%20q", answer)
    result = await gateway.client().fetch_alerts_by_fingerprints(caller(), ["q q"])
    assert result.unresolvable == ["q q"]
    assert not result.found


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(400, json={"detail": {"code": "INVALID_CEL"}}),
    ],
)
async def test_a_failing_query_falls_back_to_single_reads(gateway, answer):
    gateway.on(*QUERY, answer)
    gateway.on_json("GET", "/alerts/a", alert("a"))
    result = await gateway.client().fetch_alerts_by_fingerprints(
        caller(), ["a", "b/c", "gone"]
    )
    assert list(result.found) == ["a"]
    assert result.not_found == ["gone"]
    assert result.unresolvable == ["b/c"]


@pytest.mark.asyncio
async def test_known_missing_fingerprints_are_not_read_again(gateway):
    gateway.on_json(*QUERY, query_result(alert("back")))
    result = await gateway.client().fetch_alerts_by_fingerprints(
        caller(), ["gone", "back", "q q"], known_missing=["gone", "back", "q q"]
    )
    assert gateway.paths() == ["POST /alerts/query"]
    assert list(result.found) == ["back"]
    assert result.not_found == ["gone", "q q"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(401), GatewayUnauthorized),
        (httpx.Response(502), GatewayUnavailable),
        (httpx.ReadTimeout("x"), GatewayUnavailable),
    ],
)
async def test_one_failing_single_read_fails_the_lookup(gateway, answer, error):
    gateway.on("GET", "/alerts/q%20q", answer)
    gateway.on_json("GET", "/alerts/r%20r", alert("r r"))
    with pytest.raises(error):
        await gateway.client().fetch_alerts_by_fingerprints(caller(), ["r r", "q q"])


@pytest.mark.asyncio
async def test_lookup_has_one_overall_deadline_and_leaves_only_running_calls(gateway):
    answer = asyncio.Event()

    async def slow(request):
        await answer.wait()
        return httpx.Response(404)

    for number in range(15):
        gateway.on("GET", f"/alerts/q%20{number}", slow)
    before = asyncio.all_tasks()
    with pytest.raises(GatewayUnavailable):
        await gateway.client().fetch_alerts_by_fingerprints(
            caller(), [f"q {n}" for n in range(15)], deadline_seconds=0.1
        )
    left = asyncio.all_tasks() - before
    assert len(left) == len(gateway.requests) == ALERT_LIMIT
    answer.set()
    await asyncio.wait(left, timeout=2)
    assert asyncio.all_tasks() == before
    assert len(gateway.requests) == ALERT_LIMIT


@pytest.mark.asyncio
async def test_single_reads_are_limited_across_chats(gateway):
    running = peak = 0
    release = asyncio.Event()
    full = asyncio.Event()

    async def handler(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == ALERT_LIMIT:
            full.set()
        await release.wait()
        running -= 1
        return httpx.Response(404)

    client = FakeGateway(handler).client()
    lookups = [
        asyncio.create_task(
            client.fetch_alerts_by_fingerprints(
                caller(), [f"{chat} {n}" for n in range(8)]
            )
        )
        for chat in range(3)
    ]
    await asyncio.wait_for(full.wait(), 2)
    for _ in range(5):
        await asyncio.sleep(0)
    assert peak == ALERT_LIMIT
    release.set()
    results = await asyncio.gather(*lookups)
    assert all(len(r.not_found) == 8 for r in results)
    assert peak == ALERT_LIMIT


@pytest.mark.asyncio
async def test_an_empty_lookup_calls_nothing(gateway):
    result = await gateway.client().fetch_alerts_by_fingerprints(caller(), [])
    assert (result.found, gateway.requests) == ({}, [])


# --- observability -----------------------------------------------------------


@pytest.mark.asyncio
async def test_failures_are_counted_and_logged_without_content(gateway, caplog):
    secret = "secret-alert-text"
    gateway.on_json(*QUERY, {"detail": secret}, status=502)
    labels = dict(endpoint="query", status_class="5xx")
    before = metrics.gateway_requests_total.labels(**labels)._value.get()
    with caplog.at_level(logging.DEBUG), pytest.raises(GatewayUnavailable):
        await gateway.client().query_alerts(caller(), f"name == '{secret}'", 21)
    assert metrics.gateway_requests_total.labels(**labels)._value.get() == before + 1
    lines = [
        rendered(r)
        for r in caplog.records
        if r.name == "src.services.assistant.gateway.client"
    ]
    assert any(REQUEST_ID in line and '"endpoint": "query"' in line for line in lines)
    assert not any(secret in line or TOKEN in line for line in lines)


@pytest.mark.asyncio
async def test_the_client_follows_no_redirects_and_can_be_closed(gateway):
    client = gateway.client()
    assert not client._http.is_closed
    assert client._http.follow_redirects is False
    await client.aclose()
    assert client._http.is_closed
