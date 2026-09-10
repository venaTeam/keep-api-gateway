import logging
from typing import Any, Literal, Optional

from celpy import CELParseError
from fastapi import APIRouter
from pydantic import BaseModel

from src.models.cel import CelValidationResult
from src.repositories.cel_to_sql.cel_ast_converter import CelToAstConverter
from src.services.cel_validation import validate_cel

router = APIRouter()
logger = logging.getLogger(__name__)

# The editor is shared between features whose execution engines differ, so a
# check is always made *for a context*. Alert filters run as SQL; maintenance
# conditions are evaluated in-process by the event handler. A new context must
# be added deliberately rather than inheriting alert-query rules.
CelValidationContext = Literal["alerts", "maintenance"]


class CelExpressionPayload(BaseModel):
    cel: str
    # Opt-in: omitting `context` keeps the legacy marker-array response for
    # clients that have not migrated to the structured contract yet.
    context: Optional[CelValidationContext] = None


class CelExpressionValidationMarker(BaseModel):
    columnStart: int
    columnEnd: int


@router.post(
    "/validate",
    description=(
        "Validate a CEL expression. Returns HTTP 200 for a completed check - "
        "read the response body for the verdict. With a `context`, returns the "
        "structured {valid, diagnostics} contract; without one, the legacy "
        "marker array."
    ),
)
def validate(
    cel_payload: CelExpressionPayload,
) -> Any:
    if cel_payload.context is not None:
        return _validate_for_context(cel_payload.cel, cel_payload.context)

    return _validate_legacy(cel_payload.cel)


def _validate_for_context(
    cel: str, context: CelValidationContext
) -> CelValidationResult:
    """Structured contract - shares its rules with the matching execution path.

    FastAPI rejects an unknown context before the handler runs, so every value
    reaching here has a validator.
    """
    return validate_cel(cel, context)


def _validate_legacy(cel: str) -> list:
    """Syntax-only marker array kept for clients on the pre-context contract.

    Deliberately narrow: it under-reports, because a parseable non-boolean
    expression comes back clean. Migrate callers to `context` rather than
    widening this.
    """
    try:
        CelToAstConverter.convert_to_ast(cel)
        return []
    except CELParseError as e:
        return [
            CelExpressionValidationMarker(
                columnStart=e.column,
                columnEnd=e.column + 1,
            )
        ]
    except Exception:
        # Unsupported methods, map literals and ternaries surface as plain
        # NotImplementedError/ValueError. They used to escape the route as a 500;
        # report them as a rejection covering the whole expression instead, which
        # is the only position this response shape can express.
        logger.info("CEL expression rejected without a source position", exc_info=True)
        return [
            CelExpressionValidationMarker(
                columnStart=1,
                columnEnd=len(cel) + 1,
            )
        ]
