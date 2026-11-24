"""What of a log record could end up somewhere, for the never-logged tests."""
import logging

from src.utils.logging import CONFIG, CustomJsonFormatter

_FORMATTER = CustomJsonFormatter(CONFIG["formatters"]["json"]["fmt"])


def rendered(record: logging.LogRecord) -> str:
    """The gateway's own JSON line (exception text included) plus every attribute,
    because other formatters print other parts."""
    return f"{_FORMATTER.format(record)} {vars(record)!r}"
