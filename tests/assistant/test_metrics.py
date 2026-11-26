import os
import subprocess
import sys
from pathlib import Path

from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.multiprocess import MultiProcessCollector

from src.services.assistant.core import metrics  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_collector_is_in_the_registry_the_gateway_serves():
    text = generate_latest(REGISTRY).decode()
    for name in (
        "turns_total",
        "turn_duration_seconds",
        "turn_lock_conflicts_total",
        "rate_limited_total",
        "llm_first_token_seconds",
        "llm_tokens_total",
        "llm_errors_total",
        "llm_configured",
        "tool_calls_total",
        "drafts_total",
        "draft_outcomes_total",
        "drafts_stale_applying",
        "gateway_requests_total",
        "gateway_request_seconds",
        "retention_rows_deleted_total",
        "retention_failures_total",
        "retention_lock_skipped_total",
        "retention_last_success_unixtime",
        "internal_errors_total",
    ):
        assert f"# TYPE keep_assistant_{name} " in text, name


def test_metrics_aggregate_over_worker_processes(tmp_path):
    def run(code: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env={**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

    worker = (
        "from src.services.assistant.core import metrics\n"
        "metrics.rate_limited_total.inc()\n"
        "metrics.retention_last_success_unixtime.set({value})\n"
    )
    run(worker.format(value=100))
    run(worker.format(value=200))
    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=str(tmp_path))
    text = generate_latest(registry).decode()
    assert "keep_assistant_rate_limited_total 2.0" in text
    assert "keep_assistant_retention_last_success_unixtime 200.0" in text
