import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from src.models.cel import CelValidationResult
from src.services.cel_validation import validate_cel

router = APIRouter()
logger = logging.getLogger(__name__)

# The editor is shared between features whose execution engines differ, so a
# check is always made *for a context*. Alert filters are converted to SQL;
# maintenance, extraction, correlation rules and workflow triggers are evaluated
# in-process by celpy over the raw event payload, where any payload key is a
# legitimate field. Declaring it as a Literal means FastAPI rejects an unknown
# value with 422 before the handler runs, so a typo can never quietly fall
# through to another feature's rules.
CelValidationContext = Literal[
    "alerts", "maintenance", "extraction", "rules", "workflows"
]


class CelExpressionPayload(BaseModel):
    cel: str
    context: CelValidationContext


@router.post(
    "/validate",
    description=(
        "Validate a CEL expression for an execution context. Returns HTTP 200 "
        "for a completed check - read the response body for the verdict."
    ),
)
def validate(cel_payload: CelExpressionPayload) -> CelValidationResult:
    return validate_cel(cel_payload.cel, cel_payload.context)
