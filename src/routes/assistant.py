"""Keep Assistant's routes. Imported and mounted only with KEEP_ASSISTANT_ENABLED."""
import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from src.services.assistant.api.deps import (
    get_identity,
    parse_resource_id,
    request_id,
    request_started,
    require_ready,
)
from src.services.assistant.api.route import AssistantRoute
from src.services.assistant.api.turn_response import TurnResponse
from src.services.assistant.bl import (
    chat_turn,
    confirm_bl,
    drafts_bl,
    reconcile_bl,
    timeline,
)
from src.services.assistant.core.work import run_db
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.gateway.reader import ReadGateway
from src.services.assistant.models.api.chat import ChatRequest
from src.services.assistant.models.api.conversation import ConversationView
from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.identity import Identity

router = APIRouter(
    route_class=AssistantRoute,
    dependencies=[Depends(request_started), Depends(require_ready)],
)


@router.post("/chat", description="Send a message; the answer is an event stream.")
async def chat(
    request: Request,
    body: ChatRequest,
    identity: Identity = Depends(get_identity),
) -> Response:
    state = request.app.state
    conversation_id = (
        parse_resource_id(body.conversation_id, "conversation_not_found")
        if body.conversation_id is not None
        else None
    )
    if state.assistant_llm is None:
        raise AssistantError("llm_not_configured")
    if state.assistant_work.draining:
        raise AssistantError("not_ready")
    starting = state.assistant_work.spawn(
        chat_turn.begin(
            identity,
            body,
            conversation_id,
            _reader(request, identity),
            state.assistant_llm,
            state.assistant_work,
        )
    )
    try:
        turn = await asyncio.shield(starting)
    except asyncio.CancelledError:
        starting.add_done_callback(_stop_unused)
        raise
    return TurnResponse(turn)


def _reader(request: Request, identity: Identity) -> ReadGateway:
    """The caller's reads; Confirm alone gets the client itself."""
    return ReadGateway(request.app.state.assistant_gateway, identity.caller(request_id()))


def _stop_unused(starting: "asyncio.Task[chat_turn.Turn]") -> None:
    if not starting.cancelled() and starting.exception() is None:
        starting.result().stop()


@router.get(
    "/conversations/{conversation_id}", description="A conversation, to show it again."
)
async def get_conversation(
    request: Request,
    conversation_id: str,
    identity: Identity = Depends(get_identity),
) -> ConversationView:
    conversation = parse_resource_id(conversation_id, "conversation_not_found")
    view = await run_db(timeline.load, identity, conversation)
    applying = reconcile_bl.applying(view)
    if applying and await reconcile_bl.reconcile(
        identity, applying, _reader(request, identity)
    ):
        view = await run_db(timeline.load, identity, conversation)
    return view


@router.post("/drafts/{draft_id}/confirm", description="Change what the card lists.")
async def confirm_draft(
    request: Request,
    draft_id: str,
    started: float = Depends(request_started),
    identity: Identity = Depends(get_identity),
) -> DraftView:
    state = request.app.state
    draft = parse_resource_id(draft_id, "draft_not_found")
    if state.assistant_work.draining:
        raise AssistantError("not_ready")
    confirming = state.assistant_work.spawn(
        confirm_bl.confirm(
            identity, draft, state.assistant_gateway, state.assistant_work, started
        )
    )
    # A user who leaves must not cut it off between the write and its result.
    return await asyncio.shield(confirming)


@router.post("/drafts/{draft_id}/discard", description="Put a draft away.")
async def discard_draft(
    draft_id: str,
    identity: Identity = Depends(get_identity),
) -> DraftView:
    draft = parse_resource_id(draft_id, "draft_not_found")
    return await run_db(drafts_bl.discard, identity, draft)
