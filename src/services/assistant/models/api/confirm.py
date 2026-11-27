"""Bodies of POST /assistant/drafts/{id}/confirm and /discard. Both are optional.

Confirm carries what only the browser can compute from the payload's CEL; Discard says
why the draft was put away. keep-ui sends them; tests/fixtures/assistant_stream.json
pins their shapes and limits.
"""
import json
import math
from typing import Any, Dict, Literal, Optional, get_args

from pydantic import BaseModel, constr, root_validator, validator

from src.services.assistant.models.api.chat import has_control_characters, storable

MAX_SQL_CHARS = 20000
MAX_PARAMS = 200
MAX_PARAM_KEY_CHARS = 200
MAX_PARAM_VALUE_CHARS = 500
MAX_CEL_ROUNDTRIP_CHARS = 4000
# Of all of `derived`, as compact JSON in UTF-8.
MAX_DERIVED_BYTES = 60 * 1024


def _check_key(key: str) -> None:
    """formatQuery names a parameter after its field, `labels.team_1` for one: every
    character a field name may hold, but no whitespace or control character."""
    if not 1 <= len(key) <= MAX_PARAM_KEY_CHARS:
        raise ValueError("key length")
    if any(character.isspace() for character in key) or has_control_characters(key):
        raise ValueError("key character")
    storable(key)


def _check_value(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):  # JSON has no such number
            raise ValueError("not a number")
        return
    if isinstance(value, str):
        if len(value) > MAX_PARAM_VALUE_CHARS:
            raise ValueError("value length")
        storable(value)
        return
    raise ValueError("value type")


def _json_object(value: Any) -> Any:
    """Only a JSON object, where pydantic would also take a list of pairs."""
    if value is None or isinstance(value, (dict, BaseModel)):
        return value
    raise ValueError("not an object")


class SqlQueryInput(BaseModel):
    """The parameterized SQL of the payload's CEL, as formatQuery gives it."""

    sql: constr(strict=True, min_length=1, max_length=MAX_SQL_CHARS)
    # A string, a number or a boolean each.
    params: Dict[str, Any]

    class Config:
        extra = "forbid"

    _sql_storable = validator("sql", allow_reuse=True)(storable)
    _params_object = validator("params", pre=True, allow_reuse=True)(_json_object)

    @validator("params")
    def _bounded(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        if len(params) > MAX_PARAMS:
            raise ValueError("too many")
        for key, value in params.items():
            _check_key(key)
            _check_value(value)
        return params


class DerivedInput(BaseModel):
    """What the browser derives from one parse of the payload's CEL: the CEL it reads
    back and the SQL. The confirm input of the kinds that save a filter; the CEL that is
    stored is always the payload's."""

    cel_roundtrip: constr(strict=True, min_length=1, max_length=MAX_CEL_ROUNDTRIP_CHARS)
    sql_query: SqlQueryInput

    class Config:
        extra = "forbid"

    _cel_storable = validator("cel_roundtrip", allow_reuse=True)(storable)
    _sql_query_object = validator("sql_query", pre=True, allow_reuse=True)(
        _json_object
    )

    @root_validator(pre=True)
    def _bounded(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if derived_bytes(values) > MAX_DERIVED_BYTES:
            raise ValueError("too large")
        return values


def derived_bytes(derived: Any) -> int:
    """As the browser counts it: JSON.stringify, in UTF-8."""
    text = json.dumps(
        derived, ensure_ascii=False, separators=(",", ":"), default=_as_json
    )
    return len(text.encode("utf-8", "surrogatepass"))


def _as_json(value: Any) -> Any:
    """A model built in code, not parsed from a body."""
    if isinstance(value, BaseModel):
        return value.dict()
    raise TypeError("not JSON")


class ConfirmRequest(BaseModel):
    """Unknown keys are ignored: a newer keep-ui may call this version."""

    # Checked against the kind's `confirm_input_model`, once the draft is known.
    derived: Optional[Dict[str, Any]] = None

    _derived_object = validator("derived", pre=True, allow_reuse=True)(_json_object)


DiscardReason = Literal["opened_in_form"]


class DiscardRequest(BaseModel):
    """Unknown keys are ignored, and a reason this version does not know is none."""

    reason: Optional[DiscardReason] = None

    @validator("reason", pre=True)
    def _unknown_is_none(cls, value: Any) -> Any:
        return value if value in get_args(DiscardReason) else None
