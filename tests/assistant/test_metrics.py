import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.multiprocess import MultiProcessCollector

from src.services.assistant import lifecycle
from src.services.assistant.core import metrics
from src.services.assistant.gateway import client as client_module
from src.services.assistant.gateway.client import WRITE_ENDPOINTS, WRITE_ROUTES
from src.services.assistant.kinds import alert_status
from src.services.assistant.kinds.registry import KINDS
from src.services.assistant.models.api.draft import OutcomeCode
from src.services.assistant.models.api.stream import ToolOutcomeCode
from src.services.assistant.tools.registry import TOOLS

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
        "confirm_budget_refusals_total",
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


def test_rare_series_exist_from_the_start():
    """Prometheus sees no increase in a series that begins with its first event, and
    some of these happen once in months."""
    # A cleared series that begins again keeps its value in multi-process mode, so only
    # its presence says something.
    for collector in (
        metrics.draft_outcomes_total,
        metrics.internal_errors_total,
        metrics.gateway_requests_total,
        metrics.tool_calls_total,
        metrics.confirm_budget_refusals_total,
    ):
        collector.clear()
    lifecycle._start_series()
    text = generate_latest(REGISTRY).decode()
    assert WRITE_ENDPOINTS == tuple(WRITE_ROUTES)
    expected = [
        'internal_errors_total{site="sweep_close"}',
        *(f'confirm_budget_refusals_total{{kind="{kind}",step="0"}}' for kind in KINDS),
        *(
            f'draft_outcomes_total{{kind="{kind}",outcome="{outcome}"}}'
            for kind in KINDS
            for outcome in get_args(OutcomeCode)
        ),
        *(
            f'gateway_requests_total{{endpoint="{endpoint}",status_class="{status}"}}'
            for endpoint in WRITE_ROUTES
            for status in metrics.STATUS_CLASSES
        ),
        *(
            f'tool_calls_total{{outcome="{outcome}",tool="{tool}"}}'
            for tool in TOOLS
            for outcome in get_args(ToolOutcomeCode)
        ),
    ]
    for series in expected:
        assert f"keep_assistant_{series} " in text, series


def test_a_kind_with_a_form_counts_opened_in_form_from_the_start(monkeypatch):
    metrics.drafts_total.clear()
    monkeypatch.setattr(alert_status.AlertStatusKind, "can_open_in_form", True)
    lifecycle._start_series()
    text = generate_latest(REGISTRY).decode()
    series = 'drafts_total{event="opened_in_form",kind="change_alert_status"}'
    assert f"keep_assistant_{series} " in text


def test_every_status_class_the_client_counts_is_known():
    tree = ast.parse(Path(client_module.__file__).read_text("utf-8"))
    literals = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and [getattr(t, "id", None) for t in node.targets] == ["status_class"]
        and isinstance(node.value, ast.Constant)
    } | {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "status_class"
        and isinstance(node.value, ast.Constant)
    }
    assert literals
    computed = {f"{n}xx" for n in (2, 3, 4, 5)}
    assert literals | computed == set(metrics.STATUS_CLASSES)
