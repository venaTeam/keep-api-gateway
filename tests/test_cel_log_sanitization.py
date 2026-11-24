"""A caller's CEL must not be able to forge or bloat log lines.

It may appear in `extra` only, stripped of control characters and truncated: never
in the message, its args or a traceback.
"""

import logging
from datetime import datetime

import pytest
from sqlalchemy.exc import OperationalError

from src.models.alert import AlertStatus
from src.models.query import CountGroupBy, CountQueryDto, QueryDto
from src.repositories.alerts import static_facets
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

NEEDLE = "NEEDLEVALUE"
INVALID_CEL = f"name == '{NEEDLE}'\r\nERROR forged line \x1b[31mred\x1b[0m \x9b1m  x"
# Newlines are whitespace to CEL, and escape bytes are ordinary string characters.
VALID_CEL = f"name == '{NEEDLE}\x1b[31m\x9b' &&\r\nseverity != 'x'"
LONG_CEL = f"name == '{NEEDLE}{'a' * 20000}'"

# Longer than any sanitized value or static message, far shorter than LONG_CEL.
MAX_LOGGED = 800


def _query(cel):
    return "/alerts/query", {"cel": cel}


def _count(cel):
    return "/alerts/query/count", {"cel": cel}


def _count_by_field(cel):
    return (
        "/alerts/query/count",
        {"cel": cel, "group_by": "name", "include_missing": True},
    )


def _facets(cel):
    return "/alerts/facets/options", {"cel": cel, "facet_queries": {}}


def _facet_query(cel):
    severity = next(f for f in static_facets if f.property_path == "severity")
    return "/alerts/facets/options", {"cel": "", "facet_queries": {severity.id: cel}}


def _batch(cel):
    return "/alerts/batch_enrich", {"cel": cel, "enrichments": {"status": "firing"}}


REQUESTS = {
    "query": _query,
    "count": _count,
    "count_by_field": _count_by_field,
    "facets": _facets,
    "batch_enrich": _batch,
}


def _is_control(char):
    code = ord(char)
    return code < 32 or 127 <= code < 160 or char in "  "


def _assert_clean(record):
    """Nothing in the record carries raw CEL outside a bounded, clean extra."""
    message = record.getMessage()
    assert NEEDLE not in message, message
    assert not record.args, record.args
    assert record.exc_info in (None, False, (None, None, None)), message
    assert record.exc_text is None
    assert record.stack_info is None
    for key, value in vars(record).items():
        text = str(value)
        assert not any(_is_control(char) for char in text), (message, key, text)
        assert len(text) < MAX_LOGGED, (message, key, len(text))


def _own_records(caplog):
    return [record for record in caplog.records if record.name.startswith("src.")]


@pytest.fixture
def logs(create_alert, caplog):
    create_alert(
        "fp-1", AlertStatus.FIRING, datetime.utcnow(), {"name": "x", "severity": "high"}
    )
    caplog.set_level(logging.INFO)
    caplog.clear()
    return caplog


@pytest.mark.parametrize("name", [*REQUESTS, "facet_query"])
@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_rejected_cel(db_session, client, test_app, logs, name):
    path, body = {**REQUESTS, "facet_query": _facet_query}[name](INVALID_CEL)

    response = client.post(path, headers={"x-api-key": "k"}, json=body)

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "INVALID_CEL"
    records = _own_records(logs)
    for record in records:
        _assert_clean(record)
        # A caller's mistake is not a service error.
        assert record.levelno < logging.ERROR, record.getMessage()
    # It was logged once by the converter, once by the route: one record each, with
    # the expression still there to debug with.
    for logger_name, level in (
        ("src.repositories.cel_to_sql.cel_ast_converter", logging.WARNING),
        ("src.routes.alerts", logging.WARNING),
    ):
        (record,) = [
            r for r in records if r.name == logger_name and r.levelno == level
        ]
        if name != "facet_query":
            assert NEEDLE in record.cel
            assert record.error


@pytest.mark.parametrize("cel", [VALID_CEL, LONG_CEL], ids=["control", "long"])
# The facets query does not run on the SQLite test database; on success that route
# logs no CEL.
@pytest.mark.parametrize("name", [name for name in REQUESTS if name != "facets"])
@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_accepted_cel(db_session, client, test_app, logs, name, cel):
    path, body = REQUESTS[name](cel)

    response = client.post(path, headers={"x-api-key": "k"}, json=body)

    assert response.status_code == 200, response.text
    records = _own_records(logs)
    assert any(record.name == "src.routes.alerts" for record in records)
    for record in records:
        _assert_clean(record)


@pytest.mark.parametrize(
    "function,builder,query",
    [
        ("query_last_alerts", "build_alerts_query", QueryDto(cel=VALID_CEL)),
        (
            "query_total_alerts_count",
            "build_total_alerts_query",
            QueryDto(cel=VALID_CEL),
        ),
        (
            "query_distinct_alerts_count",
            "build_distinct_count_query",
            CountQueryDto(cel=VALID_CEL, group_by=CountGroupBy.NAME),
        ),
        (
            "query_missing_alerts_count",
            "build_missing_count_query",
            CountQueryDto(cel=VALID_CEL, group_by=CountGroupBy.NAME),
        ),
    ],
)
def test_database_failure_does_not_log_raw_cel(
    db_session, monkeypatch, caplog, function, builder, query
):
    from src.repositories import alerts as alerts_repository

    def fail(**kwargs):
        # The driver error quotes the statement, which embeds the CEL literals.
        raise OperationalError(
            f"SELECT 1 WHERE name = '{NEEDLE}\x1b[31m\r\n'", {}, Exception("gone")
        )

    monkeypatch.setattr(alerts_repository, builder, fail)
    caplog.set_level(logging.INFO)

    with pytest.raises(OperationalError):
        getattr(alerts_repository, function)(tenant_id="keep", query=query)

    (record,) = [r for r in _own_records(caplog) if r.levelno == logging.ERROR]
    _assert_clean(record)
    assert NEEDLE in record.query
    assert "gone" in record.error
    assert record.tenant_id == "keep"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_other_cel_selection_failure_is_sanitized(
    db_session, client, test_app, logs, monkeypatch
):
    from src.routes import alerts as alerts_routes

    def fail(*args, **kwargs):
        raise RuntimeError("boom\r\nERROR forged \x1b[31m" + "a" * 5000)

    monkeypatch.setattr(alerts_routes, "query_last_alerts", fail)
    path, body = _batch(VALID_CEL)

    response = client.post(path, headers={"x-api-key": "k"}, json=body)

    assert response.json()["status"] == "failed"
    (record,) = [
        r for r in _own_records(logs) if r.getMessage() == "Failed to process CEL query"
    ]
    assert record.error.startswith("boom")
    assert not any(_is_control(char) for char in record.error)
    assert len(record.error) < MAX_LOGGED


# --------------------------------------------------------------------------- #
# The sanitizer
# --------------------------------------------------------------------------- #
def test_sanitizer_replaces_control_characters():
    from src.utils.log_sanitize import sanitize_for_log

    assert sanitize_for_log("a\r\nb\tc\x00d\x1be\x7ff\x9bg") == "a??b?c?d?e?f?g"
    # Line separators, text-direction overrides and unencodable surrogates too.
    assert sanitize_for_log("a b c‮d\ud800e") == "a?b?c?d?e"
    assert sanitize_for_log("name == 'café ש'") == "name == 'café ש'"


def test_sanitizer_truncates():
    from src.utils.log_sanitize import sanitize_for_log

    assert sanitize_for_log("a" * 500) == "a" * 500
    assert len(sanitize_for_log("a" * 501)) == 500
    assert sanitize_for_log("a" * 501).endswith("...")
    assert sanitize_for_log("abcdef", max_len=5) == "ab..."


def test_sanitizer_takes_anything():
    from src.utils.log_sanitize import sanitize_for_log

    assert sanitize_for_log(None) is None
    assert sanitize_for_log("") == ""
    assert sanitize_for_log(ValueError("bad\nvalue")) == "bad?value"
