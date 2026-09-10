"""Backend-owned validation for CEL filters.

This module is the single source of truth for "is this CEL a usable filter?".
`/cel/validate` (preflight) and every endpoint that executes an alert filter call
into the same function, so a preflight verdict and an execution verdict cannot
drift apart.

What is checked, in order:

1. **Syntax** - the expression parses.
2. **Fields, methods and dialect compatibility** - the expression converts to SQL
   for the *configured* database dialect, using the same converter the query runs
   on. Unknown fields, unsupported methods and unsupported node types fail here.
3. **Boolean result** - the expression evaluates to true/false, including inside
   parentheses and on both sides of a logical operator.

A successful check is a preflight, not a guarantee: the query still validates
independently, and later database execution can still fail for its own reasons.
"""

import logging
from typing import List, Optional

from fastapi import HTTPException

from src.models.cel import (
    CelDiagnostic,
    CelDiagnosticCode,
    CelDiagnosticRange,
    CelValidationResult,
    InvalidCelDetail,
)
from src.repositories.cel_to_sql.ast_nodes import LogicalNode, Node, is_boolean_filter_node
from src.repositories.cel_to_sql.sql_providers.base import (
    CelToSqlErrorCode,
    CelToSqlException,
)

logger = logging.getLogger(__name__)

INVALID_CEL_CODE = "INVALID_CEL"
INVALID_CEL_MESSAGE = "The CEL filter is invalid."

_MESSAGES = {
    CelDiagnosticCode.SYNTAX_ERROR: "The expression could not be parsed.",
    CelDiagnosticCode.EXPECTED_BOOLEAN: "A CEL filter must evaluate to true or false.",
    CelDiagnosticCode.UNKNOWN_FIELD: "Unknown field in the expression.",
    CelDiagnosticCode.UNSUPPORTED_EXPRESSION: (
        "This expression is not supported by the alert filter."
    ),
}

_CONVERTER_CODE_TO_DIAGNOSTIC = {
    CelToSqlErrorCode.SYNTAX_ERROR: CelDiagnosticCode.SYNTAX_ERROR,
    CelToSqlErrorCode.EXPECTED_BOOLEAN: CelDiagnosticCode.EXPECTED_BOOLEAN,
    CelToSqlErrorCode.UNKNOWN_FIELD: CelDiagnosticCode.UNKNOWN_FIELD,
    CelToSqlErrorCode.UNSUPPORTED_EXPRESSION: CelDiagnosticCode.UNSUPPORTED_EXPRESSION,
}


class InvalidCelException(Exception):
    """A CEL filter the backend refuses to execute.

    Carries the diagnostics so every endpoint returns the same structured 400.
    """

    def __init__(self, diagnostics: List[CelDiagnostic], cel: str = ""):
        super().__init__(INVALID_CEL_MESSAGE)
        self.diagnostics = diagnostics
        self.cel = cel


def _whole_expression_range(cel: str) -> CelDiagnosticRange:
    """Cover the entire expression - one-based, end position exclusive."""
    lines = cel.split("\n")
    return CelDiagnosticRange(
        startLine=1,
        startColumn=1,
        endLine=len(lines),
        endColumn=len(lines[-1]) + 1,
    )


def _point_range(
    line: Optional[int], column: Optional[int]
) -> Optional[CelDiagnosticRange]:
    """A single-character range at a parser-reported position.

    Returns None when the parser did not report one - a missing location is
    reported as missing rather than guessed at.
    """
    if line is None or column is None:
        return None

    return CelDiagnosticRange(
        startLine=line,
        startColumn=column,
        endLine=line,
        endColumn=column + 1,
    )


def _range_from_converter_error(exc: CelToSqlException) -> Optional[CelDiagnosticRange]:
    """The span the converter attached, or None when it could not locate one."""
    line = getattr(exc, "line", None)
    column = getattr(exc, "column", None)

    if line is None or column is None:
        return None

    return CelDiagnosticRange(
        startLine=line,
        startColumn=column,
        endLine=getattr(exc, "end_line", None) or line,
        endColumn=getattr(exc, "end_column", None) or column + 1,
    )


def _diagnostic(
    code: CelDiagnosticCode,
    message: str = None,
    range: CelDiagnosticRange = None,
) -> CelDiagnostic:
    return CelDiagnostic(code=code, message=message or _MESSAGES[code], range=range)


def _diagnostics_from_converter_error(exc: CelToSqlException) -> List[CelDiagnostic]:
    code = _CONVERTER_CODE_TO_DIAGNOSTIC.get(
        getattr(exc, "code", None), CelDiagnosticCode.UNSUPPORTED_EXPRESSION
    )
    return [
        _diagnostic(code, message=str(exc), range=_range_from_converter_error(exc))
    ]


def validate_alert_filter_cel(cel: Optional[str]) -> CelValidationResult:
    """Validate `cel` as an alert-search filter.

    Delegates to the very converter the query runs on, so a preflight verdict
    and an execution verdict cannot disagree - syntax, fields, methods, the
    boolean-result rule and dialect compatibility are all checked by that one
    call, and neither path converts the expression twice.

    An empty expression is valid and means "no filter". Endpoints that *require*
    an expression (saved filters, maintenance rules) enforce that separately -
    it is a different rule from CEL validity.
    """
    # Imported lazily: pulling the alert field metadata in at module import time
    # would drag the database engine into every importer of this module.
    from src.repositories.alerts import properties_metadata
    from src.repositories.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
        get_cel_to_sql_provider,
    )

    if not cel or not cel.strip():
        return CelValidationResult(valid=True, diagnostics=[])

    try:
        get_cel_to_sql_provider(properties_metadata).convert_to_sql_str_v2(cel)
    except CelToSqlException as e:
        return CelValidationResult(
            valid=False, diagnostics=_diagnostics_from_converter_error(e)
        )

    return CelValidationResult(valid=True, diagnostics=[])


def validate_event_filter_cel(cel: Optional[str]) -> CelValidationResult:
    """Validate `cel` as an in-process filter over a raw event payload.

    Maintenance windows, extraction rules, correlation rules and workflow
    triggers are **not** executed as SQL - they are evaluated with celpy against
    the incoming alert payload, in the event handler or the workflow engine. So
    the alert-query rules do not apply: any payload key is a legitimate field,
    and there is no database dialect to be compatible with.

    What still holds is that the expression must parse and must produce
    true/false, because every one of those engines branches on its result.

    Whether an expression is *required* is the caller's rule, not this one.
    """
    from src.repositories.cel_to_sql.cel_ast_converter import CelToAstConverter

    if not cel or not cel.strip():
        return CelValidationResult(valid=True, diagnostics=[])

    try:
        ast: Node = CelToAstConverter.convert_to_ast(cel)
    except Exception as e:  # noqa: BLE001 - parsing failures are input failures
        line = getattr(e, "line", None)
        column = getattr(e, "column", None)
        code = (
            CelDiagnosticCode.SYNTAX_ERROR
            if line is not None or column is not None
            else CelDiagnosticCode.UNSUPPORTED_EXPRESSION
        )
        return CelValidationResult(
            valid=False,
            diagnostics=[_diagnostic(code, range=_point_range(line, column))],
        )

    if not is_boolean_filter_node(ast):
        return CelValidationResult(
            valid=False,
            diagnostics=[
                _diagnostic(
                    CelDiagnosticCode.EXPECTED_BOOLEAN,
                    range=(
                        None
                        if isinstance(ast, LogicalNode)
                        else _whole_expression_range(cel)
                    ),
                )
            ],
        )

    return CelValidationResult(valid=True, diagnostics=[])


# Every feature names its own context, even where two of them currently share an
# implementation: that keeps a later divergence a one-line change here instead of
# a silent behaviour change for whichever feature was borrowing the other's name.
_VALIDATORS = {
    "alerts": validate_alert_filter_cel,
    "maintenance": validate_event_filter_cel,
    "extraction": validate_event_filter_cel,
    "rules": validate_event_filter_cel,
    "workflows": validate_event_filter_cel,
}


def validate_cel(cel: Optional[str], context: str) -> CelValidationResult:
    """Validate `cel` for a named execution context.

    The CEL editor is shared, but the engines behind it are not: a context is
    required so alert-query rules are never silently applied to an expression
    that another engine executes.
    """
    validator = _VALIDATORS.get(context)

    if validator is None:
        raise NotImplementedError(f"No validation defined for context '{context}'")

    return validator(cel)


def ensure_valid_cel(cel: Optional[str], context: str) -> None:
    """Raise `InvalidCelException` unless `cel` is valid for `context`."""
    result = validate_cel(cel, context)

    if not result.valid:
        raise InvalidCelException(diagnostics=result.diagnostics, cel=cel or "")


def ensure_valid_alert_filter_cel(cel: Optional[str]) -> None:
    """Raise `InvalidCelException` unless `cel` is a usable alert filter."""
    ensure_valid_cel(cel, "alerts")


def invalid_cel_detail(diagnostics: List[CelDiagnostic]) -> dict:
    return InvalidCelDetail(
        code=INVALID_CEL_CODE,
        message=INVALID_CEL_MESSAGE,
        diagnostics=diagnostics,
    ).dict()


def invalid_cel_http_exception(
    cel: Optional[str], diagnostics: List[CelDiagnostic] = None
) -> HTTPException:
    """Build the structured 400 every alert-filter endpoint returns.

    Generic request-validation problems (bad pagination, malformed body) must not
    be routed through here - only a rejected CEL filter carries INVALID_CEL.
    """
    logger.info(
        "Rejecting invalid CEL filter",
        extra={
            "cel": cel,
            "diagnostic_codes": [d.code for d in (diagnostics or [])],
        },
    )
    return HTTPException(status_code=400, detail=invalid_cel_detail(diagnostics or []))


def http_exception_from_converter_error(
    cel: Optional[str], exc: CelToSqlException
) -> HTTPException:
    """Map a converter rejection raised during execution to the same 400."""
    return invalid_cel_http_exception(cel, _diagnostics_from_converter_error(exc))


def http_exception_from_invalid_cel(exc: InvalidCelException) -> HTTPException:
    return invalid_cel_http_exception(exc.cel, exc.diagnostics)
