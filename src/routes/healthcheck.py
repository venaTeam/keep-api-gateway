"""
Health / probe endpoints.

Two endpoints, one per probe:

* `/healthcheck` — **liveness**. Unchanged, unconditional 200. That is the right
  answer for liveness on an HTTP server: if it replies at all, the process can
  serve. Checking dependencies here would restart every replica at once on a
  Postgres blip. There is no separate `/livez` because this already is it.
* `/readyz` — **readiness**. DB reachable, schema at head, producer connected.

Pair with a `startupProbe` sized to the worst-case migration: migrations run
before the socket binds, so liveness would otherwise kill the pod mid-way.
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Response
from sqlalchemy import text

# Imported as modules, not names: the checks below read `db.engine`,
# `db_on_start.schema_at_head` and `factory.get_producer_instance` at call time,
# so each one stays late-bound to whatever the module currently holds.
from src.repositories import db, db_on_start
from src.services.producers import factory

logger = logging.getLogger(__name__)

# Bounded so a sick dependency makes the probe answer "not ready" rather than
# hang and tie up a worker slot.
READYZ_CHECK_TIMEOUT = float(os.environ.get("KEEP_READYZ_CHECK_TIMEOUT", "5"))

# Mounted without a prefix, so both paths are absolute.
router = APIRouter()


@router.get("/healthcheck", description="Liveness: the process can serve requests")
def healthcheck() -> dict:
    """
    Does nothing but return 200 response code

    Returns:
        dict: empty JSON object
    """
    return {}


def _check_db() -> tuple[bool, dict]:
    """DB reachable, and the stamped revision matches this image's alembic head."""
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return False, {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        at_head, db_revision, script_head = db_on_start.schema_at_head()
    except Exception as exc:
        return False, {
            "reachable": True,
            "at_head": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    detail = {
        "reachable": True,
        "at_head": at_head,
        "db_revision": db_revision,
        "script_head": script_head,
    }
    return at_head, detail


async def _check_producer() -> tuple[bool, dict]:
    """Kafka producer connected. Also nudges a cold producer to reconnect, so a
    pod that can't reach the brokers stays NotReady instead of DLQ-ing alerts."""
    producer = factory.get_producer_instance()
    if producer is None:
        # startup() creates it, so this means startup hasn't got that far yet.
        return False, {"created": False}

    try:
        healthy, detail = await producer.health(attempt_reconnect=True)
    except Exception as exc:
        return False, {"created": True, "error": f"{type(exc).__name__}: {exc}"}

    detail["created"] = True
    return healthy, detail


def _discard(task: "asyncio.Task"):
    """Consume an abandoned check's result so it can't surface as an
    unretrieved-exception warning."""
    if not task.cancelled():
        task.exception()


async def _bounded(awaitable, name: str) -> tuple[bool, dict]:
    """Run a readiness check under a timeout, reporting an overrun as a failure.

    `asyncio.wait` and not `wait_for`: `wait_for` awaits the cancellation it
    requests, and a check blocked in a worker thread cannot be cancelled, so the
    timeout would bound nothing. Here the overrunning check is abandoned.
    """
    task = asyncio.ensure_future(awaitable)
    done, _pending = await asyncio.wait({task}, timeout=READYZ_CHECK_TIMEOUT)

    if not done:
        task.cancel()
        task.add_done_callback(_discard)
        return False, {"error": f"{name} check timed out after {READYZ_CHECK_TIMEOUT}s"}

    try:
        return task.result()
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"}


@router.get(
    "/readyz",
    description="Readiness: DB reachable and at head, Kafka producer connected",
)
async def readyz(response: Response) -> dict:
    checks = {}

    # _check_db blocks on socket + DB work; inline it would park this worker's
    # event loop on every probe for as long as a sick Postgres takes to answer.
    db_ok, checks["database"] = await _bounded(
        asyncio.to_thread(_check_db), "database"
    )
    producer_ok, checks["producer"] = await _bounded(_check_producer(), "producer")

    ready = db_ok and producer_ok

    if not ready:
        response.status_code = 503
        logger.warning("Readiness check failed", extra={"checks": checks})

    return {"status": "ok" if ready else "unavailable", "checks": checks}


