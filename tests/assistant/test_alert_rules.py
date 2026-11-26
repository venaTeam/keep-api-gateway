"""The rules and the deploy document name what the code has."""
import dataclasses
import re
from pathlib import Path
from typing import get_args

import pytest
import yaml
from prometheus_client import Counter, Gauge, Histogram

from src.services.assistant.config import ENABLED_ENV_VAR, Settings
from src.services.assistant.core import metrics
from src.services.assistant.llm.client import LlmErrorKind
from src.services.assistant.models.api.draft import OutcomeCode

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs" / "assistant"
GROUPS = yaml.safe_load((DOCS / "alerts.yml").read_text(encoding="utf-8"))["groups"]
RULES = {rule["alert"]: rule for group in GROUPS for rule in group["rules"]}
DEPLOY = (DOCS / "deploy.md").read_text(encoding="utf-8")

METRICS = {
    metric._name: metric
    for metric in vars(metrics).values()
    if isinstance(metric, (Counter, Gauge, Histogram))
}

_NAME = re.compile(r"\bkeep_assistant_\w+")
_SELECTOR = re.compile(r"(keep_assistant_\w+)\{([^}]*)\}")
_MATCHER = re.compile(r'(\w+)\s*(=~|=)\s*"([^"]*)"')
_GROUPING = re.compile(r"\bby \(([^)]*)\)")


def metric_of(exposed: str):
    """What exposes a series of this name; None when nothing does."""
    if isinstance(METRICS.get(exposed), Gauge):
        return METRICS[exposed]
    base, _, suffix = exposed.rpartition("_")
    kind = {"total": Counter, "bucket": Histogram, "sum": Histogram, "count": Histogram}
    metric = METRICS.get(base)
    return metric if isinstance(metric, kind.get(suffix, ())) else None


def values_in_source(label: str) -> set[str]:
    """String constants given for a label in the package."""
    package = ROOT / "src" / "services" / "assistant"
    return {
        value
        for path in package.rglob("*.py")
        for value in re.findall(rf'\b{label}="(\w+)"', path.read_text(encoding="utf-8"))
    }


def possible(metric, label: str) -> set[str]:
    name = metric._name.removeprefix("keep_assistant_")
    if (name, label) == ("llm_errors", "kind"):
        return set(get_args(LlmErrorKind))
    if (name, label) == ("draft_outcomes", "outcome"):
        return set(get_args(OutcomeCode))
    if (name, label) == ("turns", "result"):
        return values_in_source("result") | {"ok", "error", "cancelled", "timeout"}
    if (name, label) == ("gateway_requests", "status_class"):
        classes = {f"{n}xx" for n in (2, 3, 4, 5)}
        return classes | {"timeout", "not_sent", "dropped", "cancelled"}
    if (name, label) in (("drafts", "event"), ("internal_errors", "site")):
        return values_in_source(label)
    raise AssertionError(f"unexpected matcher on {name}.{label}")


# --- the rules --------------------------------------------------------------------------


def test_names_are_unique():
    assert len(RULES) == sum(len(group["rules"]) for group in GROUPS)
    assert len({group["name"] for group in GROUPS}) == len(GROUPS)


@pytest.mark.parametrize("name", RULES)
def test_a_rule_is_complete(name):
    rule = RULES[name]
    assert set(rule) <= {"alert", "expr", "for", "labels", "annotations"}
    assert name.startswith("KeepAssistant")
    assert rule["labels"]["severity"] in ("info", "warning")
    assert rule["annotations"]["summary"] and rule["annotations"]["description"]
    assert re.fullmatch(r"\d+[mh]", rule.get("for", "1m"))
    assert rule["expr"].count("(") == rule["expr"].count(")")


@pytest.mark.parametrize("name", RULES)
def test_a_rule_is_about_metrics_that_exist(name):
    expr = RULES[name]["expr"]
    names = set(_NAME.findall(expr))
    assert names
    assert [n for n in names if metric_of(n) is None] == []
    # A counter only means something as an increase.
    for exposed in names:
        if isinstance(metric_of(exposed), Counter):
            uses = expr.count(exposed)
            assert len(re.findall(rf"increase\({exposed}\b", expr)) == uses


@pytest.mark.parametrize("name", RULES)
def test_a_rule_uses_labels_and_values_that_exist(name):
    expr = RULES[name]["expr"]
    for exposed, matchers in _SELECTOR.findall(expr):
        metric = metric_of(exposed)
        found = _MATCHER.findall(matchers)
        assert len(found) == len(matchers.split(","))
        for label, _, wanted in found:
            assert label in metric._labelnames
            assert set(wanted.split("|")) <= possible(metric, label), label
    labels = {
        label.strip() for group in _GROUPING.findall(expr) for label in group.split(",")
    }
    for exposed in _NAME.findall(expr):
        assert labels <= set(metric_of(exposed)._labelnames)


@pytest.mark.parametrize("name", RULES)
def test_a_text_names_only_labels_the_result_has(name):
    rule = RULES[name]
    text = " ".join(rule["annotations"].values())
    used = set(re.findall(r"\$labels\.(\w+)", text))
    kept = {
        label.strip()
        for group in _GROUPING.findall(rule["expr"])
        for label in group.split(",")
    }
    assert used <= kept


@pytest.mark.parametrize(
    "metric",
    [
        "llm_configured",
        "llm_errors_total",
        "llm_tokens_total",
        "turns_total",
        "turn_lock_conflicts_total",
        "gateway_requests_total",
        "drafts_stale_applying",
        "drafts_total",
        "draft_outcomes_total",
        "retention_last_success_unixtime",
        "retention_failures_total",
        "internal_errors_total",
    ],
)
def test_what_needs_somebody_has_a_rule(metric):
    assert any(f"keep_assistant_{metric}" in rule["expr"] for rule in RULES.values())


@pytest.mark.parametrize("kind", get_args(LlmErrorKind))
def test_every_kind_of_llm_error_has_a_rule(kind):
    exprs = [r["expr"] for r in RULES.values() if "llm_errors_total" in r["expr"]]
    assert any(re.search(rf'["|]{kind}["|]', expr) for expr in exprs)


# --- the deploy document ----------------------------------------------------------------


def documented() -> dict[str, str]:
    """Environment variables and the default next to each."""
    rows = re.findall(r"^\| `([A-Z][A-Z_]+)` \| ([^|]*?) \|", DEPLOY, re.M)
    assert len(rows) == len(dict(rows))
    return dict(rows)


def test_every_setting_is_documented_and_nothing_else():
    names = {setting.metadata["env"] for setting in dataclasses.fields(Settings)}
    assert set(documented()) == names | {ENABLED_ENV_VAR}


def test_the_defaults_are_the_real_ones(set_env):
    settings = Settings()  # every name is set to "" in tests: the defaults
    for setting in dataclasses.fields(Settings):
        env = setting.metadata["env"]
        if env in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            continue
        default = getattr(settings, setting.name)
        text = documented()[env].strip("`")
        if isinstance(default, bool):
            assert text == str(default).lower(), env
        elif isinstance(default, (int, float)):
            assert float(text) == default, env
        else:
            assert text == default, env


def test_the_document_names_metrics_that_exist():
    names = set(_NAME.findall(DEPLOY))
    assert names and [n for n in names if metric_of(n) is None] == []


def test_the_document_points_to_files_that_exist():
    for path in re.findall(r"`(src/[\w./]+)`", DEPLOY):
        assert (ROOT / path).exists(), path
    for target in re.findall(r"\]\(([\w.]+)\)", DEPLOY):
        assert (DOCS / target).exists(), target


def test_the_order_of_the_rollout():
    """Tables first, the UI's switch last; and the other way round, without the
    migration."""
    rollout = DEPLOY.split("## Rollout")[1].split("## Rollback")[0]
    steps = re.findall(r"^\d\. (.*)", rollout, re.M)
    assert [step.split("**")[1] for step in steps[:3]] == [
        "keep-migrations",
        "keep-api-gateway",
        "keep-ui",
    ]
    assert "gateway's" in steps[3] and "keep-ui's" in steps[4]
    rollback = DEPLOY.split("## Rollback")[1].split("## Environment")[0]
    first, second = re.findall(r"^\d\. (.*)", rollback, re.M)
    assert first.startswith("keep-ui's switch off")
    assert second.startswith("The gateway's switch off")
    assert "Never downgrade the migration" in rollback
