"""Self-contained A/B reproduction of the latency-issue-#27 mechanism.

Stands up two minimal apps that mirror the ticket-count endpoint before and after the
fix, each next to a synchronous control endpoint that represents the gateway's other
sync routes and dependencies (auth verifier, DB helpers) which share the thread pool:

    BEFORE: def slow():   time.sleep(S)          (sync, blocks a thread-pool worker)
    AFTER:  async def slow(): await asyncio.sleep(S)  (async, holds no thread)

It fires a storm of concurrent /slow requests while sampling /control latency, so the
collateral impact of a slow provider on unrelated endpoints can be compared. It does
not measure the provider call's own latency (that is the external system's speed and is
unchanged by the fix).

Run: poetry run python benchmark/ab_mechanism.py
"""

import asyncio
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI

SLEEP_SECONDS = 1.0
STORM = 60
SAMPLES = 15
SAMPLE_INTERVAL = 0.15


def _make_before_app() -> FastAPI:
    """App whose slow route blocks a worker thread, like the pre-fix ticket-count route."""
    app = FastAPI()

    @app.get("/slow")
    def slow():
        """Simulate a slow provider call using a blocking sleep."""
        time.sleep(SLEEP_SECONDS)
        return {"count": 7}

    @app.get("/control")
    def control():
        """A cheap synchronous endpoint standing in for other gateway routes/deps."""
        return {"ok": True}

    return app


def _make_after_app() -> FastAPI:
    """App whose slow route awaits, like the post-fix async ticket-count route."""
    app = FastAPI()

    @app.get("/slow")
    async def slow():
        """Simulate a slow provider call using awaited async sleep."""
        await asyncio.sleep(SLEEP_SECONDS)
        return {"count": 7}

    @app.get("/control")
    def control():
        """A cheap synchronous endpoint standing in for other gateway routes/deps."""
        return {"ok": True}

    return app


def _start_server(app: FastAPI, port: int):
    """Start uvicorn for `app` in a background thread and return (server, thread)."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread


def _stop_server(server, thread):
    """Signal uvicorn to exit and join its thread."""
    server.should_exit = True
    thread.join(timeout=10)


async def _timed_get(client: httpx.AsyncClient, url: str) -> float:
    """Return the wall-clock duration of a GET, treating failures as their elapsed time."""
    start = time.perf_counter()
    try:
        await client.get(url)
    except httpx.HTTPError:
        pass
    return time.perf_counter() - start


def _percentile(values, pct: float) -> float:
    """Return the given percentile of a list of durations."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


async def _run_bench(base: str):
    """Fire the /slow storm while sampling /control latency; return (control, slow) durations."""
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        await client.get(f"{base}/control")
        storm = asyncio.gather(
            *[_timed_get(client, f"{base}/slow") for _ in range(STORM)]
        )
        control = []
        for _ in range(SAMPLES):
            control.append(await _timed_get(client, f"{base}/control"))
            await asyncio.sleep(SAMPLE_INTERVAL)
        slow = await storm
    return control, slow


def _report(label: str, control, slow):
    """Print control- and slow-endpoint latency percentiles for one variant."""
    print(f"\n{label}")
    print(
        "  control p50={:.3f}s  p95={:.3f}s  max={:.3f}s".format(
            _percentile(control, 0.50),
            _percentile(control, 0.95),
            max(control),
        )
    )
    print(
        "  slow    p50={:.3f}s  p95={:.3f}s  max={:.3f}s".format(
            _percentile(slow, 0.50),
            _percentile(slow, 0.95),
            max(slow),
        )
    )


def main():
    """Run the BEFORE and AFTER variants and print their latency comparison."""
    print(
        f"storm={STORM} concurrent /slow (sleep={SLEEP_SECONDS}s), "
        f"sampling /control {SAMPLES}x every {SAMPLE_INTERVAL}s"
    )
    for label, factory, port in [
        ("BEFORE (sync def, blocking)", _make_before_app, 8971),
        ("AFTER (async def, awaited)", _make_after_app, 8972),
    ]:
        server, thread = _start_server(factory(), port)
        try:
            control, slow = asyncio.run(_run_bench(f"http://127.0.0.1:{port}"))
        finally:
            _stop_server(server, thread)
        _report(label, control, slow)


if __name__ == "__main__":
    main()
