"""What the read tools share: parsing their arguments, their CEL argument, and how a
failed read is told.

A read tool proposes nothing and writes nothing: it sets neither `draft_kind` nor
`makes_link`. Its result reaches the model through `encode_result`, which keeps a
filter's angle brackets readable: what a tool shows of a filter goes through
`readable_text`, and alert text through `clean_text` as everywhere.
"""
import json
from typing import Any, TypeVar

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


def read_failure(exc: Exception) -> ToolOutcome:
    """`read_failure_outcome`, with an invalid filter's diagnostics readable."""
    if isinstance(exc, InvalidCel):
        return ToolOutcome(
            "invalid_cel",
            {"status": "invalid_cel", "diagnostics": diagnostics(exc.diagnostics)},
        )
    return read_failure_outcome(exc)
