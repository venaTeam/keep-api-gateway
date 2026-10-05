"""
Health / probe endpoints.

Two endpoints, one per probe:

* `/healthcheck` — **liveness**. Unchanged, unconditional 200. That is the right
  answer for liveness on an HTTP server: if it replies at all, the process can
  serve. Checking dependencies here would restart every replica at once on a
  Postgres blip. There is no separate `/livez` because this already is it.
* `/readyz` — DB reachable, schema satisfies this image's models.

The schema check asks whether the live database contains every table and column
this image declares. Extra tables and columns are ignored, which is what makes an
image rollback work: an older image declares fewer columns, finds them all, and
is happy. It reads no migration script — there are none in this image, since
`keep-migrations` applies the schema as an Argo PreSync Job before any pod of the
new ReplicaSet exists.

`/readyz` is wired to the **startupProbe**. With migrations ordered ahead of the
pods it is a safety net rather than the gate it used to be. Deliberately not the
readinessProbe — a schema comparison going false on every replica at once would
empty the Service mid-rollout.

Because it gates startup, a false negative kills the container, so the check
is bounded:

* `KEEP_READYZ_CHECK_TIMEOUT` — so a sick dependency makes the probe answer
  "not ready" rather than hang and tie up a worker slot. Keep it under the
  probe's own `timeoutSeconds` or kubelet cuts the connection first.

`db` and `db_on_start` are imported as modules rather than names, so
the check stays late-bound to whatever the module currently holds. The router is
mounted without a prefix, so both paths are absolute.
"""

import asyncio
import logging
import os

from fastapi import APIRouter, Response
from sqlalchemy import text

from src.repositories import db, db_on_start

logger = logging.getLogger(__name__)

READYZ_CHECK_TIMEOUT = float(os.environ.get("KEEP_READYZ_CHECK_TIMEOUT", "2"))

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
    """DB reachable, and its schema satisfies the models this image declares.

    One connection for both questions. `SELECT 1` stays rather than letting the
    schema query stand in for it: `schema_drift` is memoised on success, so on
    every probe after the first it answers from cache without touching the
    database — and would report a dead database as reachable. Reusing this
    connection for the schema scan is what removes the second checkout, which
    matters because `pool_timeout` (10s) exceeds this endpoint's own budget.
    """
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            satisfied, missing = db_on_start.schema_drift(conn)
    except Exception as exc:
        logger.error("Database check failed: %s", exc, exc_info=True)
        return False, {
            "reachable": False,
            "satisfied": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    detail = {"reachable": True, "satisfied": satisfied, "missing": missing}
    if not satisfied:
        logger.warning("Database schema does not satisfy this image: %s", missing)
    else:
        logger.info("Database readiness check passed: schema satisfies this image")
    return satisfied, detail


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
        logger.error("Readiness check '%s' timed out after %ss", name, READYZ_CHECK_TIMEOUT)
        return False, {"error": f"{name} check timed out after {READYZ_CHECK_TIMEOUT}s"}

    try:
        return task.result()
    except Exception as exc:
        logger.error(
            "Readiness check '%s' failed with unhandled exception: %s",
            name,
            exc,
            exc_info=True,
        )
        return False, {"error": f"{type(exc).__name__}: {exc}"}


@router.get(
    "/readyz",
    description=(
        "Readiness: DB reachable, its schema satisfies this image's models"
    ),
)
async def readyz(response: Response) -> dict:
    checks = {}

    # _check_db blocks on socket + DB work; inline it would park this worker's
    # event loop on every probe for as long as a sick Postgres takes to answer.
    db_ok, checks["database"] = await _bounded(
        asyncio.to_thread(_check_db), "database"
    )
    ready = db_ok

    if not ready:
        response.status_code = 503
        reasons = []
        if not db_ok:
            db_detail = checks["database"]
            if db_detail.get("error"):
                reasons.append(f"database ({db_detail['error']})")
            elif not db_detail.get("reachable", True):
                reasons.append("database unreachable")
            elif not db_detail.get("satisfied", True):
                missing = db_detail.get("missing") or {}
                reasons.append(
                    "database schema does not satisfy this image "
                    f"(missing tables={missing.get('missing_tables')}, "
                    f"columns={missing.get('missing_columns')})"
                )
            else:
                reasons.append("database unhealthy")

        reason_str = f": {'; '.join(reasons)}" if reasons else ""
        logger.error(f"Readiness check failed {reason_str}", extra={"checks": checks})
    else:
        logger.debug("Readiness check passed", extra={"checks": checks})

    return {"status": "ok" if ready else "unavailable", "checks": checks}


