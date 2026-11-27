"""The compact view of an alert: all that the model ever sees of one.

Alert text comes from external monitoring systems. It is cleaned here so that it
cannot close the prompt's data delimiter, hide text or break jsonb storage.
"""
import json
import unicodedata
from typing import Any

from src.services.assistant.gateway.client import FetchedAlert

MAX_FINGERPRINT_CHARS = 512
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 200
MAX_LABEL_CHARS = 100

DELIMITERS = str.maketrans({"<": "‹", ">": "›"})
# In JSON, `<` and `>` can only be inside strings, where their escapes mean the same:
# the model reads a filter like `count < 5` as it was written, and nothing inside can
# close the data delimiter.
ANGLE_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e"})


def to_json(value: Any, *, ensure_ascii: bool = True) -> str:
    """Compact JSON for the model, without a literal angle bracket."""
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=ensure_ascii
    ).translate(ANGLE_ESCAPES)


def strip_control(value: str) -> str:
    """Line breaks and other control characters become spaces; invisible format
    characters and lone surrogates (which UTF-8 cannot carry) are dropped."""
    kept = []
    for char in value:
        category = unicodedata.category(char)
        if category in ("Cc", "Zl", "Zp"):
            kept.append(" ")
        elif category not in ("Cf", "Cs"):
            kept.append(char)
    return "".join(kept)


# Joiners and direction marks belong to ordinary text in many languages.
_ORDINARY_FORMAT = frozenset("\u200c\u200d\u200e\u200f")


def visible_text(value: str) -> str:
    """`value` without characters that show nothing or reorder what is around them, so
    that a person confirms what they read. Line breaks stay."""
    return "".join(
        char
        for char in value
        if char in "\n\t"
        or char in _ORDINARY_FORMAT
        or unicodedata.category(char) not in ("Cc", "Cf", "Cs")
    )


def clean_text(value: str, max_chars: int) -> str:
    """`value` without control characters and angle brackets, truncated."""
    # Bounded before the scan: alert text can be megabytes long.
    bounded = value[: max_chars * 4]
    return strip_control(bounded)[:max_chars].translate(DELIMITERS)


def text_or_none(value: Any) -> str | None:
    """Alert fields are free-form: only text (or a number) is shown."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def first_source(data: dict[str, Any]) -> str | None:
    source = data.get("source")
    if isinstance(source, list):
        source = source[0] if source else None
    return text_or_none(source)


def last_received(data: dict[str, Any]) -> str | None:
    value = data.get("lastReceived")
    return text_or_none(data.get("last_received") if value is None else value)


def has_incident(alert: FetchedAlert) -> bool | None:
    """None: the endpoint this alert was read from never returns incidents."""
    return bool(alert.data.get("incident")) if alert.incident_known else None


def has_note(data: dict[str, Any]) -> bool:
    note = data.get("note")
    return bool(note.strip() if isinstance(note, str) else note)


def _clean(value: Any, max_chars: int) -> str | None:
    text = text_or_none(value)
    return None if text is None else clean_text(text, max_chars)


def project_alert(alert: FetchedAlert) -> dict[str, Any]:
    data = alert.data
    return {
        "fingerprint": clean_text(alert.fingerprint, MAX_FINGERPRINT_CHARS),
        "name": _clean(data.get("name"), MAX_NAME_CHARS),
        "status": _clean(data.get("status"), MAX_LABEL_CHARS),
        "severity": _clean(data.get("severity"), MAX_LABEL_CHARS),
        "source": _clean(first_source(data), MAX_LABEL_CHARS),
        "service": _clean(data.get("service"), MAX_LABEL_CHARS),
        "environment": _clean(data.get("environment"), MAX_LABEL_CHARS),
        "lastReceived": _clean(last_received(data), MAX_LABEL_CHARS),
        "description": _clean(data.get("description"), MAX_DESCRIPTION_CHARS),
        "has_incident": has_incident(alert),
        "has_note": has_note(data),
    }
