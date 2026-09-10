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
from src.repositories.cel_to_sql.ast_nodes import (
    ComparisonNode,
    ConstantNode,
    LogicalNode,
    MemberAccessNode,
    Node,
    ParenthesisNode,
    UnaryNode,
    UnaryNodeOperator,
)
from src.repositories.cel_to_sql.properties_mapper import MultipleFieldsNode
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


def _diagnostic(
    code: CelDiagnosticCode,
    message: str = None,
    range: CelDiagnosticRange = None,
) -> CelDiagnostic:
    return CelDiagnostic(code=code, message=message or _MESSAGES[code], range=range)


def _is_boolean_filter(node) -> bool:
    """Whether `node` yields a true/false result usable as a filter.

    Bare field references count: the converter turns them into a truthiness test,
    which is a valid filter. A bare non-boolean *literal* does not - a quoted
    string or a number is a value, not a filter.
    """
    if isinstance(node, ParenthesisNode):
        return _is_boolean_filter(node.expression)

    if isinstance(node, LogicalNode):
        # Both operands of the logical operator must themselves be filters,
        # otherwise the generated SQL puts a bare value where a predicate belongs.
        return _is_boolean_filter(node.left) and _is_boolean_filter(node.right)

    if isinstance(node, UnaryNode):
        if node.operator == UnaryNodeOperator.NOT:
            return _is_boolean_filter(node.operand)
        if node.operator == UnaryNodeOperator.HAS:
            return True
        # Arithmetic negation yields a number.
        return False

    if isinstance(node, ComparisonNode):
        return True

    if isinstance(node, ConstantNode):
        return isinstance(node.value, bool)

    if isinstance(node, (MemberAccessNode, MultipleFieldsNode)):
        return True

    # Anything else (arithmetic, ternaries, ...) is rejected by the converter
    # before it reaches here; treat it as non-boolean if it ever does.
    return False


def _diagnostics_from_converter_error(exc: CelToSqlException) -> List[CelDiagnostic]:
    code = _CONVERTER_CODE_TO_DIAGNOSTIC.get(
        getattr(exc, "code", None), CelDiagnosticCode.UNSUPPORTED_EXPRESSION
    )
    return [
        _diagnostic(
            code,
            message=str(exc),
            range=_point_range(
                getattr(exc, "line", None), getattr(exc, "column", None)
            ),
        )
    ]


def validate_alert_filter_cel(cel: Optional[str]) -> CelValidationResult:
    """Validate `cel` as an alert-search filter.

    An empty expression is valid and means "no filter". Endpoints that *require*
    an expression (saved filters, maintenance rules) enforce that separately -
    it is a different rule from CEL validity.
    """
    # Imported lazily: pulling the alert field metadata in at module import time
    # would drag the database engine into every importer of this module.
    from src.repositories.alerts import properties_metadata
    from src.repositories.cel_to_sql.cel_ast_converter import CelToAstConverter
    from src.repositories.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
        get_cel_to_sql_provider,
    )

    if not cel or not cel.strip():
        return CelValidationResult(valid=True, diagnostics=[])

    try:
        ast: Node = CelToAstConverter.convert_to_ast(cel)
    except Exception as e:  # noqa: BLE001 - parsing is pure text -> AST, no I/O,
        # so every failure in it is attributable to the submitted expression.
        line = getattr(e, "line", None)
        column = getattr(e, "column", None)
        is_syntax = line is not None or column is not None
        code = (
            CelDiagnosticCode.SYNTAX_ERROR
            if is_syntax
            else CelDiagnosticCode.UNSUPPORTED_EXPRESSION
        )
        return CelValidationResult(
            valid=False,
            diagnostics=[_diagnostic(code, range=_point_range(line, column))],
        )

    # Run the real converter against the configured dialect: an expression that
    # parses can still be unconvertible, and the deployed dialect is part of
    # what "supported" means.
    try:
        get_cel_to_sql_provider(properties_metadata).convert_to_sql_str_v2(cel)
    except CelToSqlException as e:
        return CelValidationResult(
            valid=False, diagnostics=_diagnostics_from_converter_error(e)
        )

    if not _is_boolean_filter(ast):
        return CelValidationResult(
            valid=False,
            diagnostics=[
                _diagnostic(
                    CelDiagnosticCode.EXPECTED_BOOLEAN,
                    # The whole expression is the offending span only when the
                    # expression itself is the non-boolean value; a bad operand
                    # inside a logical expression has no reliable location.
                    range=(
                        None
                        if isinstance(ast, LogicalNode)
                        else _whole_expression_range(cel)
                    ),
                )
            ],
        )

    return CelValidationResult(valid=True, diagnostics=[])


def validate_maintenance_cel(cel: Optional[str]) -> CelValidationResult:
    """Validate `cel` as a maintenance-window condition.

    Maintenance rules are **not** executed as SQL - the event handler evaluates
    them with celpy against the incoming alert payload. So the alert-query rules
    do not apply here: any payload key is a legitimate field, and there is no
    dialect to be compatible with. What still holds is that the expression must
    parse and must produce true/false, because the engine branches on its result.

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

    if not _is_boolean_filter(ast):
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


_VALIDATORS = {
    "alerts": validate_alert_filter_cel,
    "maintenance": validate_maintenance_cel,
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
