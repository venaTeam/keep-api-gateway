"""What the assistant holds per worker. Used only with KEEP_ASSISTANT_ENABLED."""
import logging

from fastapi import FastAPI

from src.services.assistant.api.route import describe_exception
from src.services.assistant.core import db as db_core
from src.services.assistant.core import metrics
from src.services.assistant.llm.client import build_llm_client

logger = logging.getLogger(__name__)


def start(app: FastAPI) -> None:
    """Never raises: the assistant must not keep the gateway from starting."""
    # None: new chats answer 503 llm_not_configured, existing drafts still work.
    app.state.assistant_llm = None
    try:
        app.state.assistant_llm = build_llm_client()
    except Exception as exc:
        metrics.internal_errors_total.labels(site="startup").inc()
        logger.error("assistant: no LLM client", extra=describe_exception(exc))
    metrics.llm_configured.set(0 if app.state.assistant_llm is None else 1)


async def stop(app: FastAPI) -> None:
    """Never raises, so that the gateway's own shutdown always runs to its end."""
    try:
        if app.state.assistant_llm is not None:
            await app.state.assistant_llm.aclose()
        db_core.dispose_engine()
    except Exception as exc:
        metrics.internal_errors_total.labels(site="shutdown").inc()
        logger.error("assistant: shutdown failed", extra=describe_exception(exc))
