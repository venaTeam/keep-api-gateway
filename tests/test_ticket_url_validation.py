"""A stored `ticket_url` is opened by whoever clicks it, so it must be a web link.

Both enrichment routes refuse anything that is not an absolute http(s) URL before
they write (or, for the CEL form, before they select) anything. Clearing the field
with null or an empty string keeps working.
"""

from datetime import datetime

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import AlertAudit, LastAlert
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

FINGERPRINT = "fp-ticket"
STORED_URL = "https://jira.example.com/browse/OPS-1"
WEBHOOK_KEY = "webhook-key"
WEBHOOK_HEADERS = {"x-api-key": WEBHOOK_KEY}

BAD_URLS = [
    "javascript:alert(document.domain)",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "//example.com/x",
    "/browse/OPS-1",
    "example.com/x",
    "http:",
    "http://",
    "http:/example.com",
    "https://:443/x",
    "https://[bad/x",
    "https://example.com/a b",
    "https://example.com/\tx",
    "https://example.com/\u0000x",
    "https://example.com/\u007fx",
    "https://example.com/\u0085x",
    "https://example.com/ x",
    "https://example.com/x\n",
    " ",
    # Not text at all.
    0,
    False,
    42,
    ["https://example.com"],
    {"href": "https://example.com"},
]

GOOD_URLS = [
    "https://jira.example.com/browse/OPS-2",
    "HTTP://tickets.example.com:8080/a?b=c&d=%20#e",
    "https://user@example.com/x",
    "https://[2001:db8::1]:8443/x",
    "https://example.com/שלום",
]


def _enrich(enrichments):
    return "/alerts/enrich", {"fingerprint": FINGERPRINT, "enrichments": enrichments}


def _batch(enrichments):
    return (
        "/alerts/batch_enrich",
        {"fingerprints": [FINGERPRINT], "enrichments": enrichments},
    )


def _batch_cel(enrichments):
    return (
        "/alerts/batch_enrich",
        {"cel": "name == 'ticket'", "enrichments": enrichments},
    )


FORMS = {"enrich": _enrich, "batch": _batch, "batch_cel": _batch_cel}


def _seed(db_session, create_alert):
    setup_api_key(db_session, WEBHOOK_KEY, role="webhook")
    create_alert(FINGERPRINT, AlertStatus.FIRING, datetime.utcnow(), {"name": "ticket"})
    last_alert = db_session.query(LastAlert).one()
    last_alert.ticket_url = STORED_URL
    db_session.add(last_alert)
    db_session.commit()


def _state(db_session):
    db_session.expire_all()
    row = db_session.query(LastAlert).one()
    return row.ticket_url, row.status, db_session.query(AlertAudit).count()


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_non_http_ticket_url_is_refused_before_any_write(
    db_session, client, test_app, create_alert, form
):
    _seed(db_session, create_alert)
    before = _state(db_session)

    for bad in BAD_URLS:
        # `status` rides along to prove that nothing of the request is applied.
        path, body = FORMS[form]({"ticket_url": bad, "status": "acknowledged"})
        response = client.post(path, headers=WEBHOOK_HEADERS, json=body)

        assert response.status_code == 422, (bad, response.text)
        detail = response.json()["detail"]
        assert isinstance(detail, str) and "ticket_url" in detail
        # The refused value is not echoed back.
        if isinstance(bad, str) and bad.strip():
            assert bad not in detail
        assert _state(db_session) == before, bad


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_ticket_url_is_checked_before_the_cel_selector(
    db_session, client, test_app, create_alert
):
    _seed(db_session, create_alert)

    response = client.post(
        "/alerts/batch_enrich",
        headers=WEBHOOK_HEADERS,
        json={
            "cel": "name == 'no such alert'",
            "enrichments": {"ticket_url": "javascript:alert(1)"},
        },
    )

    assert response.status_code == 422
    assert "ticket_url" in response.json()["detail"]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_http_ticket_urls_are_stored(db_session, client, test_app, create_alert, form):
    _seed(db_session, create_alert)

    for good in GOOD_URLS:
        path, body = FORMS[form]({"ticket_url": good})
        response = client.post(path, headers=WEBHOOK_HEADERS, json=body)

        assert (response.status_code, response.json()) == (200, {"status": "ok"}), good
        assert _state(db_session)[0] == good


@pytest.mark.parametrize("empty", [None, ""])
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_ticket_url_can_still_be_cleared(
    db_session, client, test_app, create_alert, form, empty
):
    _seed(db_session, create_alert)

    path, body = FORMS[form]({"ticket_url": empty})
    response = client.post(path, headers=WEBHOOK_HEADERS, json=body)

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert _state(db_session)[0] == empty


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_unenriching_the_ticket_is_untouched(
    db_session, client, test_app, create_alert
):
    _seed(db_session, create_alert)

    response = client.post(
        "/alerts/unenrich",
        headers=WEBHOOK_HEADERS,
        json={"fingerprint": FINGERPRINT, "enrichments": ["ticket_url"]},
    )

    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert _state(db_session)[0] is None
