"""HTTP contract for CEL validation and for alert-filter execution.

Every invalid expression is asserted twice - once through the preflight and once
through a direct query - so the two cannot report different verdicts.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.repositories.cel_to_sql.sql_providers.get_cel_to_sql_provider_for_dialect import (
    get_cel_to_sql_provider_for_dialect,
)
from src.repositories.cel_to_sql.sql_providers.base import (
    CelToSqlErrorCode,
    CelToSqlException,
)
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

AUTH = {"x-api-key": "some-key"}

INVALID_EXPRESSIONS = [
    "severity ==",  # malformed syntax
    "'some text'",  # standalone string
    "42",  # standalone number
    "('some text')",  # parenthesised non-boolean
    "severity == 'critical' && 'text'",  # non-boolean logical operand
    "no_such_field == 'x'",  # unknown field
    "severity.matches('x')",  # unsupported method
]

VALID_EXPRESSIONS = [
    "severity == 'critical'",
    "status == 'firing' && severity == 'critical'",
    "name.contains('cpu')",
    "severity",
    "",  # empty alert-search CEL means no filter
]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("cel", VALID_EXPRESSIONS)
def test_preflight_accepts_supported_expressions(db_session, client, test_app, cel):
    response = client.post(
        "/cel/validate", headers=AUTH, json={"cel": cel, "context": "alerts"}
    )

    assert response.status_code == 200
    assert response.json() == {"valid": True, "diagnostics": []}


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("cel", INVALID_EXPRESSIONS)
def test_preflight_returns_200_with_valid_false(db_session, client, test_app, cel):
    """A completed check is HTTP 200 even when the expression is invalid.

    The client must read the body - a successful request does not establish that
    the expression is valid.
    """
    response = client.post(
        "/cel/validate", headers=AUTH, json={"cel": cel, "context": "alerts"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert len(body["diagnostics"]) >= 1
    assert body["diagnostics"][0]["code"]
    assert body["diagnostics"][0]["message"]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("cel", INVALID_EXPRESSIONS)
def test_direct_query_rejects_the_same_expressions(db_session, client, test_app, cel):
    """A caller that skips the preflight gets the same verdict from the query."""
    response = client.post(
        "/alerts/query", headers=AUTH, json={"cel": cel, "limit": 20, "offset": 0}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "INVALID_CEL"
    assert detail["message"]
    assert len(detail["diagnostics"]) >= 1


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("cel", INVALID_EXPRESSIONS)
def test_count_rejects_the_same_expressions(db_session, client, test_app, cel):
    response = client.post(
        "/alerts/query/count", headers=AUTH, json={"cel": cel, "limit": 20, "offset": 0}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CEL"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("cel", INVALID_EXPRESSIONS)
def test_facet_options_reject_the_same_expressions(db_session, client, test_app, cel):
    response = client.post(
        "/alerts/facets/options", headers=AUTH, json={"cel": cel, "facet_queries": {}}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CEL"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_facet_options_reject_an_invalid_per_facet_query(db_session, client, test_app):
    response = client.post(
        "/alerts/facets/options",
        headers=AUTH,
        json={"cel": "", "facet_queries": {"severity": "'some text'"}},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CEL"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_query_accepts_a_valid_expression(db_session, client, test_app):
    response = client.post(
        "/alerts/query",
        headers=AUTH,
        json={"cel": "severity == 'critical'", "limit": 20, "offset": 0},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_diagnostic_positions_are_one_based_and_end_exclusive(
    db_session, client, test_app
):
    response = client.post(
        "/cel/validate", headers=AUTH, json={"cel": "severity ==", "context": "alerts"}
    )

    diagnostic = response.json()["diagnostics"][0]
    assert diagnostic["code"] == "SYNTAX_ERROR"
    assert diagnostic["range"] == {
        "startLine": 1,
        "startColumn": len("severity ") + 1,
        "endLine": 1,
        "endColumn": len("severity ") + 2,
    }


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_diagnostic_omits_the_range_when_no_position_is_available(
    db_session, client, test_app
):
    response = client.post(
        "/cel/validate",
        headers=AUTH,
        json={"cel": "severity == 'critical' && 'text'", "context": "alerts"},
    )

    diagnostic = response.json()["diagnostics"][0]
    assert diagnostic["code"] == "EXPECTED_BOOLEAN"
    assert diagnostic["range"] is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_unknown_context_is_a_request_validation_error(db_session, client, test_app):
    """A bad `context` is a malformed request, not an invalid CEL expression."""
    response = client.post(
        "/cel/validate", headers=AUTH, json={"cel": "severity", "context": "nope"}
    )

    assert response.status_code == 422
    assert "INVALID_CEL" not in response.text


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_generic_request_validation_is_not_labelled_invalid_cel(
    db_session, client, test_app
):
    response = client.post(
        "/alerts/query",
        headers=AUTH,
        json={"cel": "severity == 'critical'", "limit": "not-a-number"},
    )

    assert response.status_code == 422
    assert "INVALID_CEL" not in response.text


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
class TestLegacyContract:
    """Clients that have not migrated still get the marker-array response."""

    def test_valid_expression_returns_an_empty_array(self, db_session, client, test_app):
        response = client.post("/cel/validate", headers=AUTH, json={"cel": "severity"})

        assert response.status_code == 200
        assert response.json() == []

    def test_syntax_error_returns_markers(self, db_session, client, test_app):
        response = client.post(
            "/cel/validate", headers=AUTH, json={"cel": "severity =="}
        )

        assert response.status_code == 200
        (marker,) = response.json()
        assert marker["columnEnd"] == marker["columnStart"] + 1

    def test_unsupported_expression_no_longer_escapes_as_a_500(
        self, db_session, client, test_app
    ):
        cel = "severity.matches('x')"
        response = client.post("/cel/validate", headers=AUTH, json={"cel": cel})

        assert response.status_code == 200
        (marker,) = response.json()
        assert marker == {"columnStart": 1, "columnEnd": len(cel) + 1}


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
class TestDatabaseFailuresStayFailures:
    """A failed query must never be disguised as a successful empty result."""

    @staticmethod
    def _operational_error():
        return OperationalError("SELECT 1", {}, Exception("connection refused"))

    @staticmethod
    def _client_that_reports_500(client):
        """A client that returns the 500 instead of re-raising it.

        The default test client re-raises server exceptions, which would hide
        the thing under test: what the *caller* sees.
        """
        return TestClient(client.app, raise_server_exceptions=False)

    def test_query_surfaces_a_database_failure(self, db_session, client, test_app):
        with patch(
            "src.repositories.alerts.build_alerts_query",
            side_effect=self._operational_error(),
        ):
            response = self._client_that_reports_500(client).post(
                "/alerts/query",
                headers=AUTH,
                json={"cel": "severity == 'critical'", "limit": 20, "offset": 0},
            )

        assert response.status_code == 500
        assert "INVALID_CEL" not in response.text

    def test_count_surfaces_a_database_failure(self, db_session, client, test_app):
        with patch(
            "src.repositories.alerts.build_total_alerts_query",
            side_effect=self._operational_error(),
        ):
            response = self._client_that_reports_500(client).post(
                "/alerts/query/count",
                headers=AUTH,
                json={"cel": "severity == 'critical'", "limit": 20, "offset": 0},
            )

        assert response.status_code == 500
        assert "INVALID_CEL" not in response.text

    def test_facet_options_surface_a_database_failure(
        self, db_session, client, test_app
    ):
        with patch(
            "src.repositories.facets.get_facets_query_builder",
            side_effect=self._operational_error(),
        ):
            response = self._client_that_reports_500(client).post(
                "/alerts/facets/options",
                headers=AUTH,
                json={"cel": "", "facet_queries": {"severity": "severity"}},
            )

        assert response.status_code == 500
        assert "INVALID_CEL" not in response.text


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
@pytest.mark.parametrize(
    "cel,expected_code",
    [
        ("no_such_field == 'x'", CelToSqlErrorCode.UNKNOWN_FIELD),
        ("severity.matches('x')", CelToSqlErrorCode.UNSUPPORTED_EXPRESSION),
        ("1 + 2", CelToSqlErrorCode.UNSUPPORTED_EXPRESSION),
        ("severity ==", CelToSqlErrorCode.SYNTAX_ERROR),
    ],
)
def test_converter_classifies_rejections_for_every_dialect(dialect, cel, expected_code):
    """Error classification must not depend on which database is deployed.

    Generated SQL differs per dialect, so the rule that decides 400-vs-500 is
    asserted against each of them rather than sqlite alone.
    """
    from src.repositories.alerts import properties_metadata

    provider = get_cel_to_sql_provider_for_dialect(dialect, properties_metadata)

    with pytest.raises(CelToSqlException) as exc_info:
        provider.convert_to_sql_str_v2(cel)

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
@pytest.mark.parametrize("cel", VALID_EXPRESSIONS)
def test_supported_expressions_convert_for_every_dialect(dialect, cel):
    from src.repositories.alerts import properties_metadata

    provider = get_cel_to_sql_provider_for_dialect(dialect, properties_metadata)

    provider.convert_to_sql_str_v2(cel)
