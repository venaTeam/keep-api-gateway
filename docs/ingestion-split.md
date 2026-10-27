# Ingestion split: who does what now

Merged to `dev` on 2026-10-11 in three repos:

| Repo | PRs | Commit on `dev` |
|---|---|---|
| `keep-api-gateway` | #100 `remove-alerts-ingestion` | `75f6e5c` |
| `keep-event-handler` | #64 `remove-non-alert-events`, #63 `resolve-tenant-here` | `5aace03` |
| `keep-ingestion` | #2 `secret`, #3 `feat/split-ingestion-service` | `7d37130` |

Read this before resolving a merge conflict against `dev` in any of the three.
The rules in [Resolving conflicts](#resolving-conflicts) are the short version.

## The rule

Each service now has one job, and the dependencies that go with it and no others.

| Service | Its only job | Postgres | Kafka |
|---|---|---|---|
| `keep-ingestion` | Receive new alerts over HTTP and publish them | **none** | produces |
| `keep-event-handler` | Consume **new alert** events and process them | reads and writes | consumes |
| `keep-api-gateway` | Serve the UI: reads, and every user-driven change (status, assign, note, enrich, delete, incidents) | reads and writes | **does not produce** |

```
sender ──POST /alerts/event*──▶ keep-ingestion ──▶ Kafka (keep-events) ──▶ keep-event-handler ──▶ Postgres
                                                                                                     ▲
UI ──────────────────────────▶ keep-api-gateway ─────────────────────────────────────────────────────┘
```

## Why

Before, the gateway did three things at once: it accepted alerts, it served the
UI, and for every UI change it wrote the DB **and** published an `enrich` /
`batch_enrich` / `delete` event that the event handler then applied a second
time. Three consequences:

- Alert intake shared a process, a deploy and a database dependency with the UI.
  A DB incident, a migration or a UI-driven gateway deploy could stop alerts
  being accepted, and an alert a sender stops retrying is lost.
- A UI change depended on Kafka being up, and was written twice by two services.
- Every service carried the others' dependencies (the gateway a Kafka producer,
  ingestion a DB role and a schema wait, the handler enrich/delete/incident
  paths).

Now intake needs only Kafka, UI changes need only Postgres, and each write has
exactly one owner.

## What changed, per repo

### keep-ingestion — intake only, no database

Serves `POST /alerts/event`, `POST /alerts/event/{provider_type}`,
`GET /alerts/event/netdata`, plus `/healthcheck`, `/readyz`, `/metrics`.

- **No DB code at all.** Removed: `src/repositories/db.py`, `db_on_start.py`,
  `db_utils.py`, `src/models/db/*` (tenant, user, operator, helpers), all
  `DATABASE_*` constants, the startup schema wait (`migrate_db()`), and
  `dispose_session()` on startup.
- **Auth is a single shared secret.** `AuthVerifierBase` compares the
  `X-API-KEY` header to `KEEP_INGESTION_API_KEY` (required; the service raises at
  import without it) using `hmac.compare_digest`. No `tenantapikey` lookup, no
  `last_used` stamping, no impersonation, no bearer tokens.
- **No operator → tenant routing here.** That needed the `operator` table.
  `_resolve_ingestion_tenant()` now just returns `KEEP_INGESTION_TENANT_ID` or
  `"keep"`. The real tenant is resolved downstream (see event handler).
- **`/readyz` checks only the Kafka producer** (`KEEP_READYZ_REQUIRE_PRODUCER`,
  `KEEP_READYZ_CHECK_TIMEOUT`). No DB or schema check.
- Unchanged: the 202 body, the 503 + `Retry-After` rejection, the DLQ fallback,
  and the `keep_alert_ingestion_total` / `keep_alert_ingestion_error_total`
  metrics, which now live only here.

### keep-event-handler — new alerts only

- **`process_event_sync` handles `alert` and nothing else**
  (`src/controllers/event_controller.py`). Removed: `_process_enrich_event`,
  `_process_batch_enrich_event`, `_process_delete_alert_event`,
  `_process_incident_event`, `FUNC_MAP`, and `process_event_wrapper`.
- **A non-alert event is logged and skipped, not retried.** Old `enrich` /
  `delete` / `incident` messages can still be in the topic backlog; they return
  `None` and their offset is committed.
- `EventType` moved to `src/models/event_dto.py`; import it from there.
- **Operator → tenant routing lives here now.** New
  `src/core/tenant_resolution.py`, `get_operator_by_name()` in
  `src/core/db/db.py`, and the `Operator` model in `src/models/db/operator.py`.
  `KafkaEventConsumer._process_with_retries` calls `_resolve_ingestion_tenant()`
  **inside the retry loop**, so a DB blip is retried instead of dropping the
  alert. It **overwrites** the `tenant_id` on the message: alert `operator` maps
  to a tenant → that tenant; no operator or unknown operator → `"keep"`
  (general). For a batch, the first alert's operator decides.

### keep-api-gateway — UI requests, direct DB writes

- **No Kafka producer.** Deleted `src/services/producers/kafka_producer.py`,
  `factory.py`, `base_event_handler.py`; `start_event_producer()` /
  `stop_event_producer()` are gone from `main.py`. (`event_subscriber.py` is
  still there and untouched.)
- **No intake routes.** `POST /alerts/event`, `POST /alerts/event/{provider_type}`
  and `GET /alerts/event/netdata` are removed from `src/routes/alerts.py`, with
  their helpers (`_retry_later`, `_ingestion_response`,
  `_publish_failed_response`, `_extract_operator`, `_resolve_ingestion_tenant`)
  and `get_operator_by_name` in `db.py`.
- **`EnrichmentsBl` writes the DB and stops.** The `event_producer` constructor
  argument and the `produce_event` / `event_type` parameters of `enrich_entity`,
  `batch_enrich` and `disposable_enrich_entity` are gone. No route takes
  `event_producer: EventProducer = Depends(get_event_producer)` any more
  (alerts and incidents).
- **Hard delete is done here.** `DELETE /alerts` with `soft_delete=false` calls
  the new `delete_alert(tenant_id, fingerprint)` in `src/repositories/db.py`,
  which removes the fingerprint's rows from `LastAlertToIncident`, `LastAlert`,
  `CommentMention`, `AlertAudit` and `Alert`, always scoped to the tenant. It
  used to be a `delete` event for the handler.
- **`/readyz` checks only the DB and schema.** `KEEP_READYZ_REQUIRE_PRODUCER` no
  longer exists here.
- Removed the unauthenticated CloudWatch/SNS path
  (`KEEP_CLOUDWATCH_DISABLE_API_KEY`) from `authverifierbase.py`; it only served
  the intake route.
- `get_last_alert_by_fingerprint(for_update=True)` now also sets
  `populate_existing=True`, so the locked row is re-read instead of returning the
  session's stale copy. Keep it: the gateway is now the only writer of these
  fields and relies on reading current values under the lock.
- Tests: `MockEventProducer` / `client.mock_producer` are replaced by
  `AlertSeeder` / `client.alert_seeder` and the `send_alert(client, event, ...)`
  helper in `tests/fixtures/client.py`, which writes alerts straight to the DB in
  place of the handler. Deleted `test_dlq_ingestion_reporting.py`,
  `test_messaging.py`, `test_producer_bounds.py` and the CloudWatch auth tests;
  added `test_delete_alert.py`.

## Resolving conflicts

If your branch touches the same code, keep `dev`'s structure and re-apply your
change on top of it.

**keep-api-gateway**

- Do not bring back `event_producer` — not as a route dependency, not as an
  `EnrichmentsBl(...)` argument, not as `produce_event=` / `event_type=`. If your
  branch added a call with those, drop the arguments.
- A new user action (new status, new enrichment, new bulk action) is a DB write
  in the gateway. Do not publish an event for it.
- Do not re-add anything under `/alerts/event`. Intake changes go to
  `keep-ingestion`.
- Do not import from `src.services.producers.factory`,
  `base_event_handler` or `kafka_producer`; they no longer exist.
- In tests, replace `client.post("/alerts/event...")` with
  `send_alert(client, event, provider_type=..., provider_id=...)`, and assertions
  on `mock_producer.produced_events` with assertions on the DB.

**keep-event-handler**

- Do not re-add handlers or `FUNC_MAP` entries for `enrich`, `batch_enrich`,
  `delete` or `incident`. If your branch changed one of those handlers, the
  change belongs in the gateway's equivalent (`EnrichmentsBl`,
  `repositories/db.py`, `incidents_bl.py`).
- Keep the tenant resolution call inside the retry loop in
  `_process_with_retries`, before `process_event_sync`.
- Do not trust `tenant_id` from the Kafka message; it is overwritten.

**keep-ingestion**

- Nothing here may import SQLAlchemy/SQLModel, open a session, or read a
  `DATABASE_*` variable. If your change needs data from the DB (tenant, operator,
  API key, provider), do it in `keep-event-handler` after consumption.
- Keep auth as the `KEEP_INGESTION_API_KEY` comparison.
- Keep the response contract (202 body, 503 + `Retry-After`) unchanged; senders
  depend on it.

## Known leftovers

These are still in `dev` and are worth knowing about; none was changed in this
work.

- **Webhook URL and key shown in the UI.** The gateway's `GET /settings/webhook`,
  `GET /providers/{type}/webhook`, provider webhook install and the CLI
  `simulate` still build `{KEEP_API_URL}/alerts/event...`, which only works if
  the ingress routes that path to `keep-ingestion` (each spot has a `NOTE`
  comment). `GET /settings/webhook` also still hands out a per-tenant DB API key,
  which `keep-ingestion` does not accept — it accepts only
  `KEEP_INGESTION_API_KEY`.
- **`keep-ingestion` docs and packaging still mention a database.** `README.md`
  (read-only role, schema wait, `KEEP_SCHEMA_*`), the `healthcheck.py` module
  docstring, `SKIP_DB_CREATION=true` in the `Dockerfile`, and the
  `sqlmodel` / `sqlalchemy` / `psycopg*` entries in `pyproject.toml` are stale.
  The code uses none of them.
- **Leftovers in `keep-event-handler`.** `delete_alert` in `src/core/db/db.py`
  has no caller left. `enrich_entity`, `batch_enrich` and
  `create_incident_from_dict` are still there and still used *inside* alert
  processing (mapping/extraction enrichment, incident correlation) — they are
  just no longer reachable from a Kafka event of their own. `EventType` still
  lists the non-alert values, which is what lets backlog messages be parsed and
  skipped.
- **Local run.** `keep-ingestion` needs `KEEP_INGESTION_API_KEY` set, and Kafka
  only; it does not need Postgres or the gateway to be up.
