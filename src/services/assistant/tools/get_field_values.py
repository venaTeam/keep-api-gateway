"""Which values one alert field takes, with how many alerts have each. One unit of the
turn's filter budget.

A field the alerts table has a facet for is counted by the gateway over every
matching alert. Any other field is counted here over a sample of the newest matches.
Only fields the context message names, and never one that holds people (an
assignee, a note, who did something, an email). A value that holds an email address
is left out whatever its field is called.
"""
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any, Optional

from pydantic import BaseModel, constr, validator

from src.services.assistant.bl.context import (
    available_fields,
    drop_facet_ids,
    facet_ids,
)
from src.services.assistant.bl.projection import MAX_LABEL_CHARS, clean_text
from src.services.assistant.gateway.errors import GatewayUnavailable, InvalidCel
from src.services.assistant.models.api.chat import storable
from src.services.assistant.tools.base import (
    READ_FAILURES,
    FilterBudgetSpent,
    ToolContext,
    ToolOutcome,
    ToolSpec,
    tool_error,
)
from src.services.assistant.tools.read_common import (
    MAX_CEL_CHARS,
    budget_spent,
    parse,
    read_failure,
)

NAME = "get_field_values"
MAX_VALUES = 50
# The newest matches a field without a facet is counted over.
SAMPLE_SIZE = 200
MAX_FIELD_CHARS = 100

# Fields that hold people, by any segment of their dotted name.
_PEOPLE_SEGMENTS = frozenset({"assignee", "note"})
# Anything shaped like an address, in a field name or a value.
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def holds_email(text: str) -> bool:
    return _EMAIL.search(text) is not None


def is_denied(field: str) -> bool:
    """A field that holds people: `assignee`, `note`, `*_by`, `*By`, `*email*`,
    by any segment of its dotted name, or a name that holds an address."""
    if holds_email(field):
        return True
    for segment in field.split("."):
        lower = segment.lower()
        if (
            lower in _PEOPLE_SEGMENTS
            or lower.endswith("_by")
            or segment.endswith("By")
            or "email" in lower
        ):
            return True
    return False


PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["field", "cel"],
    "properties": {
        "field": {
            "type": "string",
            "description": "An alert field, one of the available_fields you were given.",
        },
        "cel": {
            "type": ["string", "null"],
            "description": (
                "A CEL filter: only the alerts it matches are counted. Null: all "
                "alerts."
            ),
        },
    },
}

PROMPT_GUIDANCE = f"""\
get_field_values:
- The values an alert field takes and how many alerts have each, at most \
{MAX_VALUES}. Use it to write a filter with values that exist. When `sampled` is true \
the counts are of the newest {SAMPLE_SIZE} matching alerts only. Fields that name \
people are refused, and values that hold an email address are left out."""


class GetFieldValuesArgs(BaseModel):
    field: constr(strip_whitespace=True, min_length=1, max_length=MAX_FIELD_CHARS)
    # None: every alert, which the gateway calls "".
    cel: Optional[constr(strip_whitespace=True, max_length=MAX_CEL_CHARS)] = None

    class Config:
        extra = "forbid"

    @validator("field")
    def _storable_field(cls, value):
        return storable(value)

    @validator("cel", always=True)
    def _every_alert_or_storable(cls, value):
        return storable(value) if value else ""


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(GetFieldValuesArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    if is_denied(args.field):
        return tool_error("field_not_allowed")
    tenant_id = context.identity.tenant_id
    reader = context.gateway
    try:
        if args.field not in await available_fields(reader, tenant_id):
            return tool_error("unknown_field")
        facet_id = (await facet_ids(reader, tenant_id)).get(args.field)
        if facet_id is not None:
            return await _counted(context, args, facet_id)
        return await _sampled(context, args)
    except FilterBudgetSpent:
        return budget_spent()
    except READ_FAILURES as exc:
        return read_failure(exc)


async def _counted(
    context: ToolContext, args: GetFieldValuesArgs, facet_id: str
) -> ToolOutcome:
    try:
        options = await context.read_once(
            context.gateway.facet_options, args.cel, facet_id, filter_units=1
        )
    except (InvalidCel, GatewayUnavailable) as exc:
        # A facet that is gone answers like this too: read them again next time.
        if isinstance(exc, InvalidCel) or exc.reason in ("status_400", "status_404"):
            drop_facet_ids(context.identity.tenant_id)
        raise
    counts: Counter[str] = Counter()
    for option in options:
        value, count = option.get("value"), option.get("matches_count")
        if _is_count(count) and count > 0 and _is_value(value):
            counts[_shown(value)] += count
    return _found(args.field, counts, sampled=False)


async def _sampled(context: ToolContext, args: GetFieldValuesArgs) -> ToolOutcome:
    alerts = await context.read_once(
        context.gateway.query_alerts, args.cel, SAMPLE_SIZE, filter_units=1
    )
    counts: Counter[str] = Counter()
    for alert in alerts:
        for value in set(_values(alert.data, args.field)):
            counts[value] += 1
    return _found(
        args.field,
        counts,
        sampled=len(alerts) >= SAMPLE_SIZE,
        sample_size=len(alerts),
    )


def _found(
    field: str,
    counts: Counter[str],
    *,
    sampled: bool,
    sample_size: int | None = None,
) -> ToolOutcome:
    result: dict[str, Any] = {
        "status": "found",
        "field": clean_text(field, MAX_FIELD_CHARS),
        "values": [
            {"value": value, "count": count}
            for value, count in counts.most_common(MAX_VALUES)
        ],
        "sampled": sampled,
    }
    if sample_size is not None:
        result["sample_size"] = sample_size
    return ToolOutcome("found", result)


def _values(data: dict[str, Any], field: str) -> Iterator[str]:
    """The field's values in one alert, by its dotted name: a list gives each item."""
    value: Any = data
    for segment in field.split("."):
        if not isinstance(value, dict) or segment not in value:
            return
        value = value[segment]
    for item in value if isinstance(value, list) else [value]:
        if _is_value(item):
            yield _shown(item)


def _is_value(value: Any) -> bool:
    if isinstance(value, str):
        return value != "" and not holds_email(value)
    return isinstance(value, (int, float, bool))


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _shown(value: Any) -> str:
    # Alert text: cleaned as everywhere. JSON's own spelling for the others.
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return clean_text(text, MAX_LABEL_CHARS)


SPEC = ToolSpec(
    name=NAME,
    description=(
        "List the values one alert field takes, with how many alerts have each. "
        "Changes nothing."
    ),
    parameters=PARAMETERS,
    label="Reading field values",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
