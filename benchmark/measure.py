"""A/B latency driver for latency issue #27.

Fires many concurrent GET /dashboard/ticket-count requests at a running gateway while
sampling a control endpoint's latency, so the collateral impact of a slow ticketing
provider can be compared before (sync) and after (async) the fix.

For the clearest signal, point --control at a synchronous gateway route (one that uses
the thread pool), since that is what the sync ticket-count endpoint starves. Run the
same invocation once on dev and once on fix/dashboard-ticket-count-async.

Usage:
    poetry run python -m benchmark.measure --gateway http://localhost:8080 \
        --api-key <key> --concurrency 60 --control /healthcheck
"""

import argparse
import asyncio
import time

import httpx


async def _storm(client, gateway, api_key, concurrency):
    """Fire `concurrency` ticket-count requests concurrently and return their durations."""

    async def one():
        start = time.perf_counter()
        try:
            await client.get(
                f"{gateway}/dashboard/ticket-count",
                headers={"x-api-key": api_key},
            )
        except httpx.HTTPError:
            pass
        return time.perf_counter() - start

    return await asyncio.gather(*[one() for _ in range(concurrency)])


async def _sample_control(client, gateway, api_key, control, samples, interval):
    """Sample the control endpoint latency `samples` times, returning durations."""
    durations = []
    for _ in range(samples):
        start = time.perf_counter()
        try:
            await client.get(f"{gateway}{control}", headers={"x-api-key": api_key})
        except httpx.HTTPError:
            durations.append(float("inf"))
        else:
            durations.append(time.perf_counter() - start)
        await asyncio.sleep(interval)
    return durations


def _percentile(values, pct):
    """Return the given percentile of a list of durations, ignoring timeouts."""
    finite = sorted(v for v in values if v != float("inf"))
    if not finite:
        return float("inf")
    index = min(len(finite) - 1, int(len(finite) * pct))
    return finite[index]


async def main():
    """Run the storm and control sampling concurrently, then print latency stats."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--concurrency", type=int, default=60)
    parser.add_argument("--control", default="/healthcheck")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        storm = asyncio.create_task(
            _storm(client, args.gateway, args.api_key, args.concurrency)
        )
        control = await _sample_control(
            client,
            args.gateway,
            args.api_key,
            args.control,
            args.samples,
            args.interval,
        )
        slow = await storm

    timeouts = sum(1 for v in control if v == float("inf"))
    print(f"concurrency={args.concurrency}, control={args.control}, samples={args.samples}")
    print(f"control (unrelated endpoint) latency during storm (seconds):")
    print(f"  p50 = {_percentile(control, 0.50):.3f}")
    print(f"  p95 = {_percentile(control, 0.95):.3f}")
    print(f"  max = {max(control):.3f}")
    print(f"  timeouts = {timeouts}")
    print(f"slow (/dashboard/ticket-count) latency during storm (seconds):")
    print(f"  p50 = {_percentile(slow, 0.50):.3f}")
    print(f"  p95 = {_percentile(slow, 0.95):.3f}")
    print(f"  max = {max(slow):.3f}")


if __name__ == "__main__":
    asyncio.run(main())
