"""`deleted` is a soft delete, so writing it needs `delete:alert` on every route.

`POST /alerts/enrich`, `/alerts/batch_enrich` and `/alerts/unenrich` are gated by
`write:alert` only, but each of them can set or clear the soft-delete flag that
`DELETE /alerts` protects with `delete:alert`. The webhook role (API keys) has
`write:alert` and not `delete:alert`, so it is the role that tells the two apart.
"""

from datetime import datetime

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import AlertAudit, LastAlert
from src.repositories.dependencies import GENERIC_TENANT_UUID
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from tests.fixtures.client import client, setup_api_key, test_app  # noqa
from tests.fixtures.identity import bearer_headers

FINGERPRINT = "fp-soft-delete"
WEBHOOK_KEY = "webhook-key"
WEBHOOK_HEADERS = {"x-api-key": WEBHOOK_KEY}


def _enrich(**enrichments):
    return "/alerts/enrich", {"fingerprint": FINGERPRINT, "enrichments": enrichments}


def _batch(**enrichments):
    return (
        "/alerts/batch_enrich",
        {"fingerprints": [FINGERPRINT], "enrichments": enrichments},
    )


def _batch_cel(cel, **enrichments):
    return "/alerts/batch_enrich", {"cel": cel, "enrichments": enrichments}


def _unenrich(*keys):
    return "/alerts/unenrich", {"fingerprint": FINGERPRINT, "enrichments": list(keys)}


# Every way a request can carry the `deleted` key.
DELETED_WRITES = {
    "enrich-true": _enrich(deleted=True),
    "enrich-false": _enrich(deleted=False),
    "enrich-null": _enrich(deleted=None),
    "enrich-mixed": _enrich(status="acknowledged", deleted=True),
    "enrich-legacy-dismiss": _enrich(dismissed="true", deleted=True),
    "batch-true": _batch(deleted=True),
    "batch-false": _batch(deleted=False),
    "batch-null": _batch(deleted=None),
    "batch-mixed": _batch(status="acknowledged", deleted=True),
    "batch-cel": _batch_cel("name == 'soft-delete'", deleted=True),
    # The permission answer comes before the selector is even looked at.
    "batch-invalid-cel": _batch_cel("name ==", deleted=True),
    "unenrich": _unenrich("deleted"),
    "unenrich-mixed": _unenrich("note", "deleted"),
}


def _seed(db_session, create_alert, *, deleted: bool) -> None:
    create_alert(
        FINGERPRINT, AlertStatus.FIRING, datetime.utcnow(), {"name": "soft-delete"}
    )
    last_alert = db_session.query(LastAlert).one()
    last_alert.deleted = deleted
    last_alert.note = "keep me"
    db_session.add(last_alert)
    db_session.commit()


def _state(db_session) -> tuple:
    """Everything a refused write must leave alone."""
    db_session.expire_all()
    row = db_session.query(LastAlert).one()
    return (
        row.deleted,
        row.status,
        row.note,
        row.dismiss_mode,
        row.status_disposable,
        db_session.query(AlertAudit).count(),
    )


@pytest.mark.parametrize("write", DELETED_WRITES)
@pytest.mark.parametrize("already_deleted", [False, True])
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_webhook_key_cannot_write_deleted(
    db_session, client, test_app, create_alert, write, already_deleted
):
    path, body = DELETED_WRITES[write]
    setup_api_key(db_session, WEBHOOK_KEY, role="webhook")
    _seed(db_session, create_alert, deleted=already_deleted)
    before = _state(db_session)

    response = client.post(path, headers=WEBHOOK_HEADERS, json=body)

    assert response.status_code == 403, response.text
    assert "delete:alert" in response.json()["detail"]
    assert _state(db_session) == before


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_webhook_key_still_writes_everything_else(
    db_session, client, test_app, create_alert
):
    setup_api_key(db_session, WEBHOOK_KEY, role="webhook")
    _seed(db_session, create_alert, deleted=False)

    for path, body in (
        _enrich(status="acknowledged"),
        _batch(status="suppressed"),
        _unenrich("status"),
    ):
        response = client.post(path, headers=WEBHOOK_HEADERS, json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok"}

    deleted, status, note, _, _, audits = _state(db_session)
    assert (deleted, status, note, audits) == (False, None, "keep me", 3)


@pytest.mark.parametrize("role", ["editor", "admin"])
@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_roles_with_delete_scope_still_write_deleted(
    db_session, client, test_app, create_alert, role
):
    _seed(db_session, create_alert, deleted=False)
    headers = bearer_headers(role)

    for (path, body), expected in (
        (_enrich(deleted=True), True),
        (_unenrich("deleted"), False),
        (_batch(deleted=True), True),
    ):
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok"}
        assert _state(db_session)[0] is expected


@pytest.mark.parametrize("test_app", ["SINGLE_TENANT"], indirect=True)
def test_guard_uses_rbac_scopes_in_multi_org_keycloak(
    db_session, test_app, monkeypatch
):
    """Multi-org Keycloak enforces rbac scopes, so the guard holds there too."""
    from fastapi import HTTPException

    from src.routes import alerts as alerts_routes
    from src.services.identity_manager.identity_managers.keycloak.keycloak_authverifier import (
        KeycloakAuthVerifier,
    )

    verifier = KeycloakAuthVerifier.__new__(KeycloakAuthVerifier)
    verifier.keycloak_multi_org = True
    verifier.scopes = ["delete:alert"]
    verifier.logger = alerts_routes.logger
    monkeypatch.setattr(alerts_routes, "_delete_alert_verifier", verifier)

    def entity(role):
        return AuthenticatedEntity(
            tenant_id=GENERIC_TENANT_UUID, email="u@example.com", role=role
        )

    for role in ("noc", "viewer", "webhook"):
        with pytest.raises(HTTPException) as exc_info:
            alerts_routes._require_delete_scope(entity(role), {"deleted": False})
        assert exc_info.value.status_code == 403
        assert "delete:alert" in exc_info.value.detail
        # Any other key is none of this guard's business.
        alerts_routes._require_delete_scope(entity(role), {"status": "resolved"})

    for role in ("editor", "admin", "superadmin"):
        alerts_routes._require_delete_scope(entity(role), ["deleted"])
