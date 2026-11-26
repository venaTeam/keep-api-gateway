"""Chat Completions streaming, reduced to the few events the turn loop needs.

Nothing here logs or re-raises text from OpenAI or the conversation.
"""
import hashlib
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import openai

from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.core.log_sanitize import sanitize_for_log

logger = logging.getLogger(__name__)

# The SDK logs whole requests at DEBUG, and OPENAI_LOG, read while it is imported,
# turns that on whatever the logging configuration says.
for _library in ("openai", "httpx", "httpcore"):
    _library_logger = logging.getLogger(_library)
    _library_logger.setLevel(max(_library_logger.level, logging.WARNING))

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

LlmErrorKind = Literal[
    "unavailable", "rate_limited", "timeout", "rejected", "misconfigured"
]
_STREAM_CODES: dict[str, str] = {
    "unavailable": "llm_unavailable",
    "timeout": "llm_unavailable",
    "rate_limited": "llm_rate_limited",
    "rejected": "llm_rejected",
    "misconfigured": "llm_unavailable",
}


class LlmError(Exception):
    """`kind` is the metric label, `code` the code of the stream's error event."""

    def __init__(self, kind: LlmErrorKind):
        super().__init__(kind)
        self.kind = kind
        self.code = _STREAM_CODES[kind]


@dataclass(frozen=True)
class LlmTool:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallComplete:
    id: str
    name: str
    # Raw JSON text; possibly invalid, e.g. when the completion was cut short.
    arguments: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Finished:
    """The last event of every complete stream."""

    reason: str


LlmEvent = TextDelta | ToolCallComplete | Usage | Finished


class LlmClient(Protocol):
    model: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[LlmTool],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        safety_identifier: str | None = None,
    ) -> AsyncGenerator[LlmEvent, None]:
        """One completion. Raises LlmError. Close it (`contextlib.aclosing`) when
        leaving early, so that the connection is released."""
        ...

    async def aclose(self) -> None:
        ...


def make_safety_identifier(tenant_id: str, email: str) -> str:
    """Lets OpenAI attribute abuse to one user without learning who it is."""
    return hashlib.sha256(f"{tenant_id}\0{email}".encode()).hexdigest()[:32]


def is_allowed_base_url(url: str) -> bool:
    """https, except on this machine: the API key and alert text go there."""
    try:
        parts = urlsplit(url)
        parts.port  # raises on a malformed port
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS


def build_llm_client(
    transport: httpx.AsyncBaseTransport | None = None,
) -> "OpenAiLlmClient | None":
    """The configured client, or None (logged) when it cannot be built."""
    settings = get_settings()
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY is not set: chat answers llm_not_configured")
        return None
    if settings.openai_base_url and not is_allowed_base_url(settings.openai_base_url):
        logger.error(
            "OPENAI_BASE_URL must be https (or http on localhost): "
            "chat answers llm_not_configured"
        )
        return None
    return OpenAiLlmClient(
        api_key=settings.openai_api_key,
        model=settings.openai_model_name,
        base_url=settings.openai_base_url,
        transport=transport,
    )


def _error_kind(exc: Exception) -> LlmErrorKind:
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 429:
            return "rate_limited"
        if exc.status_code in (400, 413, 422):
            return "rejected"
        if exc.status_code in (401, 403, 404):
            # A revoked key or an unknown model: no user can do anything about it.
            return "misconfigured"
        if exc.status_code == 408:
            return "timeout"
        return "unavailable"
    if isinstance(exc, openai.APIError):
        # An error object inside the stream, after a 200.
        if exc.type == "invalid_request_error":
            return "rejected"
        if exc.code == "rate_limit_exceeded" or exc.type == "insufficient_quota":
            return "rate_limited"
    return "unavailable"


class _IncompleteStream(Exception):
    pass


class OpenAiLlmClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        # Proxies behind a custom URL reject request fields they do not know.
        self._is_openai = not base_url
        timeout = httpx.Timeout(get_settings().openai_timeout_seconds, connect=5.0)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            max_retries=1,
            # The SDK's own client follows redirects, and the pinned SDK cannot even
            # construct it with the pinned httpx.
            http_client=httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=timeout,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            ),
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[LlmTool],
        *,
        tool_choice: Literal["auto", "none"] = "auto",
        safety_identifier: str | None = None,
    ) -> AsyncGenerator[LlmEvent, None]:
        started = time.monotonic()
        try:
            async with await self._client.chat.completions.create(
                **self._request(messages, tools, tool_choice, safety_identifier)
            ) as chunks:
                async with aclosing(_normalize(chunks, started)) as events:
                    async for event in events:
                        yield event
        except Exception as exc:
            # Transport errors inside the stream are not wrapped by the SDK, and
            # a proxy can send anything.
            kind = _error_kind(exc)
            metrics.llm_errors_total.labels(kind=kind).inc()
            logger.warning(
                "llm: request failed",
                extra={
                    "kind": kind,
                    "error_type": type(exc).__name__,
                    "status": getattr(exc, "status_code", None),
                    "openai_code": sanitize_for_log(getattr(exc, "code", None) or ""),
                    "openai_request_id": sanitize_for_log(
                        getattr(exc, "request_id", None) or ""
                    ),
                },
            )
            raise LlmError(kind) from None

    def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[LlmTool],
        tool_choice: str,
        safety_identifier: str | None,
    ) -> dict[str, Any]:
        # Fields the pinned SDK has no parameter for.
        settings = get_settings()
        extra: dict[str, Any] = {"max_completion_tokens": settings.max_completion_tokens}
        if settings.openai_reasoning_effort:
            extra["reasoning_effort"] = settings.openai_reasoning_effort
        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "extra_body": extra,
        }
        if tools:  # the API refuses the other two without tools
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "strict": True,
                    },
                }
                for tool in tools
            ]
            request["tool_choice"] = tool_choice
            request["parallel_tool_calls"] = False
        if self._is_openai:
            extra["store"] = False
            if safety_identifier:
                extra["safety_identifier"] = safety_identifier
        return request


async def _normalize(chunks: Any, started: float) -> AsyncGenerator[LlmEvent, None]:
    """Chunks to events: tool calls whole, then usage, then Finished."""
    calls: dict[int, dict[str, str]] = {}
    usage: Usage | None = None
    finish_reason: str | None = None
    first = True
    async for chunk in chunks:
        if chunk.usage is not None:
            usage = Usage(
                prompt_tokens=chunk.usage.prompt_tokens or 0,
                completion_tokens=chunk.usage.completion_tokens or 0,
            )
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        # A strict-schema refusal is the model's answer, in another field.
        # The pinned SDK does not declare it, but keeps what it does not know.
        text = delta.content or getattr(delta, "refusal", None)
        if first and (text or delta.tool_calls):
            first = False
            metrics.llm_first_token_seconds.observe(time.monotonic() - started)
        if text:
            yield TextDelta(text)
        for part in delta.tool_calls or []:
            call = calls.setdefault(
                part.index or 0, {"id": "", "name": "", "arguments": ""}
            )
            call["id"] = call["id"] or part.id or ""
            if part.function is not None:
                call["name"] = call["name"] or part.function.name or ""
                call["arguments"] += part.function.arguments or ""
        if choice.finish_reason:
            finish_reason = choice.finish_reason
    # A stream that just ends would otherwise pass for a complete answer.
    if finish_reason is None:
        raise _IncompleteStream
    for _, call in sorted(calls.items()):
        if not call["id"] or not call["name"]:
            raise _IncompleteStream
        yield ToolCallComplete(**call)
    if usage is not None:
        yield usage
    yield Finished(finish_reason)
