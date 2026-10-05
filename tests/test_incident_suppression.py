"""Incident suppression is derived from dismiss state, never stored in `status`.

`IncidentStatus.SUPPRESSED` has no stored representation: the column keeps the
status the incident reverts to, and `suppressed` is computed from
`dismiss_mode`/`dismissed_until` every time it is read. That is what lets a
time-boxed dismissal lapse on its own clock with no sweeper job — and it is why
these tests care so much about what the *column* holds versus what callers see.

`IncidentStatus.DELETED` is gone; deleting an incident deletes the row.
"""

import datetime

import pytest
from pydantic import ValidationError

from src.models.alert import (
    EnrichIncidentRequestBody,
    UnEnrichIncidentRequestBody,
)
from src.models.db.incident import (
    Incident,
    IncidentDismissMode,
    IncidentSeverity,
    IncidentStatus,
)
from src.models.incident import (
    IncidentDto,
    IncidentStatusChangeDto,
    split_incident_status_keys,
)


def _incident(**kwargs) -> Incident:
    defaults = dict(
        tenant_id="test",
        user_generated_name="test-incident",
        ai_generated_name=None,
        user_summary="",
        generated_summary="",
        status=IncidentStatus.FIRING.value,
        severity=IncidentSeverity.CRITICAL.order,
        alerts_count=0,
        affected_services=[],
        sources=[],
    )
    defaults.update(kwargs)
    return Incident(**defaults)


def _future(hours=2) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)


def _past(hours=2) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)


# === the status enum itself ===


def test_deleted_status_is_gone():
    assert not hasattr(IncidentStatus, "DELETED")
    assert "deleted" not in [s.value for s in IncidentStatus]


def test_suppressed_status_exists():
    assert IncidentStatus.SUPPRESSED.value == "suppressed"


def test_deleted_no_longer_counted_as_closed():
    assert IncidentStatus.get_closed(return_values=True) == ["resolved", "merged"]


def test_suppressed_is_neither_active_nor_closed():
    # A suppressed incident keeps its underlying firing/acknowledged status, so
    # lifecycle transitions (e.g. auto-resolve) still apply to it while hidden.
    assert IncidentStatus.SUPPRESSED not in IncidentStatus.get_active()
    assert IncidentStatus.SUPPRESSED not in IncidentStatus.get_closed()


# === derivation on the DB model ===


def test_no_dismiss_state_means_stored_status():
    incident = _incident(status=IncidentStatus.FIRING.value)
    assert incident.is_dismiss_active() is False
    assert incident.get_effective_status() == "firing"


def test_permanent_dismissal_derives_suppressed():
    incident = _incident(dismiss_mode=IncidentDismissMode.PERMANENT.value)
    assert incident.is_dismiss_active() is True
    assert incident.get_effective_status() == "suppressed"


def test_permanent_dismissal_ignores_dismissed_until():
    # A stale deadline left over from an earlier time-boxed dismissal must not
    # expire a dismissal that was since made permanent.
    incident = _incident(
        dismiss_mode=IncidentDismissMode.PERMANENT.value,
        dismissed_until=_past(),
    )
    assert incident.get_effective_status() == "suppressed"


def test_dismiss_until_in_the_future_derives_suppressed():
    incident = _incident(
        status=IncidentStatus.ACKNOWLEDGED.value,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL.value,
        dismissed_until=_future(),
    )
    assert incident.get_effective_status() == "suppressed"


def test_dismiss_until_in_the_past_reverts_without_a_write():
    # The whole point of deriving: the row still says dismiss_until, nothing has
    # touched it, and the incident is already back to its underlying status.
    incident = _incident(
        status=IncidentStatus.ACKNOWLEDGED.value,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL.value,
        dismissed_until=_past(),
    )
    assert incident.is_dismiss_active() is False
    assert incident.get_effective_status() == "acknowledged"
    assert incident.dismiss_mode == IncidentDismissMode.DISMISS_UNTIL.value


def test_dismiss_until_without_a_deadline_is_not_active():
    incident = _incident(dismiss_mode=IncidentDismissMode.DISMISS_UNTIL.value)
    assert incident.is_dismiss_active() is False


def test_naive_deadline_is_read_as_utc():
    # SQLite round-trips the timezone-aware column as naive; the comparison must
    # not raise on it.
    naive = (_future()).replace(tzinfo=None)
    incident = _incident(
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL.value,
        dismissed_until=naive,
    )
    assert incident.is_dismiss_active() is True


def test_now_argument_is_honoured():
    incident = _incident(
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL.value,
        dismissed_until=_future(hours=1),
    )
    assert incident.is_dismiss_active(now=_past()) is True
    assert incident.is_dismiss_active(now=_future(hours=5)) is False


# === the DTO callers actually see ===


def test_dto_reports_suppressed_while_dismissed():
    incident = _incident(
        status=IncidentStatus.FIRING.value,
        dismiss_mode=IncidentDismissMode.PERMANENT.value,
    )
    dto = IncidentDto.from_db_incident(incident)
    assert dto.status == IncidentStatus.SUPPRESSED
    assert dto.dismiss_mode == IncidentDismissMode.PERMANENT


def test_dto_reports_underlying_status_once_expired():
    incident = _incident(
        status=IncidentStatus.FIRING.value,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL.value,
        dismissed_until=_past(),
    )
    dto = IncidentDto.from_db_incident(incident)
    assert dto.status == IncidentStatus.FIRING
    # The lapsed deadline stays visible so the UI can say when it ended.
    assert dto.dismissed_until is not None


def test_live_dismissal_outranks_enrichment_status_override():
    # The enrichment JSONB can carry a `status` key that overlays the DTO. A live
    # dismissal must still win, matching the COALESCE order CEL compiles to.
    incident = _incident(
        status=IncidentStatus.FIRING.value,
        dismiss_mode=IncidentDismissMode.PERMANENT.value,
    )
    incident.set_enrichments({"status": "acknowledged"})
    dto = IncidentDto.from_db_incident(incident)
    assert dto.status == IncidentStatus.SUPPRESSED


def test_enrichment_status_key_cannot_shadow_the_column():
    # Pre-migration rows still carry these keys. They must not overlay the typed
    # columns, or an old enrichment would pin the incident to a stale status.
    incident = _incident(status=IncidentStatus.RESOLVED.value)
    incident.set_enrichments(
        {
            "status": "firing",
            "dismiss_mode": "dismiss_until",
            "dismissed_until": "2026-01-01T00:00:00.000Z",
            "note": "kept",
        }
    )
    dto = IncidentDto.from_db_incident(incident)
    assert dto.status == IncidentStatus.RESOLVED
    assert dto.dismiss_mode is None
    assert dto.dismissed_until is None
    # Everything else in the blob still overlays as before.
    assert dto.note == "kept"


def test_unrelated_enrichments_still_overlay():
    incident = _incident()
    incident.set_enrichments({"note": "hello", "ticket_url": "http://x"})
    dto = IncidentDto.from_db_incident(incident)
    assert dto.note == "hello"
    assert dto.ticket_url == "http://x"


def test_to_db_incident_never_stores_suppressed():
    incident = _incident(dismiss_mode=IncidentDismissMode.PERMANENT.value)
    dto = IncidentDto.from_db_incident(incident)
    assert dto.status == IncidentStatus.SUPPRESSED

    round_tripped = dto.to_db_incident()
    assert round_tripped.status == IncidentStatus.FIRING.value
    assert round_tripped.dismiss_mode == IncidentDismissMode.PERMANENT.value


# === request validation ===


def _change(**kwargs):
    return IncidentStatusChangeDto(comment=None, **kwargs)


def test_suppress_defaults_to_permanent():
    change = _change(status=IncidentStatus.SUPPRESSED)
    assert change.dismiss_mode == IncidentDismissMode.PERMANENT
    assert change.dismissed_until is None


def test_permanent_suppress_drops_a_supplied_deadline():
    change = _change(
        status=IncidentStatus.SUPPRESSED,
        dismiss_mode=IncidentDismissMode.PERMANENT,
        dismissed_until=_future(),
    )
    assert change.dismissed_until is None


def test_dismiss_until_requires_a_deadline():
    with pytest.raises(ValidationError, match="dismissed_until is required"):
        _change(
            status=IncidentStatus.SUPPRESSED,
            dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
        )


def test_dismiss_until_rejects_a_past_deadline():
    with pytest.raises(ValidationError, match="must be in the future"):
        _change(
            status=IncidentStatus.SUPPRESSED,
            dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
            dismissed_until=_past(),
        )


def test_naive_deadline_is_accepted_as_utc():
    change = _change(
        status=IncidentStatus.SUPPRESSED,
        dismiss_mode=IncidentDismissMode.DISMISS_UNTIL,
        dismissed_until=_future().replace(tzinfo=None),
    )
    assert change.dismissed_until.tzinfo is not None


def test_dismiss_fields_rejected_on_a_non_suppressed_status():
    with pytest.raises(ValidationError, match="only valid with"):
        _change(
            status=IncidentStatus.FIRING,
            dismiss_mode=IncidentDismissMode.PERMANENT,
        )


def test_plain_status_change_needs_no_dismiss_fields():
    change = _change(status=IncidentStatus.RESOLVED)
    assert change.dismiss_mode is None
    assert change.dismissed_until is None


# === the single-call split: one body carrying a dismissal and a note ===


def test_split_returns_no_status_change_for_plain_enrichments():
    change, rest = split_incident_status_keys({"note": "ss", "ticket_url": "http://x"})
    assert change is None
    assert rest == {"note": "ss", "ticket_url": "http://x"}


def test_split_separates_a_dismissal_from_its_note():
    # The payload the UI actually sends when dismissing with a note.
    until = _future()
    change, rest = split_incident_status_keys(
        {
            "note": "ss",
            "status": "suppressed",
            "dismiss_mode": "dismiss_until",
            "dismissed_until": until.isoformat(),
        }
    )
    assert rest == {"note": "ss"}
    assert change.status == IncidentStatus.SUPPRESSED
    assert change.dismiss_mode == IncidentDismissMode.DISMISS_UNTIL
    assert change.dismissed_until is not None


def test_split_handles_a_status_change_with_no_enrichments():
    change, rest = split_incident_status_keys({"status": "resolved"})
    assert rest == {}
    assert change.status == IncidentStatus.RESOLVED


def test_split_defaults_a_bare_suppress_to_permanent():
    change, _ = split_incident_status_keys({"status": "suppressed"})
    assert change.dismiss_mode == IncidentDismissMode.PERMANENT


def test_split_applies_the_same_validation_as_the_status_endpoint():
    # A bad dismissal must be caught before either half is written, using the
    # status DTO's rules rather than a looser second copy of them.
    with pytest.raises(ValidationError, match="dismissed_until is required"):
        split_incident_status_keys(
            {"status": "suppressed", "dismiss_mode": "dismiss_until"}
        )
    with pytest.raises(ValidationError, match="must be in the future"):
        split_incident_status_keys(
            {
                "status": "suppressed",
                "dismiss_mode": "dismiss_until",
                "dismissed_until": _past().isoformat(),
            }
        )


def test_split_rejects_dismiss_keys_with_no_status():
    # dismiss_mode alone is ambiguous: dismissal IS the suppressed status.
    with pytest.raises(ValueError, match="require an accompanying"):
        split_incident_status_keys({"note": "ss", "dismiss_mode": "permanent"})


def test_enrich_body_accepts_a_mixed_payload():
    # No longer rejected — the route splits it instead.
    body = EnrichIncidentRequestBody(
        enrichments={
            "note": "ss",
            "status": "suppressed",
            "dismiss_mode": "permanent",
        }
    )
    assert body.enrichments["status"] == "suppressed"


@pytest.mark.parametrize("key", ["status", "dismiss_mode", "dismissed_until"])
def test_unenrich_body_rejects_status_owned_keys(key):
    with pytest.raises(ValidationError, match="not enrichments"):
        UnEnrichIncidentRequestBody(enrichments=[key], fingerprint="f")


def test_unenrich_body_allows_ordinary_keys():
    body = UnEnrichIncidentRequestBody(enrichments=["note"], fingerprint="f")
    assert body.enrichments == ["note"]
