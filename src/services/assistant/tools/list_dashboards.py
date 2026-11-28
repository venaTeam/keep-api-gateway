"""The tenant's dashboards: names and the title and type of each widget. No filter
budget. The presets a widget embeds are not shown, nor who made anything."""
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
    listed_name,
    parse,
    read_failure,
)

NAME = "list_dashboards"
MAX_WIDGETS_SHOWN = 20
MAX_WIDGET_TYPE_CHARS = 40

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name_contains"],
    "properties": {"name_contains": NAME_CONTAINS_PARAMETER},
}

PROMPT_GUIDANCE = """\
list_dashboards:
- The dashboards of the tenant, with the title and type of each widget \
(`widget_count` is how many it has; at most 20 are listed)."""


def _widgets(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    config = dashboard.get("dashboard_config")
    data = config.get("widgetData") if isinstance(config, dict) else None
    return [w for w in (data if isinstance(data, list) else ()) if isinstance(w, dict)]


def _widget_type(widget: dict[str, Any]) -> str | None:
    value = widget.get("widgetType")
    return listed_name(value)[:MAX_WIDGET_TYPE_CHARS] if isinstance(value, str) else None


def row(dashboard: dict[str, Any]) -> dict[str, Any]:
    widgets = _widgets(dashboard)
    return {
        "id": dashboard.get("id"),
        "name": listed_name(dashboard.get("dashboard_name")),
        "widget_count": len(widgets),
        "widgets": [
            {"title": listed_name(w.get("name")), "type": _widget_type(w)}
            for w in widgets[:MAX_WIDGETS_SHOWN]
        ],
    }


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(NameFilterArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        dashboards = await context.read_once(context.gateway.dashboards)
    except READ_FAILURES as exc:
        return read_failure(exc)
    return listed([row(d) for d in dashboards], args.name_contains)


SPEC = ToolSpec(
    name=NAME,
    description="List the dashboards and their widgets. Changes nothing.",
    parameters=PARAMETERS,
    label="Reading dashboards",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
