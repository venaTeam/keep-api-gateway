"""Keep Assistant's routes. Imported and mounted only with KEEP_ASSISTANT_ENABLED."""
import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from src.services.assistant.api.deps import (
    get_identity,
    parse_resource_id,
    require_ready,
)
from src.services.assistant.api.route import AssistantRoute
from src.services.assistant.api.turn_response import TurnResponse
from src.services.assistant.bl import chat_turn
from src.services.assistant.exceptions import AssistantError
from src.services.assistant.models.api.chat import ChatRequest
from src.services.assistant.models.identity import Identity

router = APIRouter(route_class=AssistantRoute, dependencies=[Depends(require_ready)])


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
            state.assistant_gateway,
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


def _stop_unused(starting: "asyncio.Task[chat_turn.Turn]") -> None:
    if not starting.cancelled() and starting.exception() is None:
        starting.result().stop()
