"""The response that streams a turn."""
import logging

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from src.services.assistant.bl.chat_turn import Turn
from src.services.assistant.core.work import log_failure
from src.services.assistant.models.api.stream import (
    STREAM_HEADERS,
    STREAM_MEDIA_TYPE,
)

logger = logging.getLogger(__name__)

# What a request may wait for the rows of a turn it has ended.
CLOSE_SECONDS = 5.0


class TurnResponse(StreamingResponse):
    """Ends the turn with the response: after the last frame, when the client goes
    away, and when the body was never read.

    It has an error boundary of its own, because the one of the route ends where the
    response begins.
    """

    def __init__(self, turn: Turn):
        super().__init__(
            turn.frames(),
            # Set here, so that no charset is added to it.
            headers={"Content-Type": STREAM_MEDIA_TYPE, **STREAM_HEADERS},
        )
        self._turn = turn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        begun = False

        async def begin(message: Message) -> None:
            nonlocal begun
            if begun:
                return await send(message)
            # Not cancelled half way: GZip holds the start back until the first body
            # and then sends it once.
            with anyio.CancelScope(shield=True):
                await send(message)
            begun = message["type"] == "http.response.body"

        try:
            await super().__call__(scope, receive, begin)
            if not begun:
                # The client was gone before the first frame. The middleware around
                # this raises when there is no answer at all.
                await send({"type": "http.response.body", "body": b""})
        except Exception as exc:
            # Never further up: the gateway's catch-all logs and returns the text.
            log_failure("stream", "assistant: stream failed", exc)
        finally:
            self._turn.stop()
            with anyio.move_on_after(CLOSE_SECONDS, shield=True):
                await self._turn.closed()
