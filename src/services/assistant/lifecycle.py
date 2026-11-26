"""What the assistant holds per worker. Used only with KEEP_ASSISTANT_ENABLED.

None of it raises: the assistant must neither keep the gateway from starting nor cut its
shutdown short.
"""
import asyncio
import logging
from typing import get_args

from fastapi import FastAPI

from src.services.assistant.api.route import describe_exception
from src.services.assistant.bl import retention, sweeps
from src.services.assistant.config import get_settings
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from src.services.assistant.core.loops import Loops
from src.services.assistant.core.work import (
    SHUTDOWN_GRACE_SECONDS,
    OngoingWork,
    install_signal_handlers,
    log_failure,
)
from src.services.assistant.gateway.client import GatewayClient
from src.services.assistant.gateway.transport import IN_PROCESS_URL, InProcessTransport
from src.services.assistant.llm.client import LlmErrorKind, build_llm_client
from src.services.assistant.models.api.draft import PAYLOAD_MODELS, OutcomeCode

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 3600.0


def start(app: FastAPI) -> None:
    app.state.assistant_work = OngoingWork()
    app.state.assistant_gateway = GatewayClient(IN_PROCESS_URL, InProcessTransport(app))
    # None: new chats answer 503 llm_not_configured, existing drafts still work.
    app.state.assistant_llm = None
    try:
        app.state.assistant_llm = build_llm_client()
    except Exception as exc:
        metrics.internal_errors_total.labels(site="startup").inc()
        logger.error("assistant: no LLM client", extra=describe_exception(exc))
    metrics.llm_configured.set(0 if app.state.assistant_llm is None else 1)
    _start_series()
    app.state.assistant_undo_signals = lambda: None
    try:
        app.state.assistant_undo_signals = install_signal_handlers(
            app.state.assistant_work, asyncio.get_running_loop()
        )
    except Exception as exc:
        log_failure("startup", "assistant: no signal handlers", exc)
    try:
        _start_loops(app.state.assistant_work)
    except Exception as exc:
        log_failure("startup", "assistant: no loops", exc)


def _start_series() -> None:
    """Prometheus sees no increase in a series that begins with its first event, and
    some of these happen once in months."""
    for kind in get_args(LlmErrorKind):
        metrics.llm_errors_total.labels(kind=kind)
    for kind in PAYLOAD_MODELS:
        for outcome in get_args(OutcomeCode):
            metrics.draft_outcomes_total.labels(kind=kind, outcome=outcome)
        metrics.drafts_total.labels(kind=kind, event="finalized_after_timeout")


def _start_loops(work: OngoingWork) -> None:
    """They end with the worker's other work."""
    settings = get_settings()
    loops = Loops(work)
    loops.start("draft_sweeps", sweeps.run, seconds=SWEEP_INTERVAL_SECONDS)
    if settings.retention_enabled:
        loops.start(
            "retention", retention.run, seconds=settings.retention_interval_seconds
        )
    else:
        logger.warning("assistant: retention is switched off")


async def settle(app: FastAPI) -> None:
    """Runs next to the gateway's own shutdown, while the LLM client and the database
    can still be used: turns and Confirms end, and writes that nobody waits for any
    more get the time to finish."""
    try:
        app.state.assistant_work.drain()
        still_writing, still_working = await asyncio.gather(
            app.state.assistant_gateway.wait_for_writes(SHUTDOWN_GRACE_SECONDS),
            app.state.assistant_work.settle(SHUTDOWN_GRACE_SECONDS),
        )
        if still_writing or still_working:
            metrics.internal_errors_total.labels(site="shutdown").inc()
            logger.warning(
                "assistant: shutting down with work left",
                extra={"writes": still_writing, "tasks": still_working},
            )
    except Exception as exc:
        log_failure("shutdown", "assistant: settling failed", exc)


async def stop(app: FastAPI) -> None:
    try:
        app.state.assistant_undo_signals()
        if app.state.assistant_llm is not None:
            await app.state.assistant_llm.aclose()
        await app.state.assistant_gateway.aclose()
        db_core.dispose_engine()
    except Exception as exc:
        log_failure("shutdown", "assistant: shutdown failed", exc)
