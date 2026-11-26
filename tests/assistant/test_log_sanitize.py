import pytest

from src.services.assistant.core.log_sanitize import MAX_LOG_CHARS, sanitize_for_log


@pytest.mark.parametrize(
    "separator", ["\r", "\n", "\r\n", "\x00", "\x1b", "\x7f", "\x85", " ", " "]
)
def test_control_characters_and_line_separators_are_removed(separator):
    cleaned = sanitize_for_log(f"before{separator}ERROR forged line")
    assert separator not in cleaned
    assert cleaned.splitlines() == [cleaned]
    assert cleaned.startswith("before")


def test_long_text_is_truncated():
    cleaned = sanitize_for_log("x" * 10_000)
    assert cleaned == "x" * MAX_LOG_CHARS + "..."


def test_text_at_the_limit_is_kept_whole():
    assert sanitize_for_log("x" * MAX_LOG_CHARS) == "x" * MAX_LOG_CHARS


def test_non_string_values_are_rendered():
    assert sanitize_for_log(None) == "None"
    assert sanitize_for_log(42) == "42"
