"""The tenant's correlation rules: how each groups alerts into incidents. One unit of
the turn's filter budget (the gateway counts each rule's incidents), once per turn:
the same read again costs nothing. Who made or changed a rule, and its assignee, are
never shown."""
from typing import Any

from src.services.assistant.tools.base import (
    READ_FAILURES,
    FilterBudgetSpent,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from src.services.assistant.tools.read_common import (
    NAME_CONTAINS_PARAMETER,
    NameFilterArgs,
    budget_spent,
    listed,
    listed_cel,
    listed_name,
    parse,
    read_failure,
)

NAME = "list_correlation_rules"
MAX_GROUPING_SHOWN = 10
MAX_GROUPING_CHARS = 100

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name_contains"],
    "properties": {"name_contains": NAME_CONTAINS_PARAMETER},
}

PROMPT_GUIDANCE = """\
list_correlation_rules:
- The correlation rules that group alerts into incidents, with each one's filter, \
grouping and how many incidents it made. The error rules_unreadable means one of the \
tenant's rules has a filter Keep cannot read: the user can fix it on the Correlations \
page."""


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return listed_name(value)


def _grouping(value: Any) -> list[str]:
    items = value if isinstance(value, list) else ()
    return [
        listed_cel(item)[:MAX_GROUPING_CHARS] for item in items if isinstance(item, str)
    ][:MAX_GROUPING_SHOWN]


def row(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fields.get("id"),
        "name": listed_name(fields.get("name")),
        "cel": listed_cel(fields.get("definition_cel")),
        # The gateway keeps the time frame in seconds, whatever unit the form showed.
        "timeframe_seconds": _number(fields.get("timeframe")),
        "grouping_criteria": _grouping(fields.get("grouping_criteria")),
        "create_on": _text(fields.get("create_on")),
        "resolve_on": _text(fields.get("resolve_on")),
        "require_approve": fields.get("require_approve") is True,
        "threshold": _number(fields.get("threshold")),
        "incident_name_template": _text(fields.get("incident_name_template")),
        "incident_prefix": _text(fields.get("incident_prefix")),
        "incidents": _number(fields.get("incidents")),
    }


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(NameFilterArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        rules = await context.read_once(context.gateway.rules, filter_units=1)
    except FilterBudgetSpent:
        return budget_spent()
    except READ_FAILURES as exc:
        # One unparseable rule: tool error rules_unreadable.
        return read_failure(exc)
    return listed([row(rule.fields) for rule in rules], args.name_contains)


SPEC = ToolSpec(
    name=NAME,
    description=(
        "List the correlation rules that group alerts into incidents. Changes nothing."
    ),
    parameters=PARAMETERS,
    label="Reading correlation rules",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
