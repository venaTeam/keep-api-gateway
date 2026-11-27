"""A page of the alerts a filter matches, in the order asked for. One unit of the turn's
filter budget; the same search again in the turn costs nothing."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, StrictInt, validator

from src.services.assistant.bl.projection import project_alert
from src.services.assistant.models.api.draft import MAX_ALERTS_PER_DRAFT
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

NAME = "search_alerts"
MAX_RESULTS = MAX_ALERTS_PER_DRAFT
SORT_FIELDS = ("lastReceived", "severity", "status", "name")
SORT_DIRECTIONS = ("asc", "desc")
# Without a sort: the newest first, as the alerts table shows them.
DEFAULT_SORT = ("lastReceived", "desc")

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cel", "limit", "sort"],
    "properties": {
        "cel": {
            "type": "string",
            "description": 'A CEL filter on alerts. "" matches every alert.',
        },
        "limit": {
            "type": "integer",
            "description": f"How many alerts to return, 1 to {MAX_RESULTS}.",
        },
        "sort": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "dir"],
                    "properties": {
                        "field": {"type": "string", "enum": list(SORT_FIELDS)},
                        "dir": {"type": "string", "enum": list(SORT_DIRECTIONS)},
                    },
                },
                {"type": "null"},
            ],
            "description": "The order. Null: the most recently received first.",
        },
    },
}

PROMPT_GUIDANCE = f"""\
search_alerts:
- Up to {MAX_RESULTS} alerts that match a CEL filter, in the order asked for. `more` \
says whether there are others: count them with count_alerts if the user wants to know \
how many."""


class Sort(BaseModel):
    field: Literal[SORT_FIELDS]  # type: ignore[valid-type]
    dir: Literal[SORT_DIRECTIONS]  # type: ignore[valid-type]

    class Config:
        extra = "forbid"


class SearchAlertsArgs(CelArgs):
    limit: StrictInt
    sort: Optional[Sort] = None

    @validator("limit")
    def _within(cls, value):
        if not 1 <= value <= MAX_RESULTS:
            raise ValueError(f"limit must be 1 to {MAX_RESULTS}")
        return value


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(SearchAlertsArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    field, direction = (args.sort.field, args.sort.dir) if args.sort else DEFAULT_SORT
    try:
        # One more than asked for tells whether there are more.
        alerts = await context.read_once(
            context.gateway.search_alerts,
            args.cel,
            args.limit + 1,
            field,
            direction,
            filter_units=1,
        )
    except FilterBudgetSpent:
        return budget_spent()
    except READ_FAILURES as exc:
        return read_failure(exc)
    shown = [project_alert(alert) for alert in alerts[: args.limit]]
    if not shown:
        return ToolOutcome("no_matches", {"status": "no_matches", "count": 0})
    return ToolOutcome(
        "found",
        {
            "status": "found",
            "count": len(shown),
            "more": len(alerts) > args.limit,
            "alerts": shown,
        },
    )


SPEC = ToolSpec(
    name=NAME,
    description=(
        "Find alerts that match a CEL filter: name, status, severity, source, "
        "service and times of each. Changes nothing."
    ),
    parameters=PARAMETERS,
    label="Searching alerts",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
