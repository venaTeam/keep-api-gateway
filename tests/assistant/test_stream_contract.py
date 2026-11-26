"""The API contract shared with keep-ui, pinned by the golden fixture."""
import json
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from src.services.assistant.config import get_settings
from src.services.assistant.exceptions import ERRORS
from src.services.assistant.models.api import stream as s
from src.services.assistant.models.api.chat import ChatRequest
from src.services.assistant.models.api.conversation import (
    ConversationView,
    TimelineDraft,
    TimelineMessage,
)
from src.services.assistant.kinds.registry import PAYLOAD_MODELS
from src.services.assistant.models.api.draft import (
    AlertOutcome,
    ChangeAlertStatusPayload,
    DraftAlert,
    DraftOutcome,
    DraftView,
    SkippedAlert,
)
from tests.assistant import golden_stream
from tests.assistant.fakes import TOKEN

GOLDEN = json.loads(golden_stream.FIXTURE.read_bytes())
EVENTS_BY_NAME = {event.event: event for event in s.STREAM_EVENTS}


def parse_sse(raw: str):
    """Reference parser: (event, data) pairs of the complete frames."""
    frames = raw.split("\n\n")
    parsed = []
    for frame in frames[:-1]:  # the last piece is empty or an incomplete frame
        fields = dict(
            line.split(": ", 1) for line in frame.split("\n") if not line.startswith(":")
        )
        if fields:
            parsed.append({"event": fields["event"], "data": json.loads(fields["data"])})
    return parsed


# --- the golden file itself --------------------------------------------------


def test_golden_fixture_matches_the_models_byte_for_byte():
    committed = golden_stream.FIXTURE.read_bytes()
    assert b"\r" not in committed
    assert committed == golden_stream.render(), (
        "contract change: run `poetry run python -m "
        "tests.assistant.golden_stream` and vendor the file into keep-ui"
    )


@pytest.mark.parametrize("name", sorted(GOLDEN["streams"]))
def test_raw_streams_parse_to_their_events(name):
    stream = GOLDEN["streams"][name]
    assert parse_sse(stream["raw"]) == stream["events"]


@pytest.mark.parametrize("name", sorted(GOLDEN["streams"]))
def test_events_validate_and_order_holds(name):
    events = GOLDEN["streams"][name]["events"]
    for item in events:
        model = EVENTS_BY_NAME[item["event"]].parse_obj(item["data"])
        assert model.data() == item["data"]
    assert events[0]["event"] == "conversation"
    if name == "truncated":
        assert all(item["event"] != "done" for item in events)
    else:
        assert events[-1]["event"] == "done"
        assert [i["event"] for i in events].count("done") == 1


def test_golden_streams_cover_the_vocabulary():
    events = [e for st in GOLDEN["streams"].values() for e in st["events"]]

    def seen(event, field):
        return {e["data"].get(field) for e in events if e["event"] == event} - {None}

    assert {e["event"] for e in events} == set(EVENTS_BY_NAME)
    assert seen("tool_status", "phase") == set(get_args(s.ToolPhase))
    assert seen("tool_status", "outcome") == set(get_args(s.ToolOutcomeCode))
    assert seen("error", "code") == set(get_args(s.StreamErrorCode))
    assert seen("done", "finish_reason") == set(get_args(s.FinishReason))
    assert {e["data"]["message_id"] is None for e in events if e["event"] == "done"} == {
        True,
        False,
    }
    assert any(s.HEARTBEAT_FRAME in st["raw"] for st in GOLDEN["streams"].values())


def test_golden_drafts_cover_every_literal():
    vocabulary = GOLDEN["vocabulary"]
    drafts = GOLDEN["drafts"].values()
    outcomes = [d["outcome"] for d in drafts if d["outcome"]]
    assert {d["state"] for d in drafts} == set(vocabulary["draft_states"])
    assert {o["code"] for o in outcomes} == set(vocabulary["outcome_codes"])
    assert {o["gateway_result"] for o in outcomes} - {None} == set(
        vocabulary["gateway_results"]
    )
    assert {r["result"] for o in outcomes for r in o["results"]} == set(
        vocabulary["alert_results"]
    )
    assert {d["permitted"] for d in drafts} == {True, False}
    assert {d["payload"]["selector"]["type"] for d in drafts} == {"fingerprints", "cel"}
    alerts = [a for d in drafts for a in d["payload"]["alerts"]]
    assert {a["has_incident"] for a in alerts} == {True, False, None}
    assert {a["has_note"] for a in alerts} == {True, False}
    for draft in drafts:
        PAYLOAD_MODELS[draft["kind"]].parse_obj(draft["payload"])


def _optional_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.__fields__.items() if field.allow_none}


@pytest.mark.parametrize(
    "model",
    [DraftView, DraftOutcome, AlertOutcome, ChangeAlertStatusPayload, DraftAlert,
     SkippedAlert, ConversationView],
)  # fmt: skip
def test_every_nullable_field_appears_null_and_set(model):
    drafts = list(GOLDEN["drafts"].values())
    outcomes = [d["outcome"] for d in drafts if d["outcome"]]
    instances = {
        DraftView: drafts,
        DraftOutcome: outcomes,
        AlertOutcome: [r for o in outcomes for r in o["results"]],
        ChangeAlertStatusPayload: [d["payload"] for d in drafts],
        DraftAlert: [a for d in drafts for a in d["payload"]["alerts"]],
        SkippedAlert: [a for d in drafts for a in d["payload"]["skipped"]],
        ConversationView: list(GOLDEN["conversations"].values()),
    }[model]
    fields = _optional_fields(model)
    assert fields
    for name in fields:
        values = [instance[name] for instance in instances]
        assert None in values, name
        assert any(value is not None for value in values), name


def test_golden_conversations_validate():
    views = [ConversationView.parse_obj(v) for v in GOLDEN["conversations"].values()]
    assert {v.turn_in_progress for v in views} == {True, False}
    assert {v.has_more for v in views} == {True, False}
    items = [item for v in views for item in v.items]
    assert {type(item) for item in items} == {TimelineMessage, TimelineDraft}
    statuses = {i.status for i in items if isinstance(i, TimelineMessage)}
    assert statuses == set(GOLDEN["vocabulary"]["message_statuses"])


def test_golden_errors_match_the_error_table():
    for code, error in GOLDEN["errors"].items():
        assert error["body"]["detail"] == error["body"]["errors"][0]["message"]
        if code == "validation":
            assert error["status"] == 400
            continue
        assert error["body"]["errors"][0]["code"] == code
        assert error["status"] == ERRORS[code].status
        if code.startswith("draft_") and code != "draft_not_found":
            DraftView.parse_obj(error["body"]["draft"])
    service_only = {"not_found", "method_not_allowed", "http_error", "not_ready"}
    assert set(ERRORS) - service_only == set(GOLDEN["errors"]) - {"validation"}


# --- framing -----------------------------------------------------------------


def test_text_cannot_break_out_of_its_frame():
    event = s.TextDeltaEvent(
        message_id=golden_stream.uid(1),
        delta="a\n\nevent: done\ndata: {}\r\n\r\n b",
    )
    encoded = event.encode()
    assert parse_sse(encoded) == [{"event": "text_delta", "data": event.data()}]
    assert encoded.count("\n") == 3
    assert "\r" not in encoded


def test_unfinished_tool_status_has_no_outcome_key():
    event = s.ToolStatusEvent(tool_call_id="c", tool="t", phase="started", label="l")
    assert "outcome" not in event.data()


def test_unknown_error_code_cannot_be_sent():
    with pytest.raises(KeyError):
        s.stream_error("something_else")


def test_heartbeat_beats_the_proxy_idle_timeout():
    assert get_settings().heartbeat_seconds == 10
    assert get_settings().heartbeat_seconds * 2 < 30
    assert s.STREAM_HEADERS["Cache-Control"] == "no-cache, no-transform"
    assert s.STREAM_HEADERS["X-Accel-Buffering"] == "no"


# --- chat request ------------------------------------------------------------


def chat(**changes):
    body = {
        "conversation_id": None,
        "message": "acknowledge these",
        "context": {
            "page": "/alerts/feed",
            "selected_fingerprints": ["a", "b"],
            "selected_count": 2,
        },
    }
    body.update(changes)
    return body


def context(**changes):
    return chat(context={**chat()["context"], **changes})


@pytest.mark.parametrize(
    "body",
    [
        chat(),
        chat(message="x" * 4000),
        chat(conversation_id=golden_stream.uid(1)),
        chat(conversation_id="not-a-uuid"),  # the route answers 404
        context(page="x" * 200),
        context(selected_fingerprints=["f" * 512]),
        context(selected_fingerprints=[str(i) for i in range(200)], selected_count=5000),
        context(selected_fingerprints=[], selected_count=0),
    ],
)
def test_valid_chat_bodies(body):
    ChatRequest.parse_obj(body)


@pytest.mark.parametrize(
    "body",
    [
        chat(message=""),
        chat(message=" \n "),
        chat(message="x" * 4001),
        chat(message="a\x00b"),
        chat(message="a\ud800b"),
        chat(message=None),
        context(page="x" * 201),
        context(page="a\x00b"),
        context(selected_count=-1),
        context(selected_count="2"),
        context(selected_fingerprints=[str(i) for i in range(201)]),
        context(selected_fingerprints=[""]),
        context(selected_fingerprints=["f" * 513]),
        context(selected_fingerprints=["a\nb"]),
        context(selected_fingerprints=["a\x7fb"]),
        context(selected_fingerprints=["a\ud800b"]),
    ],
)
def test_invalid_chat_bodies(body):
    with pytest.raises(ValidationError):
        ChatRequest.parse_obj(body)


def test_the_message_limit_is_a_setting_read_per_request(set_env):
    set_env("ASSISTANT_MAX_USER_MESSAGE_CHARS", 5)
    ChatRequest.parse_obj(chat(message=" 12345 "))
    with pytest.raises(ValidationError):
        ChatRequest.parse_obj(chat(message="123456"))


def test_duplicate_fingerprints_are_dropped_in_order():
    parsed = ChatRequest.parse_obj(context(selected_fingerprints=["b", "a", "b"]))
    assert parsed.context.selected_fingerprints == ["b", "a"]


def test_context_is_optional():
    parsed = ChatRequest.parse_obj({"message": "hi"})
    assert parsed.context.selected_fingerprints == []


# --- over HTTP, on scratch routes of the real app -------------------------------


@pytest.fixture(scope="module")
def app(assistant_router, make_app):
    view = golden_stream.drafts()["partially_applied"]

    @assistant_router.post("/scratch/chat")
    async def scratch_chat(payload: ChatRequest):
        return {}

    @assistant_router.get("/scratch/draft", response_model=DraftView)
    async def scratch_draft():
        return view

    return make_app()


@pytest.fixture()
def client(app, schema_ready):
    return TestClient(app, headers={"Authorization": TOKEN})


def test_invalid_chat_body_is_a_400_over_http(client):
    assert client.post("/assistant/scratch/chat", json=chat()).status_code == 200
    response = client.post(
        "/assistant/scratch/chat", json=context(selected_count=-1)
    )
    # A lone surrogate survives JSON parsing.
    surrogate = client.post(
        "/assistant/scratch/chat",
        content=b'{"message": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == surrogate.status_code == 400
    assert response.json()["errors"][0]["field"] == "context.selected_count"
    assert surrogate.json()["errors"][0]["field"] == "message"


def test_models_survive_fastapi_serialization(client):
    """The HTTP body and the stream must render the same draft identically."""
    response = client.get("/assistant/scratch/draft")
    assert response.json() == GOLDEN["drafts"]["partially_applied"]
