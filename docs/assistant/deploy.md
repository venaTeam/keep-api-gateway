# Keep Assistant: deploying and operating it

Keep Assistant is the chat in the UI's side panel. It answers questions about alerts
and drafts status changes that the user confirms. It is part of this service: the code
is `src/services/assistant/`, the routes `src/routes/assistant.py`. There is nothing
else to deploy.

It calls this app's own routes in-process with the user's token, so it can read and
change exactly what that user can. Only Confirm writes, and only with `batch_enrich`.

## The switch

`KEEP_ASSISTANT_ENABLED`, off by default. Off means that `/assistant/*` answers 404,
no loop runs and no OpenAI client is built, whatever else is set. `OPENAI_API_KEY` alone
does not turn it on: the other AI features use the same key.

With the switch on and no usable `OPENAI_API_KEY` or `OPENAI_BASE_URL`, only new chats
are refused (503 `llm_not_configured`). Conversations can still be read, and drafts
confirmed and discarded.

keep-ui has a switch of its own, also `KEEP_ASSISTANT_ENABLED`, for the panel; it takes
only `true`. The UI needs no other setting: it reaches `/assistant/*` through its existing
`/backend` rewrite.

## Rollout

1. **keep-migrations** (PreSync Job): the tables `assistant_conversation`,
   `assistant_draft` and `assistant_message`. The upgrade blocks writes to `tenant` until
   its transaction ends. If `KEEP_SCHEMA_EXPECTED_REVISION` is set, bump it to this
   release's head in the same release: keep-event-handler waits for it otherwise.
2. **keep-api-gateway**, switch off.
3. **keep-ui**, switch off.
4. Set the gateway's `KEEP_ASSISTANT_ENABLED=true`. Check that `/assistant/chat` no longer
   answers 404 and that `keep_assistant_llm_configured` is 1.
5. Set keep-ui's `KEEP_ASSISTANT_ENABLED=true`.

Without the tables the assistant answers 503 `not_ready`; the rest of the gateway does
not notice, and `/readyz` does not look at them.

## Rollback

1. keep-ui's switch off: the panel is gone.
2. The gateway's switch off: the routes and loops are gone. Old conversations stay in the
   database, and retention does not run while it is off.

That is all that is needed. Roll back the images after that if you have to.
**Never downgrade the migration**: it drops the conversations, and takes an exclusive lock
on `tenant`. Drafts that were being confirmed at that moment stay `applying`; they get
their result when the switch is on again.

Rolling the gateway back to a version from before this feature also takes away the `noc`
role's permission to change alert status and the soft-delete guard.

## Environment

| Name | Default | |
|---|---|---|
| `KEEP_ASSISTANT_ENABLED` | off | `true`, `1`, `yes` or `on` |
| `OPENAI_API_KEY` | – | shared with the other AI features |
| `OPENAI_MODEL_NAME` | `gpt-4o-2024-08-06` | |
| `OPENAI_BASE_URL` | OpenAI's | https, without credentials in it |
| `ASSISTANT_OPENAI_TIMEOUT_SECONDS` | 60 | one LLM call |
| `ASSISTANT_OPENAI_REASONING_EFFORT` |  | not sent when empty; `none` for models that refuse function tools on chat completions otherwise, such as `gpt-6-luna` |
| `ASSISTANT_TURN_DEADLINE_SECONDS` | 90 | one chat turn |
| `ASSISTANT_MAX_TOOL_ROUNDS` | 4 | per turn |
| `ASSISTANT_MAX_DRAFTS_PER_TURN` | 3 | |
| `ASSISTANT_MAX_COMPLETION_TOKENS` | 1024 | per LLM call |
| `ASSISTANT_HISTORY_MAX_MESSAGES` | 30 | what the model sees of a conversation |
| `ASSISTANT_HISTORY_MAX_CHARS` | 48000 | |
| `ASSISTANT_MAX_USER_MESSAGE_CHARS` | 4000 | the UI's composer has its own limit of 4000 |
| `ASSISTANT_HEARTBEAT_SECONDS` | 10 | keep it well under the 30 s idle timeout of the UI's proxy |
| `ASSISTANT_FIELDS_CACHE_TTL_SECONDS` | 600 | alert field names, per tenant |
| `ASSISTANT_RATE_LIMIT_PER_MINUTE` | 10 | messages per user |
| `ASSISTANT_RATE_LIMIT_PER_DAY` | 200 | messages per user |
| `ASSISTANT_DRAFT_TTL_SECONDS` | 1800 | how long a draft can be confirmed |
| `ASSISTANT_GATEWAY_WHOAMI_TIMEOUT_SECONDS` | 3 | calls to this app's own routes |
| `ASSISTANT_GATEWAY_READ_TIMEOUT_SECONDS` | 5 | |
| `ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS` | 12 | Confirm's status changes (`batch_enrich`); at most 12, because Confirm has 26 s for everything: a larger value is logged and 12 is used |
| `ASSISTANT_DB_POOL_SIZE` | 3 | per worker, next to the gateway's own pool |
| `ASSISTANT_DB_MAX_OVERFLOW` | 2 | |
| `ASSISTANT_DB_POOL_TIMEOUT` | 3 | seconds |
| `ASSISTANT_DB_CONNECT_TIMEOUT` | 3 | seconds |
| `ASSISTANT_DB_STATEMENT_TIMEOUT_MS` | 5000 | |
| `ASSISTANT_DB_SCHEMA_CHECK_TIMEOUT_MS` | 2000 | |
| `ASSISTANT_RETENTION_ENABLED` | true | |
| `ASSISTANT_RETENTION_DAYS` | 90 | |
| `ASSISTANT_RETENTION_INTERVAL_SECONDS` | 86400 | |
| `ASSISTANT_RETENTION_BATCH_SIZE` | 200 | conversations per transaction |
| `ASSISTANT_RETENTION_LOCK_KEY` | 7316902254418086 | advisory lock; must not be keep-migrations' 8274419300112233 |

The database is the gateway's own; `DATABASE_POOL_*` and the like do not reach the
assistant. A value that cannot be read, or that is not above 0, is logged and the default
is used. Only `ASSISTANT_DB_MAX_OVERFLOW`, `ASSISTANT_MAX_TOOL_ROUNDS`,
`ASSISTANT_MAX_DRAFTS_PER_TURN`, `ASSISTANT_FIELDS_CACHE_TTL_SECONDS` and the rate limits
may be 0. `ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS` above 12 is logged and 12 is used.

The assistant's connections set `statement_timeout` through the `options` startup
parameter, in place of an `options` in the connection string. PgBouncer refuses that
parameter unless it is in its `ignore_startup_parameters`, and then the timeout does not
apply.

## What the deployment must provide

- **Egress** from the gateway pods to the OpenAI endpoint, over https.
- **No buffering of `text/event-stream`** between the browser and the gateway, and an idle
  timeout of at least 60 s at every proxy on the way. The stream carries
  `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`, and a heartbeat
  every 10 s. A turn can last 90 s.
- **Database connections:** up to 5 more per worker.
- **Shutdown:** gunicorn's default graceful timeout (30 s) and a
  `terminationGracePeriodSeconds` above it, with room for the `preStop` hook.
- **Replicas:** any number. Locks, limits and drafts are rows; the loops of several
  workers do not get in each other's way.

### Shutdown

On SIGTERM a worker ends running turns as `interrupted` and frees their locks, and answers
new chats and Confirms with 503 `not_ready`. A Confirm that has not sent its write goes
back to `pending`; one that has sent it answers within 8 s, or its draft stays `applying`
until somebody looks at the conversation again. Writes nobody waits for any more get up to
8 s, next to the gateway's own shutdown (up to 20 s), after up to 8 s in which open
responses end: 28 s of the 30. A response does not wait longer than that for a slow
database; what it wanted to store gets its chance next to the gateway's shutdown.

A `batch_enrich` that takes longer than that is cut off, as it was before the assistant.
So is a single database statement of retention (60 s at most): its transaction rolls back
and the next run repeats it.

## Load

Everything runs in the gateway's own process and event loop. The calls to the app's own
routes are requests like any other: they count in its HTTP metrics and against its rate
limits, with the caller's address. What bounds it:

| What | Bound |
|---|---|
| Messages per user | 10 a minute, 200 a day |
| Running turns per user | 2, one per conversation |
| Calls to the app's own routes | 8 at once per worker, then `gateway_unavailable`: 1 kept for Confirm's write, 1 for critical calls (who is asking, Confirm's looks, reconcile's look), 6 for chats |
| Filters the model wrote | 4 of the 6 at once; the next one waits 5 s for its turn |
| Reads of single alerts | 4 of the 6 at once; the others wait. A critical look reads 2 at once, in places of its own |
| Database threads | 5 per worker, one per connection |
| Alerts per draft | 20 |
| Drafts per turn | 3 |
| Request body | 256 KiB |

`batch_enrich` blocks the event loop while it runs, for the status modal as for Confirm.
Running chats stand still for that long.

A call nobody waits for any more (deadline, closed panel) runs to its end and keeps its
place until then: the query routes have no statement timeout. That is what the 4 for
filters are for: however slow they are, the other calls still get a place. However
many places chats hold, who is asking, a Confirm and its write still get theirs. Two
critical calls at the same moment, with every chat place taken, can still refuse one:
`gateway_requests_total{status_class="not_sent"}` counts it.

## Data

- Conversations, drafts and messages are kept 90 days after they were last used. The rest
  of a long conversation is cut at a user's message. Nothing in the UI deletes a
  conversation; to erase one, delete its rows from `assistant_message`, `assistant_draft`
  and `assistant_conversation`, in that order, by `conversation_id`.
- The messages of a conversation, the alerts a user selected or the model looked up, and
  the names of alert fields go to OpenAI, with `store` off, and with a hash that stands
  for the user. Another endpoint (`OPENAI_BASE_URL`) gets neither of the last two.
- Logs and error bodies of the assistant hold no message text, filter, alert value or
  exception text. The routes it calls log as they do for the UI.

## Alerts

[`alerts.yml`](alerts.yml) holds Prometheus rules for the metrics below. They are not
deployed from here: copy them into the monitoring setup and route `severity` there. The
thresholds are starting points.

| Metric | Says |
|---|---|
| `keep_assistant_llm_configured` | 0 in a worker that cannot call the LLM |
| `keep_assistant_llm_errors_total` | by `kind`: `unavailable`, `rate_limited`, `timeout`, `rejected`, `misconfigured` (401, 403 or 404) |
| `keep_assistant_llm_tokens_total` | by `tenant_id`, `model` and `type`: one key serves all tenants |
| `keep_assistant_turns_total` | by `result`: `ok`, `error`, `cancelled`, `tool_limit`, `timeout` |
| `keep_assistant_turn_lock_conflicts_total` | a message while the conversation's turn runs, or seems to |
| `keep_assistant_rate_limited_total` | |
| `keep_assistant_gateway_requests_total` | by `endpoint` and `status_class`, also `timeout`, `not_sent`, `dropped`, `cancelled` |
| `keep_assistant_drafts_total` | by `event`: `created`, `confirmed`, `discarded`, `expired`, `finalized_after_timeout` |
| `keep_assistant_draft_outcomes_total` | by `outcome`, the final ones |
| `keep_assistant_drafts_stale_applying` | drafts whose result nobody knows, 5 minutes to 24 hours old |
| `keep_assistant_retention_last_success_unixtime` | |
| `keep_assistant_retention_failures_total` | |
| `keep_assistant_retention_lock_skipped_total` | normal with several workers |
| `keep_assistant_internal_errors_total` | by `site`, every failure without a metric of its own |

With the switch off none of them exist.

A high share of `cancelled` right after the rollout usually means that a proxy buffers
the stream: users see nothing and close the panel.
