"""The gateway responses keep-assistant parses, captured from the real routes.

`tests/fixtures/assistant_gateway_samples.json` is vendored byte-identical into
keep-assistant, whose client is tested against it. A failure here means a response
it depends on changed: check the consumer, then regenerate and vendor again with

    UPDATE_ASSISTANT_SAMPLES=1 poetry run pytest tests/test_assistant_gateway_samples.py

Ids and timestamps vary between runs, so they are replaced by placeholders of the
same shape. Nothing else is touched.
"""

import contextvars
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import Alert, LastAlert, LastAlertToIncident
from src.models.db.incident import Incident
from src.repositories.dependencies import GENERIC_TENANT_UUID
from tests.fixtures.client import client, setup_api_key, test_app  # noqa
from tests.fixtures.identity import TEST_JWT_SECRET, bearer_headers

SAMPLES_PATH = Path(__file__).parent / "fixtures" / "assistant_gateway_samples.json"
REQUEST_ID = "3f2c5a1e-8b7d-4c69-9a51-0d2e4f6a8b1c"
SEEDED_AT = datetime(2026, 1, 2, 3, 4, 5, 678000)

UUID_RE = re.compile(r"^[0-9a-f]{8}-?([0-9a-f]{4}-?){3}[0-9a-f]{12}$")
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"
)

# Alert fields keep-assistant reads from /alerts/query and /alerts/{fingerprint}.
CONSUMED_ALERT_FIELDS = {
    "fingerprint",
    "name",
    "status",
    "severity",
    "source",
    "environment",
    "lastReceived",
    "description",
    "note",
    "incident",
}


def _is_iso(value):
    return bool(TIMESTAMP_RE.match(value))


class _Normalizer:
    def __init__(self):
        self.ids: dict[str, str] = {}

    def __call__(self, value):
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            if UUID_RE.match(value):
                number = len(self.ids) + 1
                return self.ids.setdefault(
                    value, f"00000000-0000-4000-8000-{number:012d}"
                )
            if _is_iso(value):
                suffix = re.search(r"(Z|[+-]\d{2}:\d{2})$", value)
                return "2026-01-02T03:04:05.678" + ("Z" if suffix else "")
        return value


class _Recorder:
    """Sends requests and keeps what came back."""

    def __init__(self):
        self.samples: dict[str, dict] = {}
        self._normalize = _Normalizer()

    def __call__(self, http, name, method, path, *, headers=None, body=None):
        kwargs = {} if body is None else {"json": body}
        # Empty context: the autouse `mocked_context` fixture opens one of its own.
        response = contextvars.Context().run(
            http.request, method, path, headers=headers or {}, **kwargs
        )
        try:
            answer = response.json()
        except ValueError:
            answer = response.text
        sample = {
            "request": {"method": method, "path": path, "body": body},
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body": self._normalize(answer),
        }
        sent_id = (headers or {}).get("X-Request-ID")
        if sent_id and response.status_code != 400:
            assert response.headers["X-Request-ID"] == sent_id
        self.samples[name] = sample
        return sample


def _seed(db_session, create_alert):
    alerts = (
        (
            "fp-rich",
            {
                "name": "Database <down>",
                "severity": "critical",
                "source": ["prometheus"],
                "service": "orders",
                "environment": "production",
                "description": "connection refused",
            },
        ),
        # Colons, spaces and non-ASCII: the forms that are read one by one.
        ("fp: שלום 2", {"name": "Odd fingerprint"}),
        ("fp-bare", {"name": "Bare"}),
        ("fp-suppressed", {"name": "Suppressed", "severity": "warning"}),
    )
    # Distinct times, so that the order of the results is defined.
    for age, (fingerprint, details) in enumerate(alerts):
        timestamp = SEEDED_AT - timedelta(minutes=age)
        create_alert(fingerprint, AlertStatus.FIRING, timestamp, details)

    bare = db_session.query(Alert).filter_by(fingerprint="fp-bare").one()
    bare.source = None
    rich = db_session.query(LastAlert).filter_by(fingerprint="fp-rich").one()
    rich.note = "investigating"
    rich.assignee = "ops@example.com"
    suppressed = db_session.query(LastAlert).filter_by(fingerprint="fp-suppressed").one()
    suppressed.status = "suppressed"
    suppressed.dismiss_mode = "permanent"

    incident = Incident(
        tenant_id=GENERIC_TENANT_UUID,
        user_generated_name="Orders down",
        user_summary="",
        generated_summary="",
        alerts_count=1,
        resolve_on="never",
    )
    db_session.add(incident)
    db_session.commit()
    db_session.add(
        LastAlertToIncident(
            tenant_id=GENERIC_TENANT_UUID,
            fingerprint="fp-rich",
            incident_id=incident.id,
        )
    )
    db_session.commit()


@pytest.fixture
def recorder():
    return _Recorder()


# One app per auth setting, so the samples are captured in two tests and each test
# owns the part of the file it recorded.
def _check(recorder, part):
    stored = (
        json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
        if SAMPLES_PATH.exists()
        else {}
    )
    if os.environ.get("UPDATE_ASSISTANT_SAMPLES") == "1":
        stored[part] = recorder.samples
        # Bytes, not text: no platform line endings in a file that is compared byte
        # by byte elsewhere.
        SAMPLES_PATH.write_bytes(_render(stored))
        return
    assert stored.get(part) == recorder.samples


def _render(samples) -> bytes:
    text = json.dumps(samples, indent=2, sort_keys=True, ensure_ascii=True)
    return (text + "\n").encode("ascii")


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_captured_samples(
    db_session, client, test_app, create_alert, recorder, monkeypatch
):
    from src.routes import alerts as alerts_routes

    setup_api_key(db_session, "webhook-key", role="webhook")
    _seed(db_session, create_alert)
    noc = bearer_headers("noc", **{"X-Request-ID": REQUEST_ID})
    viewer = bearer_headers("viewer")

    # --- GET /whoami -------------------------------------------------------- #
    for role in ("noc", "viewer", "editor", "admin"):
        sample = recorder(
            client, f"whoami_{role}", "GET", "/whoami", headers=bearer_headers(role)
        )
        assert set(sample["body"]) == {"tenant_id", "role", "email", "scopes"}
    recorder(
        client,
        "whoami_invalid_token",
        "GET",
        "/whoami",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    recorder(client, "whoami_no_credentials", "GET", "/whoami")
    recorder(
        client,
        "whoami_forbidden",
        "GET",
        "/whoami",
        headers={"x-api-key": "webhook-key"},
    )

    # --- POST /alerts/query ------------------------------------------------- #
    for name, body in (
        ("query_all", {"cel": "", "limit": 21}),
        (
            "query_by_fingerprints",
            {"cel": 'fingerprint in ["fp-rich", "fp-missing"]', "limit": 21},
        ),
        ("query_no_match", {"cel": "name == 'no such alert'", "limit": 21}),
        ("query_syntax_error", {"cel": "name ==", "limit": 21}),
        ("query_unknown_field", {"cel": "nosuchfield == 'x'", "limit": 21}),
        ("query_not_a_filter", {"cel": "'just text'", "limit": 21}),
        ("query_bad_request", {"cel": "", "limit": "many"}),
    ):
        recorder(client, name, "POST", "/alerts/query", headers=noc, body=body)
    results = recorder.samples["query_all"]["body"]["results"]
    assert len(results) == 4
    for alert in results:
        assert CONSUMED_ALERT_FIELDS <= set(alert)
    by_fingerprint = {alert["fingerprint"]: alert for alert in results}
    # Incidents come as a (truthy) value here, and only here.
    assert by_fingerprint["fp-rich"]["incident"]
    assert not by_fingerprint["fp-bare"]["incident"]

    # --- POST /alerts/query/count ------------------------------------------- #
    sample = recorder(
        client,
        "count",
        "POST",
        "/alerts/query/count",
        headers=noc,
        body={"cel": "severity == 'critical'"},
    )
    assert type(sample["body"]) is int
    recorder(
        client,
        "count_invalid_cel",
        "POST",
        "/alerts/query/count",
        headers=noc,
        body={"cel": "name =="},
    )

    # --- GET /alerts/{fingerprint} ------------------------------------------ #
    for name, path in (
        ("alert", "/alerts/fp-rich"),
        ("alert_bare", "/alerts/fp-bare"),
        (
            "alert_encoded_fingerprint",
            "/alerts/fp%3A%20%D7%A9%D7%9C%D7%95%D7%9D%202",
        ),
        ("alert_not_found", "/alerts/fp-missing"),
        # %2F is decoded before routing: another route, or none.
        ("alert_slash_not_routed", "/alerts/fp%2Frich"),
        ("alert_slash_other_route", "/alerts/fp-rich%2Fhistory"),
    ):
        recorder(client, name, "GET", path, headers=noc)
    alert = recorder.samples["alert"]["body"]
    assert alert["fingerprint"] == "fp-rich"
    assert CONSUMED_ALERT_FIELDS <= set(alert)
    # Never set on this route, although the alert is in an incident.
    assert alert["incident"] is None
    recorder(client, "alert_unauthenticated", "GET", "/alerts/fp-rich")

    # --- GET /alerts/facets/fields ------------------------------------------ #
    fields = recorder(
        client, "facet_fields", "GET", "/alerts/facets/fields", headers=noc
    )["body"]
    assert fields and all(isinstance(field, str) for field in fields)

    # --- POST /alerts/batch_enrich ------------------------------------------ #
    path = "/alerts/batch_enrich?dispose_on_new_alert=false"
    assistant = {**noc, "X-Keep-Source": "assistant"}
    write = {
        "fingerprints": ["fp-rich"],
        "enrichments": {
            "status": "acknowledged",
            "dismiss_mode": None,
            "dismissed_until": None,
            "note": "on it",
        },
    }

    def batch(name, body, headers=assistant):
        return recorder(client, name, "POST", path, headers=headers, body=body)

    batch("batch_enrich_ok", write)
    batch(
        "batch_enrich_suppress_ok",
        {"fingerprints": ["fp-bare"], "enrichments": {"status": "suppressed"}},
    )
    # Writes nothing, and says ok: the re-read is what tells.
    batch("batch_enrich_unknown_fingerprint", {**write, "fingerprints": ["fp-missing"]})
    recorder(
        client,
        "query_after_write",
        "POST",
        "/alerts/query",
        headers=noc,
        body={"cel": 'fingerprint in ["fp-rich"]', "limit": 21},
    )
    (after,) = recorder.samples["query_after_write"]["body"]["results"]
    assert (after["status"], after["note"]) == ("acknowledged", "on it")

    batch("batch_enrich_not_permitted", write, headers=viewer)
    batch(
        "batch_enrich_delete_not_permitted",
        {"fingerprints": ["fp-rich"], "enrichments": {"deleted": True}},
    )
    batch("batch_enrich_unauthenticated", write, headers={})
    batch(
        "batch_enrich_invalid_token", write, headers={"Authorization": "Bearer nope"}
    )
    batch("batch_enrich_no_selector", {"enrichments": {"status": "firing"}})
    batch(
        "batch_enrich_unknown_key",
        {"fingerprints": ["fp-rich"], "enrichments": {"no_such_key": 1}},
    )
    batch(
        "batch_enrich_unsafe_ticket_url",
        {
            "fingerprints": ["fp-rich"],
            "enrichments": {"ticket_url": "javascript:alert(1)"},
        },
    )
    batch("batch_enrich_malformed", {"fingerprints": "fp-rich"})
    batch(
        "batch_enrich_bad_request_id",
        write,
        headers={**assistant, "X-Request-ID": "not-a-uuid"},
    )

    async def fail(*args, **kwargs):
        raise RuntimeError("db went away")

    monkeypatch.setattr(alerts_routes.EnrichmentsBl, "batch_enrich", fail)
    batch("batch_enrich_failed", write)

    # The classification the consumer relies on, spelled out.
    for name, status, detail_type in (
        ("query_syntax_error", 400, dict),
        ("query_unknown_field", 400, dict),
        ("query_not_a_filter", 400, dict),
        ("query_bad_request", 422, list),
        ("batch_enrich_not_permitted", 403, str),
        ("batch_enrich_delete_not_permitted", 403, str),
        ("batch_enrich_unauthenticated", 401, str),
        ("batch_enrich_invalid_token", 401, str),
        ("batch_enrich_no_selector", 400, str),
        ("batch_enrich_unknown_key", 422, str),
        ("batch_enrich_unsafe_ticket_url", 422, str),
        ("batch_enrich_malformed", 422, list),
    ):
        sample = recorder.samples[name]
        assert sample["status"] == status, name
        assert type(sample["body"]["detail"]) is detail_type, name
    for name, expected in (
        ("batch_enrich_ok", {"status": "ok"}),
        ("batch_enrich_suppress_ok", {"status": "ok"}),
        ("batch_enrich_unknown_fingerprint", {"status": "ok"}),
        ("batch_enrich_failed", {"status": "failed"}),
    ):
        assert recorder.samples[name]["status"] == 200, name
        assert recorder.samples[name]["body"] == expected, name
    bad_id = recorder.samples["batch_enrich_bad_request_id"]
    assert (bad_id["status"], bad_id["body"]) == (400, "")

    _check(recorder, "read_write")


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "SINGLE_TENANT",
            "KEEP_JWT_SECRET": TEST_JWT_SECRET,
            "KEEP_READ_ONLY": "true",
            "KEEP_READ_ONLY_BYPASS_KEY": "bypass",
        }
    ],
    indirect=True,
)
def test_captured_read_only_samples(db_session, client, test_app, recorder):
    headers = bearer_headers("noc")
    recorder(client, "whoami", "GET", "/whoami", headers=headers)
    sample = recorder(
        client,
        "batch_enrich",
        "POST",
        "/alerts/batch_enrich",
        headers=headers,
        body={"fingerprints": ["fp"], "enrichments": {"status": "acknowledged"}},
    )

    assert recorder.samples["whoami"]["status"] == 200
    assert sample["status"] == 403
    # What tells this 403 from a missing permission.
    assert "Read only" in sample["body"]["detail"]
    _check(recorder, "read_only")


def test_samples_file_is_canonical():
    """Same content, same bytes: keep-assistant compares its copy byte by byte."""
    raw = SAMPLES_PATH.read_bytes()
    # A Windows checkout may have converted the line endings.
    assert raw.replace(b"\r\n", b"\n") == _render(json.loads(raw))


def test_samples_hold_nothing_volatile():
    normalize = _Normalizer()

    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    for text in strings(json.loads(SAMPLES_PATH.read_bytes())):
        if UUID_RE.match(text):
            assert text.startswith("00000000-0000-4000-8000-"), text
        elif _is_iso(text):
            assert normalize(text) == text, text
