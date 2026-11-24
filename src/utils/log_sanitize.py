import unicodedata

# Control characters (CR, LF, ESC, ...), invisible formatting such as text-direction
# overrides, line and paragraph separators, and surrogates that cannot be encoded.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def sanitize_for_log(value, max_len: int = 500) -> str | None:
    """Text from a request (or an error quoting it) that is safe to put in a log line.

    Unsafe characters become "?", so the text cannot start a new line, hide part of
    itself or drive a terminal, and it is cut to max_len characters.
    """
    if value is None:
        return None
    text = str(value)
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return "".join(
        "?" if unicodedata.category(char) in _UNSAFE_CATEGORIES else char
        for char in text
    )
