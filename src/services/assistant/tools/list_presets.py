"""The tenant's presets the user can see: names, filters and how each shows its alerts.
No filter budget: the gateway reads them from its own table. Who made a preset is never
shown; `mine` says whether the user did."""
from typing import Any

from src.services.assistant.tools.base import (
    READ_FAILURES,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from src.services.assistant.tools.read_common import (
    NAME_CONTAINS_PARAMETER,
    NameFilterArgs,
    listed,
    listed_cel,
    listed_name,
    parse,
    read_failure,
)

NAME = "list_presets"
# Of the tags a row shows.
MAX_TAGS_SHOWN = 10

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name_contains"],
    "properties": {"name_contains": NAME_CONTAINS_PARAMETER},
}

PROMPT_GUIDANCE = """\
list_presets:
- The saved alert views (presets) the user can see, with each one's filter. `mine` \
says whether the user made it; the static one is the feed of every alert."""


def _cel(preset: dict[str, Any]) -> str | None:
    for option in preset.get("options") or ():
        if isinstance(option, dict) and option.get("label") == "CEL":
            return listed_cel(option.get("value"))
    return None


def _tags(preset: dict[str, Any]) -> list[str]:
    tags = preset.get("tags")
    names = [
        listed_name(tag.get("name"))
        for tag in (tags if isinstance(tags, list) else ())
        if isinstance(tag, dict)
    ]
    return [name for name in names if name][:MAX_TAGS_SHOWN]


def row(preset: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": preset.get("id"),
        "name": listed_name(preset.get("name")),
        "cel": _cel(preset),
        "is_private": preset.get("is_private") is True,
        "is_noisy": preset.get("is_noisy") is True,
        "counter_shows_firing_only": preset.get("counter_shows_firing_only") is True,
        "tags": _tags(preset),
        "static": preset.get("static") is True,
        "mine": preset.get("mine") is True,
    }


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(NameFilterArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        presets = await context.read_once(context.gateway.presets)
    except READ_FAILURES as exc:
        return read_failure(exc)
    return listed([row(preset) for preset in presets], args.name_contains)


SPEC = ToolSpec(
    name=NAME,
    description="List the alert presets (saved views) the user can see. Changes nothing.",
    parameters=PARAMETERS,
    label="Reading presets",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
