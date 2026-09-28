# Agent runbook — validate an SSE deployment

**You are validating a deployed environment, not changing one.** Read this whole file before
running anything. Companion: [`handoff.md`](handoff.md) (what the work is, full configuration
reference); the checks you run live in [`acceptance-kit/`](acceptance-kit/).

## Mission

Confirm, with evidence, that:

1. the four services are running the build that contains the real-time SSE work;
2. Redis is configured correctly for the cross-pod fan-out — **this is the part that is
   silently wrong most often**;
3. a viewer actually receives every alert, including through a rolling restart;
4. nothing regressed.

Finish by reporting PASS/FAIL per check with the measured numbers, and a single verdict:
*safe to expose to users* or *not, because X*.

## Guardrails

- **Dev / integration only.** Several checks delete a pod, roll the gateway Deployment and
  restart Redis. Never run `--disruptive` against production. If you cannot tell which you
  are pointed at, stop and ask.
- **Do not modify application config to make a check pass.** If `SSE_FANOUT` is unset, that
  is a finding to report, not something to fix by hand — the deployment is Helm-managed and
  a manual `oc set env` would be overwritten on the next sync and would invalidate the run.
- **Do not edit, commit or push repository files.** Do not merge PRs.
- **Do not fabricate numbers.** Any check you could not run is reported as "not run", never
  inferred from another result.
- **Stop and report** if: preconditions are missing, more than two checks fail, or the
  environment turns out to be production.

## Step 0 — Preconditions

```bash
oc whoami && oc project            # you must be logged in, on the target namespace
oc get pods -l app=keep-api-gateway -o wide
```

Record: namespace, gateway replica count, image digests of all four deployments.

```bash
oc get deploy -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image' --no-headers | grep keep
```

**Is this actually the new build?** Two markers, both cheap. The `connected` event carries
`keepalive_seconds`, and `/metrics` exposes `keep_sse_fanout_connected`:

```bash
POD=$(oc get pods -l app=keep-api-gateway -o jsonpath='{.items[0].metadata.name}')
oc exec "$POD" -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/metrics',timeout=15).read().decode())" | grep keep_sse
```

No `keep_sse_*` metrics at all → the old build is deployed. Stop and report that; nothing
below is meaningful.

## Step 1 — Configuration audit (the highest-value step)

```bash
cd docs/sse/acceptance-kit
cp config.env.example config.env    # fill in namespace, route, tenant, auth, notify token
set -a; . ./config.env; set +a
./run_acceptance.py --only config
```

Check each of these against `handoff.md` §3, and report any that are wrong:

| # | What to confirm | Why it matters |
|---|---|---|
| 1 | `SSE_FANOUT=redis` on **every** gateway pod | Default is `none`. With it unset everything looks healthy and viewers silently see ~1/N of alerts. |
| 2 | `REDIS_KEY_PREFIX` non-empty and unique to this deployment | The channel is `<prefix>keep:sse`. Every single-tenant Keep uses tenant id `keep`, so two deployments sharing a Redis with no prefix will deliver each other's alerts into the wrong browsers. |
| 3 | The team did **not** rely on `REDIS_DB` for isolation | Pub/sub channels are instance-wide whatever the DB index. `REDIS_DB` isolates data operations only. |
| 4 | `SSE_NOTIFY_TOKEN` identical on gateway, event handler **and** workflows | A value on the gateway alone makes every notification 401. Absent everywhere = the notify route is open to anything that can reach it. |
| 5 | `keep_sse_fanout_connected = 1` on every pod | 0 means the gateway started but never reached Redis — it degrades to local delivery and keeps serving. |
| 6 | Subscriber count on the channel equals `replicas × KEEP_WORKERS` | Each gunicorn worker is its own broker. A count below that means some processes are not subscribed. |
| 7 | Gateway `KEEP_CORS_TRUSTED_ORIGINS` contains the UI origin | Browser SSE is cross-origin; without it no stream opens at all and every delivery check below is meaningless. |
| 8 | `SSE_KEEPALIVE_INTERVAL_SECONDS` (default 15) is below the router/LB idle timeout | Otherwise the proxy cuts idle streams and clients reconnect in a loop. |

For 6, ask the Redis directly:

```bash
oc exec deploy/<redis> -- valkey-cli PUBSUB NUMSUB "<prefix>keep:sse"   # or redis-cli
```

## Step 2 — Non-disruptive checks

```bash
./run_acceptance.py --json acceptance-$(date +%Y%m%dT%H%M%S).json
```

Expected, on a multi-pod deployment:

| Check | Pass |
|---|---|
| `fanout/cross-pod` | 10/10 on **every** pod (notify goes to one pod only) |
| `delivery/end-to-end` | 5/5 on every pod, ingest HTTP 202 |
| `shutdown/keepalive-announced` | `connected` contains `keepalive_seconds` |
| `security/notify-token` | 401 / 401 / 401 / 204 |
| `d2/poll-presets-removed` | 0 log lines |
| `fanout/errors` | all counters zero |

`fanout/cross-pod` delivering only to the notified pod is **the defect this work fixes** —
report it as a configuration failure (almost always #1 or #5 above), not as a code bug.

## Step 3 — What a user sees

```bash
UI_ROUTE=https://<ui-route> node browser_check.mjs plain
```

Every ingested alert must become a row, p50 a few seconds. On the broken configuration this
lands around 4 of 10.

Log in as a user **who belongs to a Keycloak org group** (e.g. `alice`). A user with no org
group cannot open the feed at all for an unrelated pre-existing reason — see §Known below.

## Step 4 — Disruptive checks (dev/integration only)

```bash
./run_acceptance.py --disruptive --json acceptance-disruptive-$(date +%Y%m%dT%H%M%S).json
UI_ROUTE=https://<ui-route> node browser_check.mjs rollout
```

| Check | Pass | Broken config looks like |
|---|---|---|
| `shutdown/stream-closes-on-sigterm` | stream ends <5 s after the pod is deleted | ~30 s, keepalives still arriving |
| `delivery/through-rolling-restart` | ≥95 % of viewer-alert pairs | ~15 % |
| `fanout/redis-restart` | stream survives, delivery resumes | subscription never recovers |
| `browser_check.mjs rollout` | every alert visible | ~4 of 20 |

A raw-subscriber result between 80–95 % on `rolling` is acceptable: the probe has no
catch-up refetch, the browser does. Judge user impact from the browser run.

## Step 5 — Redis fitness

Confirm with whoever owns the Redis:

- **Persistence off is fine and preferred** — the fan-out is pub/sub only, nothing is read
  back, a lost message costs at most one client refetch. No PVC, no AOF/RDB, no keyspace
  notifications, no modules.
- **Connections**: `2 × replicas × KEEP_WORKERS` (one publisher + one subscriber per
  process). Check this against the instance's connection budget.
- **ACL**: the user needs `PUBLISH` **and** `SUBSCRIBE` on `<prefix>keep:sse`.
- **Redis Cluster** works but uses plain `PUBLISH` (broadcast to all nodes), not sharded
  pub/sub. A single instance, Sentinel or a managed non-clustered endpoint is the better fit.

## Step 6 — Report

Report exactly this, and nothing you did not measure:

```
Environment: <namespace>, <N> gateway pods x KEEP_WORKERS=<W>, image digests <...>
Verdict: SAFE TO EXPOSE / NOT SAFE - <one line>

Configuration audit:   <8 items, PASS/FAIL each, with the observed value>
Non-disruptive:        <check: measured vs expected>
Browser:               <visible/posted, p50, max>
Disruptive:            <check: measured vs expected>   (or "not run - reason")
Redis fitness:         <connections, persistence, ACL, topology>

Failures, each with: what was measured, the most likely cause, and the config change needed.
Not run: <checks and why>
```

Attach the `--json` outputs.

## Known issues that are NOT this work — do not chase them

| Symptom | Reality |
|---|---|
| A user without a Keycloak org group (`keep_admin`, `carol`) hits an infinite redirect loop on the alerts feed | `/backend/workflows/query` 401s on the UI's `keepActiveTenant=` token, the client signs out and reloads. Pre-existing; use a user with an org group. |
| Every delivery check reads 0 | On some realms `POST /alerts/event` resolves to the generic tenant `keep` while reads and streams resolve to the caller's org tenant. If `TENANT_ID` is not the tenant ingestion lands in, nothing is ever delivered. Verify: POST an alert, then GET `/alerts/<fingerprint>` with the same auth header. |
| Fresh environment, services up but everything 500s on missing tables | The gateway no longer owns the schema; the `keep-migrations` image must run before the services start. |
| `keep_events_in_total` counts twice per alert | Pre-existing double increment in the event handler. Cosmetic. |

## If you are asked to go further

The following were **not** covered by the 2026-09-17 validation and are still open:
Tier 0 client-resilience scenarios (outage, frozen stream, SIGTERM rollover) which need a
local stack, the 12-check sanity E2E suite, key-prefix isolation between two deployments
sharing one Redis, and a soak at production shape and rate. See `handoff.md` §6.
