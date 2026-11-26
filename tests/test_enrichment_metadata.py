"""Audit description of an enrichment, with and without the Keep Assistant marker."""

import re

import pytest

from src.models.action_type import ActionType
from src.services.enrichments_bl import EnrichmentsBl
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity

USER = AuthenticatedEntity(tenant_id="t", email="ops@example.com", role="noc")
API_KEY = AuthenticatedEntity(
    tenant_id="t", email="ops@example.com", api_key_name="ci", role="webhook"
)

# keep-ui splits the note out of the description with this regex
# (src/features/alerts/alert-detail-sidebar/ui/alert-timeline.tsx).
UI_NOTE_REGEX = re.compile(r"\s*-?\s*With note:\s*(.+)$", re.IGNORECASE)

# entity, enrichments, action, main description, note suffix, checks incidents
CASES = {
    "status": (
        USER,
        {"status": "acknowledged"},
        ActionType.MANUAL_STATUS_CHANGE,
        "Alert status was changed to acknowledged by ops@example.com",
        "",
        False,
    ),
    "resolve": (
        USER,
        {"status": "resolved"},
        ActionType.MANUAL_RESOLVE,
        "Alert status was changed to resolved by ops@example.com",
        "",
        True,
    ),
    "status_and_note": (
        USER,
        {"status": "suppressed", "note": "investigating - see wiki"},
        ActionType.MANUAL_STATUS_CHANGE,
        "Alert status was changed to suppressed by ops@example.com",
        " - With note: investigating - see wiki",
        False,
    ),
    "modal_shape": (
        USER,
        {
            "status": "acknowledged",
            "dismiss_mode": None,
            "dismissed_until": None,
            "note": "on it",
        },
        ActionType.MANUAL_STATUS_CHANGE,
        "Alert status was changed to acknowledged by ops@example.com",
        " - With note: on it",
        False,
    ),
    "empty_note": (
        USER,
        {"status": "firing", "note": ""},
        ActionType.MANUAL_STATUS_CHANGE,
        "Alert status was changed to firing by ops@example.com",
        "",
        False,
    ),
    "api_key_status": (
        API_KEY,
        {"status": "acknowledged", "note": "n"},
        ActionType.API_STATUS_CHANGE,
        "Alert status was changed to acknowledged by API `ci`",
        " - With note: n",
        False,
    ),
    "api_key_resolve": (
        API_KEY,
        {"status": "resolved"},
        ActionType.API_AUTOMATIC_RESOLVE,
        "Alert status was changed to resolved by API `ci`",
        "",
        True,
    ),
    "ticket": (
        USER,
        {"ticket_url": "https://jira.example.com/1", "note": "n"},
        ActionType.TICKET_ASSIGNED,
        "Ticket assigned by ops@example.com - https://jira.example.com/1",
        " - With note: n",
        False,
    ),
    "note_only": (
        USER,
        {"note": "hello"},
        ActionType.COMMENT,
        "Comment added by ops@example.com - hello",
        "",
        False,
    ),
    "generic": (
        USER,
        {"assignee": "a@example.com"},
        ActionType.GENERIC_ENRICH,
        "Alert enriched by ops@example.com - {'assignee': 'a@example.com'}",
        "",
        False,
    ),
}


@pytest.mark.parametrize("case", CASES)
def test_descriptions_are_unchanged_without_the_marker(case):
    entity, enrichments, action, main, suffix, checks = CASES[case]

    expected = (action, main + suffix, checks)
    assert EnrichmentsBl.get_enrichment_metadata(enrichments, entity) == expected
    assert (
        EnrichmentsBl.get_enrichment_metadata(enrichments, entity, via_assistant=False)
        == expected
    )


@pytest.mark.parametrize("case", CASES)
def test_marker_comes_before_the_note(case):
    entity, enrichments, action, main, suffix, checks = CASES[case]

    assert EnrichmentsBl.get_enrichment_metadata(
        enrichments, entity, via_assistant=True
    ) == (action, f"{main} via Keep Assistant{suffix}", checks)


@pytest.mark.parametrize(
    "case", ["status_and_note", "modal_shape", "api_key_status", "ticket"]
)
def test_ui_still_extracts_only_the_note(case):
    entity, enrichments, *_ = CASES[case]

    _, description, _ = EnrichmentsBl.get_enrichment_metadata(
        enrichments, entity, via_assistant=True
    )

    match = UI_NOTE_REGEX.search(description)
    assert match.group(1) == enrichments["note"]
    assert description[: match.start()].endswith(" via Keep Assistant")


def test_marker_is_keyword_only():
    with pytest.raises(TypeError):
        EnrichmentsBl.get_enrichment_metadata({"status": "firing"}, USER, True)
