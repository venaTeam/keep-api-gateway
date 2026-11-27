"""Stored outcomes a newer version wrote: read leniently, written strictly.

A rollback leaves outcomes behind whose code, gateway result or alert result this
version does not know, plus keys it has never heard of. The conversation still loads.
"""
import ast
import json
import uuid
from pathlib import Path

import pydantic
import pytest

from src.services.assistant.bl import drafts_bl, outcomes
from src.services.assistant.models.api.draft import AlertOutcome, DraftOutcome
from src.services.assistant.models.db import Draft
from tests.assistant import golden_stream
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
async def test_an_object_type_this_version_does_not_know(app, db, conversation):
    """Shown as it is, never left out: what the newer version saved is still there."""
    stored_object = {"type": "silence", "id": "a-silence-id", "name": "Night"}
    draft = with_draft(
        db, conversation, state="applied", outcome=stored(object=stored_object)
    )
    outcome = (await shown(app, conversation))["outcome"]
    assert outcome["object"] == {**stored_object, "path": None}
    assert (outcome["code"], outcome["message"]) == (
        "applied",
        outcomes.UNREADABLE_OUTCOME,
    )
    assert read(db, Draft, draft)["outcome"]["object"] == stored_object


@pytest.mark.asyncio
async def test_no_link_for_an_object_type_this_version_does_not_know(
    app, db, conversation
):
    """Where such a page is, if anywhere, only the newer version knows: the view has
    no path, and the stored row keeps it."""
    incident = str(uuid.uuid4())
    stored_object = {
        "type": "incident",
        "id": incident,
        "name": "x",
        "path": f"/incidents/{incident}",
    }
    draft = with_draft(
        db, conversation, state="applied", outcome=stored(object=stored_object)
    )
    outcome = (await shown(app, conversation))["outcome"]
    assert outcome["message"] == outcomes.UNREADABLE_OUTCOME
    assert outcome["object"] == {**stored_object, "path": None}
    assert read(db, Draft, draft)["outcome"]["object"] == stored_object


def test_an_object_type_this_version_knows_keeps_its_link():
    preset = str(uuid.uuid4())
    saved = {"type": "preset", "id": preset, "name": "x", "path": f"/alerts/{preset}"}
    outcome = outcomes.readable(stored(object=saved))
    assert outcome.object.path == saved["path"]
    assert outcome.message == "The stored text."


@pytest.mark.asyncio
async def test_a_draft_of_a_newer_version(app, db, conversation):
    """The one of the fixture's `future` section, as a row: every value it holds is
    kept, and the text claims nothing."""
    newer = json.loads(golden_stream.FIXTURE.read_bytes())["future"]["drafts"]["applied"]
    with_draft(
        db,
        conversation,
        kind=newer["kind"],
        state=newer["state"],
        payload=newer["payload"],
        outcome=newer["outcome"],
    )
    draft = await shown(app, conversation)
    assert (draft["kind"], draft["state"]) == (newer["kind"], newer["state"])
    assert draft["payload"] == newer["payload"]
    assert draft["outcome"] == {
        **newer["outcome"],
        "message": outcomes.UNREADABLE_OUTCOME,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_object",
    [{"type": "preset"}, {"id": "a-preset-id"}, "a-preset-id"],
    ids=["no-id", "no-type", "not-an-object"],
)
async def test_an_object_this_version_cannot_read(
    app, db, conversation, stored_object
):
    """Without the shape of an object: left out, and the text claims nothing; the row
    keeps it."""
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


@pytest.mark.parametrize(
    "unknown",
    [{"code": NEWER_CODE}, {"object": {"type": "silence", "id": "a-silence-id"}}],
    ids=["code", "object_type"],
)
@pytest.mark.parametrize("write", ["keep_applying", "resolve"])
def test_writing_an_unknown_code_raises(db, conversation, write, unknown):
    draft = with_draft(db, conversation, state="applying")
    before = read(db, Draft, draft)
    key = drafts_bl.DraftKey(draft, TENANT, EMAIL, conversation)
    bad = lenient(**unknown)
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


def test_only_reading_gives_the_text_that_claims_nothing():
    """`fully_readable` tells by that text: no code but the reading may name it."""
    package = Path(outcomes.__file__).parents[1]
    users = {}
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(node, ast.Name) and node.id == "UNREADABLE_OUTCOME"
                for node in ast.walk(function)
            ):
                users.setdefault(path.name, set()).add(function.name)
    assert users == {"outcomes.py": {"readable", "fully_readable"}}
