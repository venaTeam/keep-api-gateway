"""The current state of alerts the model names by fingerprint. No filter budget: it
reads them as a draft's selection does (one query, then single reads)."""
from typing import Any, List

from pydantic import BaseModel, conlist, constr, validator

from src.services.assistant.bl.projection import clean_text, project_alert
from src.services.assistant.models.api.chat import (
    MAX_FINGERPRINT_CHARS,
    has_control_characters,
    storable,
)
from src.services.assistant.models.api.draft import MAX_ALERTS_PER_DRAFT
from src.services.assistant.tools.base import (
    READ_FAILURES,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)
from src.services.assistant.tools.read_common import parse, read_failure

NAME = "get_alerts"
MAX_FINGERPRINTS = MAX_ALERTS_PER_DRAFT

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fingerprints"],
    "properties": {
        "fingerprints": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                f"Fingerprints of the alerts, exactly as given to you. 1 to "
                f"{MAX_FINGERPRINTS}."
            ),
        },
    },
}

PROMPT_GUIDANCE = """\
get_alerts:
- The current state of alerts whose fingerprints you were given or found. Never \
invent a fingerprint."""


class GetAlertsArgs(BaseModel):
    fingerprints: conlist(
        constr(min_length=1, max_length=MAX_FINGERPRINT_CHARS),
        min_items=1,
        max_items=MAX_FINGERPRINTS,
    )

    class Config:
        extra = "forbid"

    @validator("fingerprints")
    def _clean_and_distinct(cls, value: List[str]):
        for fingerprint in value:
            if has_control_characters(fingerprint):
                raise ValueError("a fingerprint has no control characters")
            storable(fingerprint)
        return list(dict.fromkeys(value))


def _shown(fingerprints: list[str]) -> list[str]:
    # The model's own words.
    return [clean_text(fp, MAX_FINGERPRINT_CHARS) for fp in fingerprints]


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(GetAlertsArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    try:
        fetched = await context.gateway.fetch_alerts_by_fingerprints(args.fingerprints)
    except READ_FAILURES as exc:
        return read_failure(exc)
    alerts = [project_alert(alert) for alert in fetched.found.values()]
    result = {
        "status": "found" if alerts else "not_found",
        "alerts": alerts,
        "not_found": _shown(fetched.not_found),
        "unresolvable": _shown(fetched.unresolvable),
    }
    return ToolOutcome(result["status"], result)


SPEC = ToolSpec(
    name=NAME,
    description=(
        "Read alerts by their fingerprints: name, status, severity, source, service "
        "and times of each. Changes nothing."
    ),
    parameters=PARAMETERS,
    label="Reading alerts",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
)
