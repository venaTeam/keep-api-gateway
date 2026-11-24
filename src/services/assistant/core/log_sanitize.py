"""Make untrusted text safe to put in a log field."""
import re

# C0 and C1 control characters (CR and LF included), DEL, and the two Unicode
# line separators that some log viewers break lines on.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")

MAX_LOG_CHARS = 200


def sanitize_for_log(value: object, max_chars: int = MAX_LOG_CHARS) -> str:
    """`value` as one line without control characters, truncated."""
    text = value if isinstance(value, str) else str(value)
    # Cut before cleaning too: an enormous value must not be scanned whole.
    cleaned = _CONTROL.sub(" ", text[: max_chars + 1])
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + "..."
    return cleaned
