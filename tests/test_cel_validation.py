"""Unit coverage for the shared CEL validation path.

These exercise the validator directly - the dialect-dependent SQL conversion it
performs is covered against the deployed dialect by `tests/cel_to_sql`, and the
HTTP contract by `tests/test_cel_validation_routes.py`.
"""

import pytest

from src.models.cel import CelDiagnosticCode
from src.services.cel_validation import (
    InvalidCelException,
    ensure_valid_alert_filter_cel,
    validate_alert_filter_cel,
    validate_cel,
    validate_maintenance_cel,
)


def _codes(result):
    return [d.code for d in result.diagnostics]


@pytest.mark.parametrize(
    "cel",
    [
        "severity == 'critical'",
        "severity > 'info'",
        "status == 'firing' && severity == 'critical'",
        "(severity == 'critical')",
        "!(severity == 'critical')",
        "name.contains('cpu')",
        "name.startsWith('cpu')",
        "name.endsWith('cpu')",
        "severity in ['critical', 'high']",
        "has(labels.foo)",
        "true",
        # A bare field is a truthiness filter, which the converter supports.
        "severity",
        "deleted",
    ],
)
def test_supported_alert_filters_are_valid(cel):
    result = validate_alert_filter_cel(cel)

    assert result.valid, _codes(result)
    assert result.diagnostics == []


@pytest.mark.parametrize("cel", ["", "   ", None])
def test_empty_alert_search_cel_means_no_filter(cel):
    """An alert search without an expression is unfiltered, not invalid."""
    result = validate_alert_filter_cel(cel)

    assert result.valid
    assert result.diagnostics == []


@pytest.mark.parametrize(
    "cel,expected_code",
    [
        ("severity ==", CelDiagnosticCode.SYNTAX_ERROR),
        ("a b c", CelDiagnosticCode.SYNTAX_ERROR),
        # Standalone values are not filters.
        ("'some text'", CelDiagnosticCode.EXPECTED_BOOLEAN),
        ('"some text"', CelDiagnosticCode.EXPECTED_BOOLEAN),
        ("42", CelDiagnosticCode.EXPECTED_BOOLEAN),
        # ... including wrapped in parentheses, or as a logical operand.
        ("('some text')", CelDiagnosticCode.EXPECTED_BOOLEAN),
        ("severity == 'critical' && 'text'", CelDiagnosticCode.EXPECTED_BOOLEAN),
        ("'text' || severity == 'critical'", CelDiagnosticCode.EXPECTED_BOOLEAN),
        # Unknown fields and unsupported constructs.
        ("no_such_field == 'x'", CelDiagnosticCode.UNKNOWN_FIELD),
        ("severity.matches('x')", CelDiagnosticCode.UNSUPPORTED_EXPRESSION),
        ("1 + 2", CelDiagnosticCode.UNSUPPORTED_EXPRESSION),
        ("severity > 'info' ? 1 : 2", CelDiagnosticCode.UNSUPPORTED_EXPRESSION),
    ],
)
def test_rejected_alert_filters(cel, expected_code):
    result = validate_alert_filter_cel(cel)

    assert not result.valid
    assert _codes(result) == [expected_code]


def test_syntax_error_carries_a_one_based_position():
    result = validate_alert_filter_cel("severity ==")

    (diagnostic,) = result.diagnostics
    assert diagnostic.range is not None
    # One-based, end exclusive: the range covers exactly the offending character.
    assert diagnostic.range.startLine == 1
    assert diagnostic.range.endLine == 1
    assert diagnostic.range.endColumn == diagnostic.range.startColumn + 1
    assert diagnostic.range.startColumn == len("severity ") + 1


def test_standalone_value_range_covers_the_whole_expression():
    cel = "'some text'"
    result = validate_alert_filter_cel(cel)

    (diagnostic,) = result.diagnostics
    assert diagnostic.range.startLine == 1
    assert diagnostic.range.startColumn == 1
    assert diagnostic.range.endLine == 1
    assert diagnostic.range.endColumn == len(cel) + 1


def test_semantic_error_without_a_reliable_position_omits_the_range():
    """A bad operand inside a logical expression has no traceable span.

    The converter does not preserve source locations through mapping, so no
    position is invented for it.
    """
    result = validate_alert_filter_cel("severity == 'critical' && 'text'")

    (diagnostic,) = result.diagnostics
    assert diagnostic.code == CelDiagnosticCode.EXPECTED_BOOLEAN
    assert diagnostic.range is None


def test_ensure_valid_raises_with_diagnostics():
    with pytest.raises(InvalidCelException) as exc_info:
        ensure_valid_alert_filter_cel("'some text'")

    assert exc_info.value.cel == "'some text'"
    assert [d.code for d in exc_info.value.diagnostics] == [
        CelDiagnosticCode.EXPECTED_BOOLEAN
    ]


def test_ensure_valid_accepts_a_supported_filter():
    ensure_valid_alert_filter_cel("severity == 'critical'")


class TestMaintenanceContext:
    """Maintenance runs on celpy, not SQL - it has its own rules."""

    def test_unknown_fields_are_allowed(self):
        """Maintenance conditions match the raw alert payload, so any key is fair.

        Applying the alert-query field mapping here would reject legitimate rules.
        """
        assert validate_maintenance_cel("some_payload_key == 'x'").valid
        assert not validate_alert_filter_cel("some_payload_key == 'x'").valid

    def test_still_requires_a_boolean_result(self):
        result = validate_maintenance_cel("'some text'")

        assert not result.valid
        assert _codes(result) == [CelDiagnosticCode.EXPECTED_BOOLEAN]

    def test_still_requires_valid_syntax(self):
        result = validate_maintenance_cel("severity ==")

        assert not result.valid
        assert _codes(result) == [CelDiagnosticCode.SYNTAX_ERROR]


def test_validate_cel_dispatches_by_context():
    assert validate_cel("severity == 'critical'", "alerts").valid
    assert validate_cel("anything == 'x'", "maintenance").valid

    with pytest.raises(NotImplementedError):
        validate_cel("severity == 'critical'", "not-a-context")
