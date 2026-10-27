"""Enrichment request bodies must accept null values.

The dismiss/restore and change-status UI flows clear typed columns by sending
`dismiss_mode: null` / `dismissed_until: null`. The request model therefore can't
constrain values to `str` (Pydantic v1 rejects None for a `str` field with
"none is not an allowed value"); it uses `dict[str, Any]` so null-to-clear works.
"""

import pytest

from src.models.alert import BatchEnrichAlertRequestBody, EnrichAlertRequestBody


def test_enrich_body_accepts_null_dismiss_fields():
    body = EnrichAlertRequestBody(
        enrichments={"status": "acknowledged", "dismiss_mode": None, "dismissed_until": None},
        fingerprint="fp-1",
    )
    assert body.enrichments["dismiss_mode"] is None
    assert body.enrichments["dismissed_until"] is None
    assert body.enrichments["status"] == "acknowledged"


def test_batch_enrich_body_accepts_null_dismiss_fields():
    body = BatchEnrichAlertRequestBody(
        enrichments={"dismiss_mode": None, "dismissed_until": None, "note": "x"},
        fingerprints=["fp-1", "fp-2"],
    )
    assert body.enrichments["dismiss_mode"] is None
    assert body.enrichments["dismissed_until"] is None
