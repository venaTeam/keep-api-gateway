"""Prometheus collectors. Label values are closed sets except tenant_id and model.

Served by the gateway's own /metrics.
"""
from src.repositories.prometheus_multiproc import Counter, Gauge, Histogram

_PREFIX = "keep_assistant_"
_TURN_BUCKETS = (1, 2, 5, 10, 20, 30, 45, 60, 90, 120)
_FAST_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20)

# Chat turns. result: ok | error | cancelled | tool_limit | timeout
turns_total = Counter(f"{_PREFIX}turns_total", "Finished chat turns", ["result"])
turn_duration_seconds = Histogram(
    f"{_PREFIX}turn_duration_seconds", "Chat turn duration", buckets=_TURN_BUCKETS
)
# Rounds of tool calls in a turn: how close turns come to ASSISTANT_MAX_TOOL_ROUNDS.
turn_tool_rounds = Histogram(
    f"{_PREFIX}turn_tool_rounds",
    "Rounds of tool calls in a chat turn",
    buckets=(0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16),
)
turn_lock_conflicts_total = Counter(
    f"{_PREFIX}turn_lock_conflicts_total", "Turns refused: one is already running"
)
rate_limited_total = Counter(
    f"{_PREFIX}rate_limited_total", "Messages refused by the rate limit"
)

# LLM. error kind: unavailable | rate_limited | timeout | rejected | misconfigured
llm_first_token_seconds = Histogram(
    f"{_PREFIX}llm_first_token_seconds",
    "Time to the first streamed LLM event",
    buckets=_FAST_BUCKETS,
)
llm_tokens_total = Counter(
    f"{_PREFIX}llm_tokens_total", "LLM tokens used", ["tenant_id", "model", "type"]
)
llm_errors_total = Counter(f"{_PREFIX}llm_errors_total", "LLM call failures", ["kind"])
# The least configured worker wins: one worker without a key is a problem.
llm_configured = Gauge(
    f"{_PREFIX}llm_configured",
    "1 when the LLM client can be built",
    multiprocess_mode="livemin",
)

# Tools and drafts. event: created | confirmed | discarded | opened_in_form (put
# away to finish in keep-ui's form) | expired | finalized_after_timeout | unsupported
# (a Confirm refused: the kind is switched off, or unknown here, as kind "unknown")
tool_calls_total = Counter(
    f"{_PREFIX}tool_calls_total", "Tool calls by outcome", ["tool", "outcome"]
)
drafts_total = Counter(f"{_PREFIX}drafts_total", "Draft events", ["kind", "event"])
draft_outcomes_total = Counter(
    f"{_PREFIX}draft_outcomes_total", "Final draft outcomes", ["kind", "outcome"]
)
# A write Confirm did not send: too little of its time was left to tell the result.
# step: the write's index in the plan, from "0"; at "0" the draft went back to pending.
confirm_budget_refusals_total = Counter(
    f"{_PREFIX}confirm_budget_refusals_total",
    "Confirm writes not sent for lack of time",
    ["kind", "step"],
)
# 0 for a draft kind switched off (kinds/registry.py). The least willing worker wins.
kind_enabled = Gauge(
    f"{_PREFIX}kind_enabled",
    "1 when a draft kind can be drafted and confirmed",
    ["kind"],
    multiprocess_mode="livemin",
)
drafts_stale_applying = Gauge(
    f"{_PREFIX}drafts_stale_applying",
    "Drafts stuck in applying for more than 5 minutes",
    multiprocess_mode="livemax",
)

# Gateway. A status_class is the answer's class, or how the call ended without one.
STATUS_CLASSES = (
    "2xx",
    "3xx",
    "4xx",
    "5xx",
    "timeout",
    "not_sent",
    "dropped",
    "cancelled",
)
gateway_requests_total = Counter(
    f"{_PREFIX}gateway_requests_total",
    "Gateway requests",
    ["endpoint", "status_class"],
)
gateway_request_seconds = Histogram(
    f"{_PREFIX}gateway_request_seconds",
    "Gateway request duration",
    ["endpoint"],
    buckets=_FAST_BUCKETS,
)

# Retention
retention_rows_deleted_total = Counter(
    f"{_PREFIX}retention_rows_deleted_total", "Rows deleted by retention", ["table"]
)
retention_failures_total = Counter(
    f"{_PREFIX}retention_failures_total", "Failed retention runs"
)
retention_lock_skipped_total = Counter(
    f"{_PREFIX}retention_lock_skipped_total",
    "Retention runs ended because another worker holds the lock",
)
# `max`, not a live mode: the success of a worker that has since died counts.
retention_last_success_unixtime = Gauge(
    f"{_PREFIX}retention_last_success_unixtime",
    "Unix time of the last successful retention run",
    multiprocess_mode="max",
)

# Every failure path that has no metric of its own.
internal_errors_total = Counter(
    f"{_PREFIX}internal_errors_total", "Internal errors by code site", ["site"]
)
