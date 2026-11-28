"""The tags the tenant has for presets, by name: a preset can only get one of these.
No filter budget. Their ids stay here."""
from typing import Any

from pydantic import BaseModel

from src.services.assistant.tools.base import (
    READ_FAILURES,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from src.services.assistant.tools.read_common import (
    MAX_LIST_ROWS,
    listed_name,
    parse,
    read_failure,
)

NAME = "list_tags"

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {},
}

PROMPT_GUIDANCE = """\
list_tags:
- The names of the tags presets can have. Only these exist: never make one up."""


class ListTagsArgs(BaseModel):
    class Config:
        extra = "forbid"


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(ListTagsArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        tags = await context.read_once(context.gateway.tags)
    except READ_FAILURES as exc:
        return read_failure(exc)
    names = sorted(
        {name for tag in tags if (name := listed_name(tag.get("name")))},
        key=lambda name: (name.casefold(), name),
    )
    shown = names[:MAX_LIST_ROWS]
    return ToolOutcome(
        "found",
        {
            "status": "found",
            "rows": shown,
            "total": len(names),
            "more": len(names) > len(shown),
        },
    )


SPEC = ToolSpec(
    name=NAME,
    description="List the names of the tags presets can have. Changes nothing.",
    parameters=PARAMETERS,
    label="Reading tags",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
