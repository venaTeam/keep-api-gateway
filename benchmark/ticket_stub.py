"""Slow ticketing-provider stub for benchmarking GET /dashboard/ticket-count.

Simulates a slow external ticketing system so the collateral impact of the gateway's
ticket-count endpoint can be measured before/after latency issue #27.

Usage:
    SLEEP=10 poetry run uvicorn benchmark.ticket_stub:app --port 9099

Then configure a ticket_count provider whose count_url points at
http://localhost:9099/count and run benchmark.measure against the gateway.
"""

import asyncio
import os

from fastapi import FastAPI

app = FastAPI()

SLEEP_SECONDS = float(os.environ.get("SLEEP", "10"))


@app.get("/count")
async def count():
    """Return a fixed count after an artificial delay."""
    await asyncio.sleep(SLEEP_SECONDS)
    return {"count": 7}
