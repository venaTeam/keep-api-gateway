# Real-time SSE

| File | Read it when |
|---|---|
| [`handoff.md`](handoff.md) | you are deploying the work or configuring it in Helm — PR status, the full configuration reference, what the Redis must provide, what is still owed |
| [`deployment-validation-agent.md`](deployment-validation-agent.md) | you (or an agent) are validating a deployment — guardrails, ordered steps, expected values, reporting contract |
| [`acceptance-kit/`](acceptance-kit/) | you want to run the checks: `run_acceptance.py` against any environment, `browser_check.mjs` for what a user sees, `redis/valkey-dev.yaml` as a dev Redis reference |

**One-line version.** A browser's SSE stream lives in the memory of the single gateway pod
that served it, while `POST /sse/notify` load-balances to any pod — so with N gateway
processes a viewer receives roughly 1/N of alerts, and the dropped notifications still
return HTTP 204. The fix gives the processes a shared Redis pub/sub channel. It is opt-in:
**without `SSE_FANOUT=redis` the new code is inert.**

Measured on a 5-pod deployment, before → after: per-viewer delivery 0.20 → 1.00, alerts
visible in a real browser 4/10 → 10/10, through a rolling restart 4/20 → 20/20, and a
deleted pod stops stranding viewers for 34 s (now 1 s).
