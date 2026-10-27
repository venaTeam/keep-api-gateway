from datetime import datetime

import pytest

from src.models.alert import AlertStatus
from src.models.db.alert import Alert, AlertAudit, LastAlert
from src.repositories.db import delete_alert
from src.repositories.dependencies import GENERIC_TENANT_UUID
from tests.fixtures.client import client, setup_api_key, test_app  # noqa


def _count(db_session, model, fingerprint, tenant_id=GENERIC_TENANT_UUID) -> int:
    db_session.expire_all()
    return (
        db_session.query(model)
        .filter(model.tenant_id == tenant_id, model.fingerprint == fingerprint)
        .count()
    )


def _delete(client, fingerprint, **body):
    return client.request(
        "DELETE",
        "/alerts",
        headers={"x-api-key": "some-key"},
        json={
            "fingerprint": fingerprint,
            "lastReceived": datetime.utcnow().isoformat(),
            **body,
        },
    )


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_soft_delete_keeps_the_rows(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("alert-soft", AlertStatus.FIRING, datetime.utcnow(), {"name": "Soft"})

    response = _delete(client, "alert-soft")

    assert response.status_code == 200
    assert _count(db_session, Alert, "alert-soft") == 1
    last_alert = db_session.query(LastAlert).filter_by(fingerprint="alert-soft").one()
    assert last_alert.deleted is True


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize("elastic_client", [False], indirect=True)
def test_hard_delete_removes_the_rows(
    db_session, client, test_app, create_alert, elastic_client
):
    create_alert("alert-hard", AlertStatus.FIRING, datetime.utcnow(), {"name": "Hard"})
    create_alert("alert-kept", AlertStatus.FIRING, datetime.utcnow(), {"name": "Kept"})

    response = _delete(client, "alert-hard", soft_delete=False)

    assert response.status_code == 200
    for model in (Alert, LastAlert, AlertAudit):
        assert _count(db_session, model, "alert-hard") == 0
    assert _count(db_session, Alert, "alert-kept") == 1
    assert _count(db_session, LastAlert, "alert-kept") == 1


def test_hard_delete_is_scoped_to_the_tenant(db_session, create_alert):
    """The same fingerprint under another tenant must survive."""
    other_tenant = "other-tenant"
    create_alert("alert-shared", AlertStatus.FIRING, datetime.utcnow(), {"name": "Mine"})
    create_alert(
        "alert-shared",
        AlertStatus.FIRING,
        datetime.utcnow(),
        {"name": "Theirs"},
        tenant_id=other_tenant,
    )

    delete_alert(GENERIC_TENANT_UUID, "alert-shared", session=db_session)

    assert _count(db_session, Alert, "alert-shared") == 0
    assert _count(db_session, LastAlert, "alert-shared") == 0
    assert _count(db_session, Alert, "alert-shared", other_tenant) == 1
    assert _count(db_session, LastAlert, "alert-shared", other_tenant) == 1
