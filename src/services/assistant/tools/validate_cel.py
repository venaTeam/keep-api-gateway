"""Whether the gateway accepts a filter, before it is used. No filter budget: the
gateway only parses it."""
from typing import Any, Literal

from pydantic import constr

from src.services.assistant.tools.base import (
    READ_FAILURES,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from src.services.assistant.tools.read_common import (
    MAX_CEL_CHARS,
    CelArgs,
    diagnostics,
    parse,
    read_failure,
)

NAME = "validate_cel"
CONTEXTS = ("alerts", "rules")

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cel", "context"],
    "properties": {
        "cel": {
            "type": "string",
            "description": f"The CEL filter to check, at most {MAX_CEL_CHARS} chars.",
        },
        "context": {
            "type": "string",
            "enum": list(CONTEXTS),
            "description": (
                "Where it will be used: alerts (searches, presets, drafts) or rules "
                "(correlation rules)."
            ),
        },
    },
}

PROMPT_GUIDANCE = """\
validate_cel:
- Check a CEL filter you wrote before you use it, when you are not sure of it. If it \
is not valid, fix it with the diagnostics, or ask the user."""


class ValidateCelArgs(CelArgs):
    cel: constr(strip_whitespace=True, min_length=1, max_length=MAX_CEL_CHARS)
    context: Literal[CONTEXTS]  # type: ignore[valid-type]


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(ValidateCelArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        answer = await context.read_once(
            context.gateway.check_cel, args.cel, args.context
        )
    except READ_FAILURES as exc:
        return read_failure(exc)
    valid = answer["valid"]
    result = {
        "status": "found" if valid else "invalid_cel",
        "valid": valid,
        "diagnostics": diagnostics(answer.get("diagnostics")),
    }
    return ToolOutcome(result["status"], result)


SPEC = ToolSpec(
    name=NAME,
    description="Check whether a CEL filter is valid. Changes nothing.",
    parameters=PARAMETERS,
    label="Checking a filter",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
