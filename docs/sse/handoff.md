# Real-time SSE — handoff

Last updated 2026-09-22. Everything needed to deploy the real-time SSE work, verify it in a
deployed environment, and finish the tests that are still owed.

| Path | What it is |
|---|---|
| [`acceptance-kit/`](acceptance-kit/) | **the thing you run** — acceptance checks for any deployed environment |
| [`deployment-validation-agent.md`](deployment-validation-agent.md) | runbook for an agent validating a deployment: guardrails, ordered steps, reporting contract |
| `keep-namespace/evidence-sse-validation-2026-09-17/` (not committed) | the raw before/after artifacts and the throwaway rig that produced them, on the validation machine. The numbers that matter are reproduced below. |

---

## 1. Where the work stands

Four PRs, all open, all `MERGEABLE`, **none merged**:

| Repo | PR | Branch head | Base |
|---|---|---|---|
| keep-api-gateway | **#91** | `ac64782` (merge of dev into the branch, 2026-09-17) | `dev` |
| keep-event-handler | **#59** | `2c7949b` | `dev` |
| keep-workflows | **#38** | `17dd21c` | `dev` |
| keep-ui | **#68** | `e8a6e531` | `dev` |

- #91 conflicted with dev (`poetry.lock` content-hash: dev dropped alembic in #92 while the
  branch adds `redis`). Fixed by merging dev into the PR branch, keeping dev's lock and
  re-hashing with Poetry 2.4.2. Result: 0 packages moved, PR diff still exactly the 12 SSE
  files, full gateway suite on the merged tree **747 passed / 0 failed**, CI green,
  `CONFLICTING → MERGEABLE/CLEAN`.
- **keep-ui #68's red check is infrastructure**, not code: a Docker Hub pull failure
  (`moby/buildkit` manifest) from 2026-09-09. Re-run it.
- Merge order when you do land them: **event handler + workflows → gateway → keep-ui**.

### What the work contains

| Tier | Repo | Change |
|---|---|---|
| Tier 1 | gateway | Redis/Valkey pub/sub fan-out behind `SSE_FANOUT=redis` |
| Tier 0.5 | gateway | streams close on SIGTERM; `connected` announces the keepalive; delivered/no-subscriber and closed-by-reason counters |
| Tier 0 | keep-ui | liveness watchdog, capped backoff with jitter, catch-up refetch on reconnect, tenant-scoped leader lock, 401 → session refresh |
| D2 | EH + workflows + UI | stop computing and sending `poll-presets` |

---

## 2. The defect, and why Redis is not optional

A browser's SSE stream lives in the memory of the one gateway pod that served it
(`sse_broadcaster` is a module-level dict). `POST /sse/notify` from the event handler
load-balances to *any* pod; a pod with no matching subscriber returns **204** and drops the
notification silently. With N gateway processes a viewer sees roughly 1/N of events, and
nothing logs an error.

The divisor is **`replicas × KEEP_WORKERS`** — each gunicorn worker is its own process with
its own broker.

Tier 1 gives the processes a shared bus: each subscribes to one Redis channel; a pod that
receives a notify delivers locally *and* publishes, and every other pod delivers to its own
subscribers. Nothing else in the stack can do this (Kafka is consumed by the event handler,
not the gateway), so **without `SSE_FANOUT=redis` the new images behave exactly like the
old ones for delivery.** You would still get: streams closing on SIGTERM, the UI
self-healing, no `poll-presets` work, and the notify token.

### Measured on a 5-pod deployment (image digest the only difference)

| | before | after |
|---|---|---|
| Real browser, 10 alerts | **4 of 10 ever appeared** (p50 5.7 s) | **10 of 10** (p50 1.05 s) |
| Real browser, 20 alerts across a rolling restart | **4 of 20** (p50 70 s) | **20 of 20** (p50 2.9 s) |
| Soak 300/min × 5 min, 6 subscribers | **1 subscriber got everything, 5 got nothing** (17 % of pairs) | **all 6** (98 %) |
| Notify one pod ×20, 5 pods subscribed | 20/20 on that pod, **0/20 on the other four** | **20/20 on all five** |
| Rolling restart, 6 raw subscribers | **47/312 = 15 %** | **344/354 = 97 %** |
| Pod delete → stream ends | **34.0 s**, keepalives still flowing | **1.02 s** |
| `poll-presets` in a 25-min window | **276** | **0** |
| Ingest→delivery latency / Kafka lag | p50 0.57 s / 0–1 | p50 0.55 s / 0–1 |

Unit suites: **zero regressions** in all four repos (gateway 544→576 passed with +32 new SSE
tests including 2 that need a real Redis; eh 657→660; wf 178→181; ui 469→489 tests).

---

## 3. Configuration (for Helm)

Only **keep-api-gateway** talks to Redis.

### keep-api-gateway

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SSE_FANOUT` | **yes** | `none` | Must be `redis`. Any other value keeps the per-process broker, silently. |
| `REDIS_HOST` / `REDIS_PORT` | yes (unless Sentinel) | `localhost` / `6379` | |
| `REDIS_DB` | no | `0` | **Does not isolate pub/sub** — channels are instance-wide whatever the DB index. |
| `REDIS_KEY_PREFIX` | **yes on a shared Redis** | `""` | Channel = `<prefix><SSE_FANOUT_CHANNEL>`. The only isolation there is; the gateway logs a warning when empty. |
| `SSE_FANOUT_CHANNEL` | no | `keep:sse` | Leave it; disambiguate with the prefix. |
| `REDIS_SSL` | if TLS | `false` | |
| `REDIS_USERNAME` / `REDIS_PASSWORD` | if auth | unset | ACL user needs **PUBLISH and SUBSCRIBE** on the channel. |
| `REDIS_SENTINEL_ENABLED` / `_HOSTS` / `_SERVICE_NAME` | if Sentinel | `false` / `localhost:26379` / `mymaster` | `host:port,host:port` |
| `SSE_NOTIFY_TOKEN` | recommended | unset | When set, `POST /sse/notify` requires `X-Keep-Notify-Token`. Unset = open route that reaches every browser. |
| `SSE_KEEPALIVE_INTERVAL_SECONDS` | no | `15` | Client watchdog = `max(10 s, 3 × interval)`. Must stay **below** the router/LB idle timeout. |

Not configurable: the Redis health-check interval is fixed at 30 s in code.

### keep-event-handler and keep-workflows

`SSE_NOTIFY_TOKEN` — same value as the gateway, sent as `X-Keep-Notify-Token`. **No Redis
settings.** Leave `SSE_NOTIFY_WORKERS` (1), `SSE_NOTIFY_MAX_PENDING` (1000) and
`SSE_NOTIFY_COALESCE_ENABLED` (true) at their defaults — raising workers was measured to
change nothing and trades away FIFO ordering.

### keep-ui

Nothing new. Pre-existing but fatal if wrong: the **gateway's** `KEEP_CORS_TRUSTED_ORIGINS`
must contain the UI origin — browser SSE is cross-origin and no stream opens without it.

### Three things that bite

1. **The prefix is not cosmetic.** Every single-tenant deployment uses tenant id `keep`. Two
   deployments sharing one Redis with an empty prefix share the channel *and* the tenant id,
   so one environment's alerts can be pushed into the other's browsers.
2. **Token ordering.** The gateway enforces from the moment it has the value. Roll **senders
   first, gateway second**, or accept a window of 401s (alerts still persist; the client's
   catch-up recovers them on the next event).
3. **Rollback is a config change.** `SSE_FANOUT=none` → per-process delivery, nothing else
   changes. Redis is on no request path other than fan-out publish/subscribe. Verified.

### What the Redis must provide

Pub/sub only: nothing persisted, nothing read back, a lost message costs one client refetch.

- **No persistence** (RDB/AOF off preferred), no PVC, no keyspace notifications, no modules.
- **Connections**: `2 × replicas × KEEP_WORKERS` (publisher + subscriber per process).
- **Memory**: negligible — no keys are stored.
- **Redis Cluster** works but uses plain `PUBLISH` (broadcast to all nodes), not sharded
  pub/sub; a single instance, Sentinel, or a managed non-clustered endpoint is simpler.
- [`acceptance-kit/redis/valkey-dev.yaml`](acceptance-kit/redis/valkey-dev.yaml) is a dev/integration reference encoding exactly
  these requirements. Ignore it if your chart provisions Redis.

---

## 4. Verifying a deployed environment

```bash
cd docs/sse/acceptance-kit
cp config.env.example config.env        # namespace, route, tenant, auth, notify token
set -a; . ./config.env; set +a

./run_acceptance.py --only config       # what Helm actually rendered
./run_acceptance.py                     # non-disruptive; safe on a shared dev env
./run_acceptance.py --disruptive        # + pod delete, rolling restart, Redis restart
./run_acceptance.py --json out.json     # machine-readable too

UI_ROUTE=https://keep-ui-...  node browser_check.mjs plain     # what a user sees
UI_ROUTE=https://keep-ui-...  node browser_check.mjs rollout   # ... across a restart
```

Exit code 0 = every selected check passed; each prints measured vs expected.
[`acceptance-kit/README.md`](acceptance-kit/README.md) has the per-check table, thresholds, and failure triage.

**The kit was exercised in both directions** on the 5-pod sandbox: with fan-out on, 8/8 pass;
with `SSE_FANOUT` unset, it fails exactly where it should (`fanout/cross-pod` 0/10 on every
pod). A green run therefore means something. One exception: the `config` check was added
after the sandbox token expired and is **untested** — verify it once, it only reads and prints.

**Before real users**, on dev/integration: `--disruptive`, both browser modes, and the two
gaps in §6.

---

## 5. Rebuilding a test environment from scratch

The sandbox used for the evidence (`ilay-cloud-dev`) **idles to zero and has no PVCs**, so
Postgres and Keycloak state are lost between sessions and the `oc` token expires daily.
the evidence directory's `tools/` rebuilds it:

| Step | Tool | Notes |
|---|---|---|
| Build before/after images for all 4 repos | `cluster/build_all.sh` | binary builds from git worktrees; tags each digest `:before` / `:after`, records `digests.txt` |
| Flip the whole namespace between builds | `cluster/switch_variant.sh before\|after` | env identical, image digest the only difference |
| Re-seed Keycloak (groups, alice/bob/carol, groups mapper, redirect URIs) | `cluster/kc_seed.py` | **always PUT full client representations** — a partial PUT deletes the resource server and crash-loops the gateway |
| Kafka topic at production shape | `kafka-topics --create --partitions 45` | |
| Presets per tenant (so the EH pays the real CEL cost) | `soak/seed_presets.py 20` | |
| Load / subscribers / metrics / analysis | `soak/run.sh RATE MIN SUBS [perturb]` | `perturb` adds a gateway roll at 50 % and a Redis restart at 66 % |
| Per-pod and end-to-end fan-out probes | `cluster/perpod.py`, `cluster/ingest_fanout.py` | |
| Shutdown / rolling restart | `cluster/rollout.py pod-delete\|rolling` | |
| Redis restart + malformed payload tolerance | `cluster/fanout_robustness.py` | |
| Browser through Keycloak | `probes/cluster_browser.mjs` | logs in, switches tenant, measures time-to-visible |

Image digests from the 2026-09-17 build are in `tools/cluster/digests.txt` (they stay valid
while the image stream does).

### Using the tools

They were written in a session scratchpad, so before running any of them:

```bash
export SSE_WORK_DIR=/some/work/dir     # must contain before/ and after/ git worktrees
cd "$SSE_WORK_DIR"
for r in keep-api-gateway keep-event-handler keep-workflows keep-ui; do
  git -C ~/keep-namespace/$r worktree add --detach "$SSE_WORK_DIR/before/$r" <before-sha>
  git -C ~/keep-namespace/$r worktree add --detach "$SSE_WORK_DIR/after/$r"  <after-sha>
done                                    # SHAs are in tools/shas.txt
cp -R <evidence-dir>/tools/* "$SSE_WORK_DIR/"
```

Each script now fails fast with a clear message if `SSE_WORK_DIR` is unset. Two more edits
are needed for a different environment:

- `tools/soak/common.py` — hardcodes the sandbox `KC`/`GW` routes and the tenant map. Change
  those three constants.
- `tools/cluster/*.py` — read the notify token from `cluster/notify_token.txt`; recreate it
  per `cluster/notify_token.README` (it is deliberately not stored).

The Python tools need `requests` (any venv); the browser ones need `playwright` — easiest is
to run them with `node_modules` from a keep-ui checkout on the path.

**Environment quirk that wasted a run:** on that realm `POST /alerts/event` always resolves
to the generic tenant `keep`, while reads and streams resolve to the caller's org tenant. If
the probe's tenant is not the one ingestion lands in, everything measures zero for the wrong
reason. Check with: POST an alert, then GET `/alerts/<fingerprint>` with the same auth.

---

## 6. Still owed

1. **Tier 0 client-resilience scenarios** — 75 s outage, frozen (SIGSTOP) stream, SIGTERM
   rollover, cross-process in the browser, two-tab handover. Needs the local stack:
   `tools/local/stack.sh up before|after`, then `tools/probes/run_browser.sh <variant>`
   (scenarios `tabs crossproc rollover outage silent`), faults via `tools/local/ctl.sh`.
   The local stack runs its own compose project (`ssevalidate`) with isolated volumes, the UI
   on **:3100** so it does not disturb anything on :3000, and writes nothing into the repos.
   Only `tabs` was completed (1 shared stream, 0.06 s handover).
2. **The 12-check sanity E2E suite** on both builds:
   `cd <tree>/keep-ui && UI_BASE_URL=http://localhost:3100 GATEWAY_URL=http://localhost:8080
   WORKFLOWS_URL=http://localhost:8081 npm run test:e2e`.
3. **Key-prefix isolation** — two deployments on one Redis with different prefixes must not
   see each other. Not exercised; it is the one claim in §3 that rests on code reading.
4. **A soak at your production shape and rate.** The 2026-09-08 sessions measured the event
   handler as the throughput ceiling (~38.5 ms/event single-threaded after D2) and gateway
   pods idling at ~750 MiB against a 1000 MiB limit — worth re-checking with your numbers.

---

## 7. Known issues that are *not* this work

| Issue | Detail |
|---|---|
| **No-group users cannot open the alerts feed** | `keep_admin`, `carol` and any user without a Keycloak org group: `/backend/workflows/query` returns 401 *"could not find any group that represents the org and the role"* on the UI's `keepActiveTenant=` token, the client signs out, the page reloads in a loop (measured: 47 navigations, 10 such 401s in one clean login). A plain bearer for the same user returns 200; alice/bob are fine. Fix: a tenant-role-grant row on `keep`, or stop signing out on a workflows 401. |
| **Ingestion resolves to the generic tenant** | See §5. Pre-existing. |
| **The gateway no longer owns the schema** | Since gw #85/#92 the schema lives in the separate `keep-migrations` image (`keep-migrate --target head`, an Argo PreSync Job). A fresh environment needs that Job before the services start, and the sanity-check skill's `deploy.sh` has no migration step, so a `--clean` local deploy from dev leaves an empty database. |
| **Testing trap** | Running pytest in a git worktree of keep-workflows imports `src/` from the **main checkout** (no `tests/__init__.py`, and a `.pth` in the shared venv wins). Always `PYTHONPATH=.`; it produced five convincing phantom "regressions". |
| **Metrics double count** | The event handler's `keep_events_in_total` increments twice per alert (two `inc()` sites). Pre-existing, cosmetic. |

---

## 8. Evidence index

| Claim | File |
|---|---|
| Protocol and expectations, written before the runs | `evidence-sse-validation-2026-09-17/test-plan.md` |
| All before/after results and the verdict | `evidence-sse-validation-2026-09-17/findings.md` |
| Unit suites, both trees, per-repo | `evidence-sse-validation-2026-09-17/unit/unit-report.md` + junit XML |
| Per-pod fan-out, end-to-end, token, connected, sync-route | `evidence-sse-validation-2026-09-17/cluster/*-before.json` / `*-after.json` |
| Soak (raw load/subscriber CSVs) | `evidence-sse-validation-2026-09-17/cluster/soak-*-report.md`, `tools/soak/` |
| Shutdown + rolling restart | `evidence-sse-validation-2026-09-17/cluster/poddelete-*.json`, `rolling-*.json` |
| Redis restart + malformed payloads | `evidence-sse-validation-2026-09-17/cluster/fanout-robustness-after.json` |
| Real browser, both builds | `evidence-sse-validation-2026-09-17/cluster/browser-cluster-*.json` |
| Local single-host probes (before) | `evidence-sse-validation-2026-09-17/local/local-probes-before.json` |

The validation itself was read-only: no branch, commit, merge or file change was made in any
repository while measuring. The only repository write in the whole exercise was the
explicitly requested conflict fix pushed to PR #91's own branch; `dev` was never touched.
