"""Assistant settings, read from the environment on first use.

Only `ASSISTANT_*` names, so that the gateway's own `DATABASE_POOL_*` cannot resize
the assistant's pool, and the `OPENAI_*` names the gateway's other AI features use.
The database itself is the gateway's (`consts.DB_CONNECTION_STRING`).
"""
import functools
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ENABLED_ENV_VAR = "KEEP_ASSISTANT_ENABLED"

# The longest a single write may take, and the default of the batch_enrich writes.
MAX_WRITE_TIMEOUT_SECONDS = 12.0


def _is_true(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """The server-side switch. Off by default: OPENAI_API_KEY may already be set for
    the other AI features, so a key alone must not turn the assistant on."""
    return _is_true(os.environ.get(ENABLED_ENV_VAR) or "")


def _env(
    name: str,
    default: Any,
    cast: Callable[[str], Any] = str,
    *,
    zero: bool = False,
    maximum: float | None = None,
    repr: bool = True,
) -> Any:
    """`zero`: 0 means something sensible here. Below that, and where it does not (0
    days of retention would delete everything), a number is as unusable as a typo.
    `maximum`: a larger number is logged and the maximum is used."""

    def read() -> Any:
        value = os.environ.get(name)
        if not value:
            return default
        try:
            result = cast(value)
            # Not `<= 0`, which is false for nan.
            if cast in (int, float) and not (result > 0 or (zero and result == 0)):
                raise ValueError("out of range")
        except ValueError:
            # A typo here must not take the whole gateway down.
            logger.error(
                "assistant: unusable setting, using the default",
                extra={"setting": name},
            )
            return default
        if maximum is not None and result > maximum:
            logger.error(
                "assistant: setting above its maximum, using the maximum",
                extra={"setting": name, "maximum": maximum},
            )
            return maximum
        return result

    return field(default_factory=read, repr=repr, metadata={"env": name})


@dataclass(frozen=True)
class Settings:
    # Database, per worker. Small and impatient: assistant queries may neither
    # starve the gateway's pool nor eat up the 26 s budget of a Confirm.
    db_pool_size: int = _env("ASSISTANT_DB_POOL_SIZE", 3, int)
    db_max_overflow: int = _env("ASSISTANT_DB_MAX_OVERFLOW", 2, int, zero=True)
    db_pool_timeout: float = _env("ASSISTANT_DB_POOL_TIMEOUT", 3.0, float)
    db_connect_timeout: int = _env("ASSISTANT_DB_CONNECT_TIMEOUT", 3, int)
    db_statement_timeout_ms: int = _env("ASSISTANT_DB_STATEMENT_TIMEOUT_MS", 5000, int)
    db_schema_check_timeout_ms: int = _env(
        "ASSISTANT_DB_SCHEMA_CHECK_TIMEOUT_MS", 2000, int
    )

    # Calls to the gateway's own routes
    gateway_whoami_timeout_seconds: float = _env(
        "ASSISTANT_GATEWAY_WHOAMI_TIMEOUT_SECONDS", 3.0, float
    )
    gateway_read_timeout_seconds: float = _env(
        "ASSISTANT_GATEWAY_READ_TIMEOUT_SECONDS", 5.0, float
    )
    # The batch_enrich writes only; the other writes have their own, fixed. Bounded:
    # Confirm has 26 s for everything, and a write it cannot wait for it never sends.
    gateway_write_timeout_seconds: float = _env(
        "ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS",
        MAX_WRITE_TIMEOUT_SECONDS,
        float,
        maximum=MAX_WRITE_TIMEOUT_SECONDS,
    )

    # LLM. Without a key the chat answers 503 `llm_not_configured`; confirming and
    # discarding existing drafts keeps working.
    openai_api_key: str = _env("OPENAI_API_KEY", "", repr=False)
    openai_model_name: str = _env("OPENAI_MODEL_NAME", "gpt-4o-2024-08-06")
    openai_base_url: str = _env("OPENAI_BASE_URL", "", repr=False)
    openai_timeout_seconds: float = _env(
        "ASSISTANT_OPENAI_TIMEOUT_SECONDS", 60.0, float
    )
    # Newer models refuse function tools on chat completions unless it is `none`.
    # Empty: not sent.
    openai_reasoning_effort: str = _env("ASSISTANT_OPENAI_REASONING_EFFORT", "")

    # Chat turn
    turn_deadline_seconds: float = _env("ASSISTANT_TURN_DEADLINE_SECONDS", 90.0, float)
    max_tool_rounds: int = _env("ASSISTANT_MAX_TOOL_ROUNDS", 4, int, zero=True)
    max_drafts_per_turn: int = _env(
        "ASSISTANT_MAX_DRAFTS_PER_TURN", 3, int, zero=True
    )
    max_completion_tokens: int = _env("ASSISTANT_MAX_COMPLETION_TOKENS", 1024, int)
    history_max_messages: int = _env("ASSISTANT_HISTORY_MAX_MESSAGES", 30, int)
    history_max_chars: int = _env("ASSISTANT_HISTORY_MAX_CHARS", 48_000, int)
    max_user_message_chars: int = _env("ASSISTANT_MAX_USER_MESSAGE_CHARS", 4000, int)
    # Must stay well under Next's 30 s idle proxy timeout.
    heartbeat_seconds: float = _env("ASSISTANT_HEARTBEAT_SECONDS", 10.0, float)
    fields_cache_ttl_seconds: float = _env(
        "ASSISTANT_FIELDS_CACHE_TTL_SECONDS", 600.0, float, zero=True
    )

    # Drafts and limits
    draft_ttl_seconds: int = _env("ASSISTANT_DRAFT_TTL_SECONDS", 1800, int)
    # Draft kinds held back, by name: kinds/registry.py reads it, name by name.
    disabled_kinds: str = _env("ASSISTANT_DISABLED_KINDS", "")
    rate_limit_per_minute: int = _env(
        "ASSISTANT_RATE_LIMIT_PER_MINUTE", 10, int, zero=True
    )
    rate_limit_per_day: int = _env("ASSISTANT_RATE_LIMIT_PER_DAY", 200, int, zero=True)

    # Retention
    retention_enabled: bool = _env("ASSISTANT_RETENTION_ENABLED", True, _is_true)
    retention_days: int = _env("ASSISTANT_RETENTION_DAYS", 90, int)
    retention_interval_seconds: int = _env(
        "ASSISTANT_RETENTION_INTERVAL_SECONDS", 86_400, int
    )
    retention_batch_size: int = _env("ASSISTANT_RETENTION_BATCH_SIZE", 200, int)
    # Must differ from keep-migrations' advisory lock (8274419300112233).
    retention_lock_key: int = _env(
        "ASSISTANT_RETENTION_LOCK_KEY", 7316902254418086, int
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Read the environment again on the next use (tests)."""
    get_settings.cache_clear()
