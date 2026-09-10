"""Wire models for the CEL validation contract.

Editor coordinates are **one-based** with an **exclusive end** position, matching
Monaco's `IMarkerData`. A range is optional: it is present only when the failure
has a reliable source location, and is omitted rather than invented for semantic
errors that cannot be traced back to a span of text.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class CelDiagnosticCode(str, Enum):
    """Machine-readable reason a CEL expression was rejected."""

    SYNTAX_ERROR = "SYNTAX_ERROR"
    EXPECTED_BOOLEAN = "EXPECTED_BOOLEAN"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    UNSUPPORTED_EXPRESSION = "UNSUPPORTED_EXPRESSION"


class CelDiagnosticRange(BaseModel):
    """One-based, end-exclusive editor coordinates."""

    startLine: int
    startColumn: int
    endLine: int
    endColumn: int


class CelDiagnostic(BaseModel):
    code: CelDiagnosticCode
    message: str
    range: Optional[CelDiagnosticRange] = None


class CelValidationResult(BaseModel):
    """Body of a *completed* validation check.

    HTTP 200 means the check ran, not that the expression is valid - clients must
    read `valid`. A transport or service failure is a failed validation request
    and must never be read as `valid: true`.
    """

    valid: bool
    diagnostics: List[CelDiagnostic] = Field(default_factory=list)


class InvalidCelDetail(BaseModel):
    """`detail` payload of the HTTP 400 returned for a rejected CEL filter."""

    code: str = "INVALID_CEL"
    message: str
    diagnostics: List[CelDiagnostic] = Field(default_factory=list)
