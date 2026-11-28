"""What the read tools share: parsing their arguments, their CEL argument, and how a
failed read is told.

A read tool proposes nothing and writes nothing: it sets neither `draft_kind` nor
`makes_link`. Its result reaches the model through `encode_result`, which keeps a
filter's angle brackets readable: what a tool shows of a filter goes through
`readable_text`, and alert text through `clean_text` as everywhere.
"""
import json
from typing import Any, Optional, TypeVar

from pydantic import BaseModel, ValidationError, constr, validator

from src.services.assistant.bl.projection import clean_text, readable_text
from src.services.assistant.gateway.errors import InvalidCel
from src.services.assistant.models.api.chat import storable
from src.services.assistant.tools.base import (
    MAX_DIAGNOSTICS_SHOWN,
    ToolOutcome,
    read_failure_outcome,
    tool_error,
)

MAX_CEL_CHARS = 2000
MAX_ERRORS_SHOWN = 10
# What a tool shows of a diagnostic of a filter.
MAX_DIAGNOSTIC_CHARS = 300

Args = TypeVar("Args", bound=BaseModel)


class CelArgs(BaseModel):
    """Arguments with a filter the model wrote: "" is every alert."""

    cel: constr(strip_whitespace=True, max_length=MAX_CEL_CHARS)

    class Config:
        extra = "forbid"

    @validator("cel")
    def _storable(cls, value):
        return storable(value)


def invalid_arguments(errors: list[dict[str, str]]) -> ToolOutcome:
    return ToolOutcome(
        "invalid_arguments", {"status": "invalid_arguments", "errors": errors}
    )


def parse(model: type[Args], arguments: str) -> Args | ToolOutcome:
    """The model's arguments, or the outcome that tells it what is wrong."""
    try:
        raw = json.loads(arguments)
    except (ValueError, RecursionError):
        return invalid_arguments([{"field": "arguments", "message": "not valid JSON"}])
    try:
        return model.parse_obj(raw)
    except ValidationError as exc:
        return invalid_arguments(
            [
                {
                    "field": clean_text(".".join(map(str, e["loc"])), 100),
                    "message": clean_text(e["msg"], 200),
                }
                for e in exc.errors()[:MAX_ERRORS_SHOWN]
            ]
        )


def budget_spent() -> ToolOutcome:
    """The turn's filter budget is spent: nothing was sent."""
    return tool_error("cel_lookup_limit")


def diagnostics(items: Any) -> list[str]:
    """The gateway's diagnostics of a filter, readable: at most
    MAX_DIAGNOSTICS_SHOWN."""
    if not isinstance(items, list):
        return []
    return [
        readable_text(str(item.get("message", "")), MAX_DIAGNOSTIC_CHARS)
        for item in items
        if isinstance(item, dict)
    ][:MAX_DIAGNOSTICS_SHOWN]


# --- the lists of the tenant's objects ----------------------------------------------

# Rows of a list a tool shows; `total` and `more` say how many there are.
MAX_LIST_ROWS = 20
# Of a filter a row shows.
MAX_LISTED_CEL_CHARS = 200
# Of a name: as long as a link may take one, so that the model can name it again.
MAX_LISTED_NAME_CHARS = 256
MAX_NAME_FILTER_CHARS = 256

NAME_CONTAINS_PARAMETER: dict[str, Any] = {
    "type": ["string", "null"],
    "description": "Only those whose name contains this text, in any case. Null: all.",
}


class NameFilterArgs(BaseModel):
    """A list tool's one argument: part of a name, or None for every one."""

    name_contains: Optional[
        constr(strip_whitespace=True, max_length=MAX_NAME_FILTER_CHARS)
    ] = None

    class Config:
        extra = "forbid"

    @validator("name_contains", always=True)
    def _all_or_storable(cls, value):
        return storable(value) if value else None


def listed_name(value: Any) -> str | None:
    """A name a row shows: readable, as the tenant wrote it. Its angle brackets travel
    as JSON escapes, like a filter's."""
    return readable_text(value, MAX_LISTED_NAME_CHARS) if isinstance(value, str) else None


def listed_cel(value: Any) -> str | None:
    return readable_text(value, MAX_LISTED_CEL_CHARS) if isinstance(value, str) else None


def listed(
    rows: list[dict[str, Any]], name_contains: str | None, name: str = "name"
) -> ToolOutcome:
    """`{status:"found", rows, total, more}` of the rows whose `name` contains the
    text, by name: at most MAX_LIST_ROWS, never a bare list."""
    wanted = name_contains.casefold() if name_contains else None
    matching = [
        row
        for row in rows
        if wanted is None or wanted in (row.get(name) or "").casefold()
    ]
    matching.sort(key=lambda row: ((row.get(name) or "").casefold(), str(row.get("id"))))
    shown = matching[:MAX_LIST_ROWS]
    return ToolOutcome(
        "found",
        {
            "status": "found",
            "rows": shown,
            "total": len(matching),
            "more": len(matching) > len(shown),
        },
    )


def read_failure(exc: Exception) -> ToolOutcome:
    """`read_failure_outcome`, with an invalid filter's diagnostics readable."""
    if isinstance(exc, InvalidCel):
        return ToolOutcome(
            "invalid_cel",
            {"status": "invalid_cel", "diagnostics": diagnostics(exc.diagnostics)},
        )
    return read_failure_outcome(exc)
