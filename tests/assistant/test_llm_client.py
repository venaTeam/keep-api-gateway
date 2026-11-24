"""The real OpenAI SDK over a mock transport: what goes out, and how raw SSE bytes
come back as events."""
import asyncio
import json
import logging
from contextlib import aclosing

import httpx
import pytest

from src.services.assistant.core import metrics
from src.services.assistant.llm.client import (
    Finished,
    LlmError,
    LlmTool,
    OpenAiLlmClient,
    TextDelta,
    ToolCallComplete,
    Usage,
    build_llm_client,
    is_allowed_base_url,
    make_safety_identifier,
)
from tests.assistant.conftest import TEST_OPENAI_KEY
from tests.assistant.logs import rendered

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["a"],
    "properties": {"a": {"type": "string"}},
}
TOOL = LlmTool("change_alert_status", "Draft a change", SCHEMA)
MESSAGES = [{"role": "user", "content": "secret-user-text"}]


def chunk(delta=None, finish=None, usage=None, **fields) -> str:
    choices = []
    if delta is not None or finish is not None:
        choices = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    body = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": choices,
        "usage": usage,
        **fields,
    }
    return f"data: {json.dumps(body)}\n\n"


def call_delta(index=0, id=None, name=None, arguments=None) -> dict:
    function = {"name": name, "arguments": arguments}
    return {"tool_calls": [{"index": index, "id": id, "function": function}]}


USAGE = chunk(usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
DONE = "data: [DONE]\n\n"
ANSWER = chunk({"content": "Hi"}) + chunk(finish="stop") + USAGE + DONE


class Bytes(httpx.AsyncByteStream):
    """Raw network reads; an exception in the list is raised at that point."""

    def __init__(self, *parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            yield part if isinstance(part, bytes) else part.encode()

    async def aclose(self):
        self.closed = True


class FakeOpenAi:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            answer = Bytes(answer)
        if isinstance(answer, Bytes):
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=answer
            )
        return answer

    def client(self, base_url="") -> OpenAiLlmClient:
        return OpenAiLlmClient(
            api_key="test-key-not-real",
            model="test-model",
            base_url=base_url,
            transport=httpx.MockTransport(self.handle),
        )

    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


def error(status: int, code="x", **headers) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"message": "secret-openai-text", "type": "t", "code": code}},
        # Keeps the SDK's one retry immediate.
        headers={"retry-after-ms": "1", "x-request-id": "req_1", **headers},
    )


async def collect(client, tools=(TOOL,), **options) -> list:
    return [event async for event in client.stream(MESSAGES, list(tools), **options)]


# --- the request ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_body():
    fake = FakeOpenAi(ANSWER)
    await collect(fake.client(), safety_identifier="s" * 32)
    assert str(fake.requests[0].url) == "https://api.openai.com/v1/chat/completions"
    assert fake.requests[0].headers["Authorization"] == "Bearer test-key-not-real"
    assert fake.body() == {
        "model": "test-model",
        "messages": MESSAGES,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 1024,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "change_alert_status",
                    "description": "Draft a change",
                    "parameters": SCHEMA,
                    "strict": True,
                },
            }
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "store": False,
        "safety_identifier": "s" * 32,
    }


@pytest.mark.asyncio
async def test_custom_base_url_gets_no_openai_only_fields():
    fake = FakeOpenAi(ANSWER)
    await collect(fake.client("https://proxy.test/v1"), safety_identifier="s" * 32)
    assert str(fake.requests[0].url) == "https://proxy.test/v1/chat/completions"
    assert "store" not in fake.body()
    assert "safety_identifier" not in fake.body()
    assert fake.body()["tools"][0]["function"]["strict"] is True


@pytest.mark.asyncio
async def test_no_safety_identifier_when_none_is_given():
    fake = FakeOpenAi(ANSWER)
    await collect(fake.client())
    assert "safety_identifier" not in fake.body()
    assert fake.body()["store"] is False


@pytest.mark.asyncio
async def test_tool_choice_none_keeps_the_tools():
    fake = FakeOpenAi(ANSWER)
    await collect(fake.client(), tool_choice="none")
    assert fake.body()["tool_choice"] == "none"
    assert len(fake.body()["tools"]) == 1


@pytest.mark.asyncio
async def test_without_tools_no_tool_options_are_sent():
    fake = FakeOpenAi(ANSWER)
    await collect(fake.client(), tools=())
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(fake.body())


def test_safety_identifier_is_a_short_stable_hash():
    value = make_safety_identifier("keep", "user@example.com")
    assert value == make_safety_identifier("keep", "user@example.com")
    assert len(value) == 32 and int(value, 16)
    assert "user" not in value
    assert value != make_safety_identifier("other", "user@example.com")
    assert make_safety_identifier("a", "bc") != make_safety_identifier("ab", "c")


def test_timeouts_and_retries():
    client = FakeOpenAi().client()._client
    assert client.timeout == httpx.Timeout(60, connect=5)
    assert client.max_retries == 1
    assert client._client.follow_redirects is False


# --- the stream ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_usage_and_finish():
    fake = FakeOpenAi(
        chunk({"role": "assistant", "content": ""})
        + chunk({"content": "Hel"})
        + chunk({"content": "lo"})
        + chunk(finish="stop")
        + USAGE
        + DONE
    )
    assert await collect(fake.client()) == [
        TextDelta("Hel"),
        TextDelta("lo"),
        Usage(11, 7),
        Finished("stop"),
    ]


@pytest.mark.asyncio
async def test_usage_is_optional():
    fake = FakeOpenAi(chunk({"content": "Hi"}, finish="stop") + DONE)
    assert await collect(fake.client()) == [TextDelta("Hi"), Finished("stop")]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 7, 64])
async def test_tool_call_arguments_split_anywhere(size):
    """Split over chunks, and the bytes over network reads (UTF-8 included)."""
    arguments = json.dumps({"a": "é — x"}, ensure_ascii=False)
    raw = (
        chunk(call_delta(id="call_1", name="change_alert_status", arguments=""))
        + "".join(chunk(call_delta(arguments=char)) for char in arguments)
        + chunk(finish="tool_calls")
        + USAGE
        + DONE
    ).encode()
    parts = [raw[i : i + size] for i in range(0, len(raw), size)]
    assert await collect(FakeOpenAi(Bytes(*parts)).client()) == [
        ToolCallComplete("call_1", "change_alert_status", arguments),
        Usage(11, 7),
        Finished("tool_calls"),
    ]


@pytest.mark.asyncio
async def test_interleaved_tool_calls_are_kept_apart():
    fake = FakeOpenAi(
        chunk({"content": "Working"})
        + chunk(call_delta(1, "b", "two", '{"b'))
        + chunk(call_delta(0, "a", "one", '{"a'))
        + chunk(call_delta(1, arguments='":1}'))
        + chunk(call_delta(0, arguments='":0}'))
        + chunk(finish="tool_calls")
        + DONE
    )
    assert await collect(fake.client()) == [
        TextDelta("Working"),
        ToolCallComplete("a", "one", '{"a":0}'),
        ToolCallComplete("b", "two", '{"b":1}'),
        Finished("tool_calls"),
    ]


@pytest.mark.asyncio
async def test_a_completion_cut_at_the_token_limit_says_so():
    fake = FakeOpenAi(
        chunk(call_delta(0, "a", "one", '{"a')) + chunk(finish="length") + DONE
    )
    assert await collect(fake.client()) == [
        ToolCallComplete("a", "one", '{"a'),
        Finished("length"),
    ]


@pytest.mark.asyncio
async def test_a_refusal_is_text():
    fake = FakeOpenAi(chunk({"refusal": "I cannot"}) + chunk(finish="stop") + DONE)
    assert await collect(fake.client()) == [TextDelta("I cannot"), Finished("stop")]


@pytest.mark.asyncio
async def test_leaving_early_closes_the_response():
    body = Bytes(chunk({"content": "a"}), chunk({"content": "b"}), DONE)
    async with aclosing(FakeOpenAi(body).client().stream(MESSAGES, [])) as events:
        async for event in events:
            assert event == TextDelta("a")
            break
    assert body.closed


@pytest.mark.asyncio
async def test_cancelling_closes_the_response():
    class Hanging(Bytes):
        async def __aiter__(self):
            yield chunk({"content": "a"}).encode()
            await asyncio.Event().wait()

    body = Hanging()
    started = asyncio.Event()

    async def consume():
        async for _ in FakeOpenAi(body).client().stream(MESSAGES, []):
            started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed


# --- failures ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "kind", "code", "calls"),
    [
        (400, "rejected", "llm_rejected", 1),
        (413, "rejected", "llm_rejected", 1),
        (422, "rejected", "llm_rejected", 1),
        (429, "rate_limited", "llm_rate_limited", 2),
        (401, "misconfigured", "llm_unavailable", 1),
        (403, "misconfigured", "llm_unavailable", 1),
        (404, "misconfigured", "llm_unavailable", 1),
        (408, "timeout", "llm_unavailable", 2),
        (500, "unavailable", "llm_unavailable", 2),
        (503, "unavailable", "llm_unavailable", 2),
    ],
)
async def test_status_errors(status, kind, code, calls):
    fake = FakeOpenAi(error(status), error(status))
    counter = metrics.llm_errors_total.labels(kind=kind)
    before = counter._value.get()
    with pytest.raises(LlmError) as raised:
        await collect(fake.client())
    assert (raised.value.kind, raised.value.code) == (kind, code)
    assert len(fake.requests) == calls  # one retry, and only where it can help
    assert counter._value.get() == before + 1


@pytest.mark.asyncio
async def test_a_retry_can_succeed():
    fake = FakeOpenAi(error(429), ANSWER)
    assert (await collect(fake.client()))[-1] == Finished("stop")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx.ConnectError("x"), "unavailable"),
        (httpx.ReadTimeout("x"), "timeout"),
        (httpx.ConnectTimeout("x"), "timeout"),
    ],
)
async def test_transport_errors(failure, kind):
    with pytest.raises(LlmError) as raised:
        await collect(FakeOpenAi(failure, failure).client())
    assert raised.value.kind == kind


@pytest.mark.asyncio
async def test_redirects_are_not_followed():
    fake = FakeOpenAi(error(302, Location="https://elsewhere.test/"))
    with pytest.raises(LlmError) as raised:
        await collect(fake.client())
    assert raised.value.kind == "unavailable"
    assert len(fake.requests) == 1


def stream_error(type: str, code: str | None = None) -> str:
    body = {"error": {"message": "secret-openai-text", "type": type, "code": code}}
    return f"data: {json.dumps(body)}\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rest", "kind"),
    [
        ([httpx.ReadTimeout("x")], "timeout"),
        ([httpx.RemoteProtocolError("x")], "unavailable"),
        (["data: {not json\n\n"], "unavailable"),
        (["data: [1]\n\n"], "unavailable"),
        ([stream_error("server_error")], "unavailable"),
        ([stream_error("invalid_request_error")], "rejected"),
        ([stream_error("requests", "rate_limit_exceeded")], "rate_limited"),
        ([DONE], "unavailable"),  # ended without a finish reason
        ([], "unavailable"),
        ([chunk(call_delta(0, None, "one", "{}")), chunk(finish="stop")], "unavailable"),
    ],
)
async def test_failures_inside_the_stream(rest, kind):
    fake = FakeOpenAi(Bytes(chunk({"content": "partial"}), *rest))
    events = []
    with pytest.raises(LlmError) as raised:
        async for event in fake.client().stream(MESSAGES, [TOOL]):
            events.append(event)
    assert raised.value.kind == kind
    assert events == [TextDelta("partial")]
    assert len(fake.requests) == 1  # never retried once the stream has started


@pytest.mark.asyncio
async def test_no_content_in_logs_or_errors(caplog):
    fake = FakeOpenAi(error(400, code="context_length_exceeded"))
    with caplog.at_level(logging.DEBUG), pytest.raises(LlmError) as raised:
        await collect(fake.client())
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    lines = [rendered(record) for record in caplog.records]
    assert any('"openai_code": "context_length_exceeded"' in line for line in lines)
    for line in lines:
        assert "secret" not in line and "test-key-not-real" not in line


@pytest.mark.asyncio
async def test_first_token_time_is_observed_once():
    def observed():
        for metric in metrics.llm_first_token_seconds.collect():
            for sample in metric.samples:
                if sample.name.endswith("_count"):
                    return sample.value

    before = observed()
    await collect(FakeOpenAi(ANSWER).client())
    assert observed() == before + 1


# --- building the client -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://proxy.example/v1", True),
        ("http://localhost:1234/v1", True),
        ("http://127.0.0.1/v1", True),
        ("http://[::1]:1234/v1", True),
        ("http://proxy.example/v1", False),
        ("http://localhost.evil.example/v1", False),
        ("http://localhost@evil.example/v1", False),
        ("ftp://localhost/v1", False),
        ("https://", False),
        ("https://host:port/v1", False),
        ("https://[bad/v1", False),
        ("proxy.example", False),
    ],
)
def test_base_url_rule(url, allowed):
    assert is_allowed_base_url(url) is allowed


@pytest.mark.asyncio
async def test_the_client_is_built_from_the_settings(set_env):
    set_env("OPENAI_MODEL_NAME", "configured-model")
    set_env("OPENAI_BASE_URL", "https://proxy.test/v1")
    fake = FakeOpenAi(ANSWER)
    client = build_llm_client(httpx.MockTransport(fake.handle))
    await collect(client)
    assert client.model == "configured-model"
    assert str(fake.requests[0].url) == "https://proxy.test/v1/chat/completions"
    assert fake.requests[0].headers["Authorization"] == f"Bearer {TEST_OPENAI_KEY}"
    await client.aclose()


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("OPENAI_API_KEY", "", "OPENAI_API_KEY is not set"),
        ("OPENAI_BASE_URL", "http://user:pw@proxy.test", "OPENAI_BASE_URL must be https"),
    ],
)
def test_no_client_without_usable_settings(set_env, caplog, setting, value, message):
    set_env(setting, value)
    assert build_llm_client() is None
    assert any(r.levelname == "ERROR" and message in r.message for r in caplog.records)
    assert "pw@" not in caplog.text
