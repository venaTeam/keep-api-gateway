"""How many alerts a filter matches. One unit of the turn's filter budget; the same
filter again in the turn costs nothing."""
from typing import Any

from src.services.assistant.tools.base import (
    READ_FAILURES,
    FilterBudgetSpent,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from src.services.assistant.tools.read_common import (
    CelArgs,
    budget_spent,
    parse,
    read_failure,
)

NAME = "count_alerts"

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cel"],
    "properties": {
        "cel": {
            "type": "string",
            "description": 'A CEL filter on alerts. "" counts every alert.',
        },
    },
}

PROMPT_GUIDANCE = """\
count_alerts:
- The exact number of alerts a CEL filter matches. Use it for "how many" questions \
instead of searching. A count of null means the gateway could not tell."""


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(CelArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        count = await context.read_once(
            context.gateway.count_matching, args.cel, filter_units=1
        )
    except FilterBudgetSpent:
        return budget_spent()
    except READ_FAILURES as exc:
        return read_failure(exc)
    return ToolOutcome("found", {"status": "found", "count": count})


SPEC = ToolSpec(
    name=NAME,
    description="Count the alerts that match a CEL filter. Changes nothing.",
    parameters=PARAMETERS,
    label="Counting alerts",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
