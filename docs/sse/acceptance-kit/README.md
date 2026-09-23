# SSE acceptance kit

Checks that the real-time SSE work behaves correctly in a **deployed** environment,
before it reaches real users. Point it at dev or integration; the disruptive checks
restart pods, so never run those against production.

Built from the before/after validation on 2026-09-17 — see [`../handoff.md`](../handoff.md)
and, if you are an agent running this, [`../deployment-validation-agent.md`](../deployment-validation-agent.md).
Origin:
(kept outside this repo, on the validation machine) — every threshold below is the
value that separated the broken build from the fixed one on a 5-pod deployment.

## What is being validated

| Layer | Change | Why it needs a deployed environment |
|---|---|---|
| Tier 1 (gateway) | Redis/Valkey pub/sub fan-out behind `SSE_FANOUT=redis` | the defect only exists with >1 gateway process |
| Tier 0.5 (gateway) | streams close on SIGTERM; `connected` announces the keepalive; new counters | needs a real pod lifecycle |
| Tier 0 (keep-ui) | watchdog, capped backoff, catch-up refetch, tenant-scoped leader lock | needs a real browser |
| D2 (EH + workflows) | stop computing and sending `poll-presets` | needs the real pipeline |

## The defect, in one paragraph

A browser's SSE stream lives in the memory of the one gateway pod that served it.
`POST /sse/notify` from the event handler load-balances to *any* pod; a pod with no
matching subscriber returns **204** and drops the notification silently. With N gateway
processes a viewer therefore sees roughly 1/N of events. Measured on 5 pods before the
fix: mean per-viewer delivery **0.20**, and in a browser **4 of 10** alerts ever appeared.
After: **1.00** and **10/10**. Note the divisor is `replicas × KEEP_WORKERS` — each
gunicorn worker is a separate process with its own broker.

## Is the kit itself trustworthy?

It was exercised on a 5-pod deployment on 2026-09-17, in both directions:

- fan-out **on**: 8/8 checks pass (`fanout` 10/10 on every pod, `e2e` 5/5 on every pod,
  `token` 401/401/204, `poddelete` stream ended in 0.98 s, `presets` 0).
- fan-out **off** (`SSE_FANOUT` unset on the gateway): the kit *fails* exactly where it should —
  `preflight/fanout-enabled`, `preflight/fanout-connected`, and `fanout/cross-pod` at 0/10
  on every pod. A green run therefore means something.

## Prerequisites

- `oc` logged in, with `exec`, `logs`, `port-forward` and (for disruptive checks) `delete pod`
  / `rollout restart` in the target namespace.
- Python 3.9+ (standard library only).
- For `browser_check.mjs`: Node plus `playwright` (run it from a keep-ui checkout, or
  `npm i playwright && npx playwright install chromium`).

## Run

```bash
cp config.env.example config.env     # fill in
set -a; . ./config.env; set +a

./run_acceptance.py --only config    # print what Helm actually rendered, and check it
./run_acceptance.py                  # non-disruptive: safe on a shared dev env
./run_acceptance.py --disruptive     # + pod delete, rolling restart, Redis restart
./run_acceptance.py --json out.json  # also machine-readable
UI_ROUTE=https://keep-ui-...  node browser_check.mjs plain
UI_ROUTE=https://keep-ui-...  node browser_check.mjs rollout
```

Exit code 0 means every selected check passed. Each check prints what it measured next
to what was expected, so a failure is readable without re-running.

## The checks

| Name | What it proves | Pass threshold | Before the fix |
|---|---|---|---|
| `config` | prints the effective SSE/Redis env of all three deployments, the computed channel, and how many broker processes to expect | non-empty prefix | n/a |
| `preflight` | >1 gateway pod; `SSE_FANOUT=redis`; `keep_sse_fanout_connected=1` on every pod; non-empty `REDIS_KEY_PREFIX` | all true | `SSE_FANOUT=none` |
| `fanout` | notify **one** pod → every pod's subscriber receives it | 10/10 on every pod | 10/10 on one pod, 0/10 on the rest |
| `e2e` | real alerts through the route reach a viewer on every pod | 5/5 on every pod | per-pod 0–2 of 5 |
| `connected` | `connected` carries `keepalive_seconds` | present | absent |
| `token` | `/sse/notify` rejects a missing/wrong token | 401 / 401 / 204 | 204 / 204 / 204 (open) |
| `presets` | `poll-presets` is gone | 0 log lines | 276 in 12 min |
| `errors` | no fan-out error counters climbing | all zero | n/a |
| `poddelete` *(disruptive)* | a rollout does not strand viewers on a dying pod | stream ends <5 s | ~30 s, keepalives still flowing |
| `rolling` *(disruptive)* | alerts ingested during a full rolling restart still arrive | ≥95 % of viewer-alert pairs | ~15 % |
| `redis` *(disruptive)* | fan-out survives a Redis restart | stream survives, delivery resumes | n/a |

`browser_check.mjs` is the user-visible one: every ingested alert must become a row.
Measured on 5 pods — plain: **4/10 → 10/10** (p50 5.7 s → 1.05 s); across a rolling
restart: **4/20 → 20/20** (p50 70 s → 2.9 s).

## Configuration reference (for Helm)

Only **keep-api-gateway** talks to Redis. The event handler and workflows need one
variable; keep-ui needs none.

### keep-api-gateway

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SSE_FANOUT` | **yes** | `none` | Must be `redis`. Any other value keeps the per-process broker — the deployment behaves exactly as before, silently. |
| `REDIS_HOST` | yes (unless Sentinel) | `localhost` | |
| `REDIS_PORT` | no | `6379` | |
| `REDIS_DB` | no | `0` | Logical DB for data operations. **Does not isolate pub/sub** — channels are instance-wide whatever the DB index. |
| `REDIS_KEY_PREFIX` | **yes on a shared Redis** | `""` | The channel is `<REDIS_KEY_PREFIX><SSE_FANOUT_CHANNEL>`. This is the *only* isolation between deployments. The gateway logs a warning at startup when it is empty. |
| `SSE_FANOUT_CHANNEL` | no | `keep:sse` | Leave it; disambiguate with the prefix. |
| `REDIS_SSL` | if TLS | `false` | |
| `REDIS_USERNAME` / `REDIS_PASSWORD` | if auth | unset | The ACL user needs `PUBLISH` **and** `SUBSCRIBE` on the channel. |
| `REDIS_SENTINEL_ENABLED` | if Sentinel | `false` | |
| `REDIS_SENTINEL_HOSTS` | if Sentinel | `localhost:26379` | `host:port,host:port` |
| `REDIS_SENTINEL_SERVICE_NAME` | if Sentinel | `mymaster` | |
| `REDIS_SENTINEL_USERNAME` / `REDIS_SENTINEL_PASSWORD` | if the Sentinels require AUTH | unset | Sentinel discovery credentials; may differ from the master's. |
| `SSE_NOTIFY_TOKEN` | recommended | unset | When set, `POST /sse/notify` requires header `X-Keep-Notify-Token`. Unset leaves the route open, and it reaches every browser. |
| `SSE_KEEPALIVE_INTERVAL_SECONDS` | no | `15` | Announced to the client, which sets its watchdog to `max(10s, 3 × interval)`. Must stay **below** your router/LB idle timeout or idle streams get cut. |

Not configurable: the Redis health-check interval is fixed at 30 s in code, so a
half-open pub/sub socket is detected and re-subscribed rather than hanging.

### keep-event-handler and keep-workflows

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SSE_NOTIFY_TOKEN` | with the gateway's | unset | Same value as the gateway; sent as `X-Keep-Notify-Token`. **No Redis settings here** — these services only POST to the gateway. |

`SSE_NOTIFY_WORKERS` (1), `SSE_NOTIFY_MAX_PENDING` (1000) and
`SSE_NOTIFY_COALESCE_ENABLED` (true) exist on both: **leave them at the defaults**.
Raising `SSE_NOTIFY_WORKERS` was measured to change nothing and trades away FIFO ordering.

Pre-existing but essential: the gateway's `KEEP_CORS_TRUSTED_ORIGINS` must contain the UI
origin — browser SSE is a cross-origin request, and without it no stream opens at all.

**Rollback**: set `SSE_FANOUT` back to `none` (or remove it) and the gateway falls back to
per-process delivery with no other behaviour change — Redis is on no request path other than
fan-out publish/subscribe. Verified by doing exactly this on a 5-pod deployment.

### Ordering constraint for the release

The gateway enforces `SSE_NOTIFY_TOKEN` from the moment it has the value, so if one release
rolls the gateway before the senders, every notification 401s until the event handler and
workflows catch up. Either roll the **senders first and the gateway second**, or accept a
short window — alerts still persist, only the live push is delayed, and the client's
catch-up refetch recovers them on the next event.

Overall order: event handler + workflows → gateway → keep-ui.

### What the Redis actually has to provide

The fan-out is **pub/sub only**: nothing is persisted, nothing is read back, and a lost
message costs at most one client refetch.

- **No persistence** — RDB/AOF off is fine and preferred; no PVC.
- **No keyspace notifications**, no modules, no Lua.
- **Connections**: 2 per gateway process (one publisher, one subscriber) =
  `2 × replicas × KEEP_WORKERS`. Five pods with one worker = 10 connections.
- **Memory**: negligible; no keys are stored.
- **Throughput**: one small JSON message per notification, delivered to every process.
- **Redis Cluster**: the code uses plain `PUBLISH`/`SUBSCRIBE`, not sharded pub/sub. It works
  on a cluster but broadcasts to all nodes; a single instance, Sentinel, or a managed
  non-clustered endpoint is the simpler fit.
- `redis/valkey-dev.yaml` is a reference for dev/integration only and encodes exactly these
  requirements (persistence off, 64Mi request). Use your platform's Redis in production.

### Why the prefix matters more than it looks

Every single-tenant Keep deployment uses the tenant id `keep`. Two deployments sharing one
Redis instance with an empty prefix therefore share both the channel *and* the tenant id —
so one environment's alerts can be pushed into the other's browsers. A distinct
`REDIS_KEY_PREFIX` per deployment is what prevents that.

### What each misconfiguration looks like

| Mistake | Symptom | Caught by |
|---|---|---|
| `SSE_FANOUT` not set to `redis` | everything looks healthy, viewers see ~1/N of events | `preflight/fanout-enabled` |
| Redis unreachable / wrong credentials | gateway still starts and serves (degrades to local delivery); `keep_sse_fanout_connected=0`, `keep_sse_fanout_errors_total{operation="subscribe"}` climbs | `preflight/fanout-connected`, `errors` |
| Empty `REDIS_KEY_PREFIX` on a shared Redis | cross-deployment delivery; startup warning in the gateway log | `preflight/key-prefix`, `config/channel` |
| Token on the gateway before the senders | every notification 401s; event handler logs notify failures | `token`, and `fanout` reports the 401 explicitly |
| Keepalive interval above the router idle timeout | idle streams cut by the proxy, reconnect loop | `connected` (shows the announced interval) |

## Reading a failure

- `preflight/fanout-enabled` fails → the images are deployed but the feature is off; the
  cross-pod behaviour will be identical to before.
- `fanout` passes but `e2e` fails → fan-out works, so look at the pipeline
  (event handler lag, Kafka, or the tenant mismatch below), not at SSE.
- Everything reads 0 → check the tenant. On some realms `POST /alerts/event` resolves to
  the generic tenant `keep` while reads and streams resolve to the caller's org tenant; if
  `TENANT_ID` is not the one ingestion lands in, nothing is ever delivered and it looks
  like total loss. Verify by POSTing an alert and GETting `/alerts/<fingerprint>` with the
  same auth header.
- `rolling` lands between 80–95 % → raw subscribers have no catch-up refetch; the browser
  recovers those. Judge the user impact with `browser_check.mjs rollout`.

## Known-unrelated issues you may hit

- A user with **no Keycloak org group** (e.g. `keep_admin`) cannot open the alerts feed:
  `/backend/workflows/query` 401s on the `keepActiveTenant=` token, the client signs out
  and the page reloads in a loop. Use a user with an org group. Pre-existing.
- The gateway no longer owns the schema (it moved to the `keep-migrations` image), so a
  fresh environment needs that Job to run before the services start.
