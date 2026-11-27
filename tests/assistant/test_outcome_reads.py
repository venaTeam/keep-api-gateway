"""Stored outcomes a newer version wrote: read leniently, written strictly.

A rollback leaves outcomes behind whose code, gateway result or alert result this
version does not know, plus keys it has never heard of. The conversation still loads.
"""
import pydantic
import pytest

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.models.api.draft import AlertOutcome, DraftOutcome
from src.services.assistant.models.db import Draft
from tests.assistant.conftest import TENANT
from tests.assistant.rows import EMAIL, add_conversation, add_draft, add_message, read
from tests.assistant.running import Exchange, Keep, equip

# A code no version of this one writes.
NEWER_CODE = "code_of_a_newer_version"
SHOWN_OBJECT = {
    "type": "preset",
    "id": "a-preset-id",
    "name": "Night shift",
    "path": None,
}


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    return make_app()


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture(autouse=True)
def keep(app):
    keep = Keep({"fp-1": "firing", "fp-2": "firing"})
    equip(app, gateway=keep)
    return keep


@pytest.fixture()
def conversation(db):
    return add_conversation(db)


def stored(**values) -> dict:
    """An outcome as a newer version stores it: an object, and its own raw key."""
    return {
        "code": "applied",
        "message": "The stored text.",
        "gateway_result": "ok",
        "applied_count": 0,
        "failed_count": 0,
        "unconfirmed_count": 0,
        "skipped_count": 0,
        "not_found_count": 0,
        "results": [],
        "finished_at": "2026-11-26T10:00:00+00:00",
        "object": {"type": "preset", "id": "a-preset-id", "name": "Night shift"},
        "confirm_state": {"sent_at": "2026-11-26T09:59:59+00:00", "step": 1},
        **values,
    }


def result(value: str) -> dict:
    return {"fingerprint": "fp-1", "name": None, "result": value, "status_after": None}


def with_draft(db, conversation, **values):
    draft = add_draft(db, conversation, **values)
    add_message(db, conversation, "tool", tool_call_id="1", draft_id=draft)
    return draft


async def shown(app, conversation) -> dict:
    exchange = await Exchange(
        app, "GET", f"/assistant/conversations/{conversation}"
    ).finish()
    assert exchange.status == 200, exchange.text
    [draft] = [i["draft"] for i in exchange.json()["items"] if i["type"] == "draft"]
    return draft


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["from_the_future", "change_alert_status"])
async def test_an_outcome_code_this_version_does_not_know(app, db, conversation, kind):
    draft = with_draft(
        db,
        conversation,
        kind=kind,
        state="failed",
        outcome=stored(code=NEWER_CODE, gateway_result="rejected"),
    )
    outcome = (await shown(app, conversation))["outcome"]
    assert outcome["code"] == NEWER_CODE
    assert outcome["message"] == outcomes.UNREADABLE_OUTCOME
    assert outcome["gateway_result"] == "rejected"
    assert outcome["object"] == SHOWN_OBJECT
    # Keys this version does not know are neither a failure nor shown.
    assert "confirm_state" not in outcome
    assert read(db, Draft, draft)["outcome"]["object"]["type"] == "preset"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unknown",
    [
        {"gateway_result": "conflict"},
        {"results": [result("created")], "unconfirmed_count": 1},
    ],
    ids=["gateway_result", "result"],
)
async def test_any_value_this_version_does_not_know(app, db, conversation, unknown):
    with_draft(db, conversation, state="failed", outcome=stored(**unknown))
    outcome = (await shown(app, conversation))["outcome"]
    assert outcome["message"] == outcomes.UNREADABLE_OUTCOME
    for field, value in unknown.items():
        assert outcome[field] == value


@pytest.mark.asyncio
async def test_an_outcome_it_knows_keeps_its_text(app, db, conversation):
    with_draft(db, conversation, state="applied", outcome=stored())
    outcome = (await shown(app, conversation))["outcome"]
    assert (outcome["code"], outcome["message"]) == ("applied", "The stored text.")
    assert outcome["object"] == SHOWN_OBJECT
    assert "confirm_state" not in outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_object",
    [{"type": "silence", "id": "a-silence-id"}, {"type": "preset"}, "a-preset-id"],
    ids=["unknown-type", "no-id", "not-an-object"],
)
async def test_an_object_this_version_cannot_read(
    app, db, conversation, stored_object
):
    """Left out, and the text claims nothing; the row keeps it."""
    draft = with_draft(
        db, conversation, state="applied", outcome=stored(object=stored_object)
    )
    outcome = (await shown(app, conversation))["outcome"]
    assert outcome["object"] is None
    assert (outcome["code"], outcome["message"]) == (
        "applied",
        outcomes.UNREADABLE_OUTCOME,
    )
    assert read(db, Draft, draft)["outcome"]["object"] == stored_object


# --- writing stays strict ---------------------------------------------------------------


def lenient(**values) -> DraftOutcome:
    """Past every check, as a bug would hand it over."""
    fields = {k: v for k, v in stored(**values).items() if k in DraftOutcome.__fields__}
    return DraftOutcome.construct(**fields)


@pytest.mark.parametrize(
    "unknown",
    [
        {"code": NEWER_CODE},
        {"gateway_result": "conflict"},
        {"results": [result("created")]},
        {"object": {"type": "silence", "id": "a-silence-id"}},
    ],
    ids=["code", "gateway_result", "result", "object_type"],
)
def test_the_models_refuse_what_this_version_does_not_know(unknown):
    with pytest.raises(pydantic.ValidationError):
        DraftOutcome.parse_obj(stored(**unknown))
    if "results" in unknown:
        with pytest.raises(pydantic.ValidationError):
            AlertOutcome.parse_obj(unknown["results"][0])


@pytest.mark.parametrize("write", ["keep_applying", "resolve"])
def test_writing_an_unknown_code_raises(db, conversation, write):
    draft = with_draft(db, conversation, state="applying")
    before = read(db, Draft, draft)
    key = drafts_bl.DraftKey(draft, TENANT, EMAIL, conversation)
    bad = lenient(code=NEWER_CODE)
    with pytest.raises(pydantic.ValidationError):
        if write == "keep_applying":
            drafts_bl.keep_applying(key, bad)
        else:
            drafts_bl.resolve(
                key,
                from_state="applying",
                state="failed",
                event="x",
                outcome=bad,
                by_user=False,
            )
    assert read(db, Draft, draft) == before
