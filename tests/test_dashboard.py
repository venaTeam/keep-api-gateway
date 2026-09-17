from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from src.routes import dashboard
from tests.fixtures.client import client, test_app  # noqa

TICKET_URL = "https://tickets.example.com/count"


def test_compose_ticket_url_maps_detection_hamal():
    """detection=hamal sets system_failure=true."""
    url = dashboard._compose_ticket_url(TICKET_URL, None, None, "hamal")
    assert "system_failure=true" in url


def test_compose_ticket_url_maps_detection_direct():
    """detection=direct sets system_failure=false."""
    url = dashboard._compose_ticket_url(TICKET_URL, None, None, "direct")
    assert "system_failure=false" in url


def test_compose_ticket_url_ignores_unknown_detection():
    """Any detection value outside direct/hamal is not mapped."""
    url = dashboard._compose_ticket_url(TICKET_URL, None, None, "all")
    assert "system_failure" not in url


def test_compose_ticket_url_passes_team_and_state():
    """team and state are forwarded unchanged."""
    url = dashboard._compose_ticket_url(TICKET_URL, "team-a", "open", None)
    assert "team=team-a" in url
    assert "state=open" in url


def test_compose_ticket_url_preserves_existing_query():
    """Pre-existing query params on count_url are retained and merged."""
    url = dashboard._compose_ticket_url(f"{TICKET_URL}?foo=bar", "team-a", None, None)
    assert "foo=bar" in url
    assert "team=team-a" in url


def test_resolve_ticket_url_returns_count_url():
    """The count_url of the ticket_count provider is returned."""
    providers = [
        SimpleNamespace(
            type="ticket_count",
            details={"authentication": {"count_url": TICKET_URL}},
        )
    ]
    with patch.object(
        dashboard.ProvidersFactory, "get_installed_providers", return_value=providers
    ):
        assert dashboard._resolve_ticket_url("keep") == TICKET_URL


def test_resolve_ticket_url_no_provider_raises_404():
    """Absence of a ticket_count provider raises 404."""
    providers = [SimpleNamespace(type="prometheus", details={})]
    with patch.object(
        dashboard.ProvidersFactory, "get_installed_providers", return_value=providers
    ):
        with pytest.raises(HTTPException) as exc:
            dashboard._resolve_ticket_url("keep")
    assert exc.value.status_code == 404


def test_resolve_ticket_url_missing_count_url_raises_400():
    """A ticket_count provider without a count_url raises 400."""
    providers = [SimpleNamespace(type="ticket_count", details={"authentication": {}})]
    with patch.object(
        dashboard.ProvidersFactory, "get_installed_providers", return_value=providers
    ):
        with pytest.raises(HTTPException) as exc:
            dashboard._resolve_ticket_url("keep")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ticket_count_happy_path(db_session, client, test_app):
    """A numeric provider payload is returned as {"count": N}."""
    with patch(
        "src.routes.dashboard._resolve_ticket_url", return_value=TICKET_URL
    ), patch(
        "src.routes.dashboard._fetch_count_payload",
        new=AsyncMock(return_value={"count": 5}),
    ):
        response = client.get(
            "/dashboard/ticket-count", headers={"x-api-key": "some-key"}
        )
    assert response.status_code == 200
    assert response.json() == {"count": 5}


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ticket_count_team_not_found_passthrough(db_session, client, test_app):
    """A "Team not found" payload is passed through verbatim."""
    payload = {"Team not found": "team-x"}
    with patch(
        "src.routes.dashboard._resolve_ticket_url", return_value=TICKET_URL
    ), patch(
        "src.routes.dashboard._fetch_count_payload",
        new=AsyncMock(return_value=payload),
    ):
        response = client.get(
            "/dashboard/ticket-count", headers={"x-api-key": "some-key"}
        )
    assert response.status_code == 200
    assert response.json() == payload


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ticket_count_non_dict_body_returns_zero(db_session, client, test_app):
    """A non-dict provider body yields {"count": 0} instead of crashing."""
    with patch(
        "src.routes.dashboard._resolve_ticket_url", return_value=TICKET_URL
    ), patch(
        "src.routes.dashboard._fetch_count_payload",
        new=AsyncMock(return_value=[1, 2, 3]),
    ):
        response = client.get(
            "/dashboard/ticket-count", headers={"x-api-key": "some-key"}
        )
    assert response.status_code == 200
    assert response.json() == {"count": 0}


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ticket_count_provider_error_returns_500(db_session, client, test_app):
    """An outbound HTTP failure surfaces as 500."""
    with patch(
        "src.routes.dashboard._resolve_ticket_url", return_value=TICKET_URL
    ), patch(
        "src.routes.dashboard._fetch_count_payload",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    ):
        response = client.get(
            "/dashboard/ticket-count", headers={"x-api-key": "some-key"}
        )
    assert response.status_code == 500


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ticket_count_no_provider_returns_404(db_session, client, test_app):
    """The endpoint returns 404 when no ticket_count provider is installed."""
    providers = [SimpleNamespace(type="prometheus", details={})]
    with patch(
        "src.routes.dashboard.ProvidersFactory.get_installed_providers",
        return_value=providers,
    ):
        response = client.get(
            "/dashboard/ticket-count", headers={"x-api-key": "some-key"}
        )
    assert response.status_code == 404


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ticket_count_composes_filters_into_url(db_session, client, test_app):
    """Query filters are composed into the URL passed to the fetch helper."""
    fetch = AsyncMock(return_value={"count": 1})
    with patch(
        "src.routes.dashboard._resolve_ticket_url", return_value=TICKET_URL
    ), patch("src.routes.dashboard._fetch_count_payload", new=fetch):
        response = client.get(
            "/dashboard/ticket-count?team=team-a&state=open&detection=hamal",
            headers={"x-api-key": "some-key"},
        )
    assert response.status_code == 200
    called_url = fetch.await_args.args[0]
    assert "team=team-a" in called_url
    assert "state=open" in called_url
    assert "system_failure=true" in called_url
