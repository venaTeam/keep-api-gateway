"""The earlier messages of a conversation, as the LLM accepts them.

A turn can end anywhere (disconnect, timeout, a worker that is killed), so the rows may
hold tool calls without an answer and answers without a call. Both are refused by the
LLM and would make the conversation unusable for good: they are left out here.
"""
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from src.services.assistant.bl import conversations_bl
from src.services.assistant.bl.conversations_bl import TurnRef
from src.services.assistant.bl.prompt import untrusted
from src.services.assistant.config import get_settings
from src.services.assistant.core.db import get_session
from src.services.assistant.models.db import Conversation, Message

_COLUMNS = (
    Message.role,
    Message.content,
    Message.tool_calls,
    Message.tool_call_id,
)


def load(turn: TurnRef) -> list[dict[str, Any]]:
    """What was said before this turn's own message."""
    settings = get_settings()
    with get_session() as session:
        rows = session.execute(
            sa.select(*_COLUMNS)
            .join(
                Conversation,
                sa.and_(
                    Conversation.id == Message.conversation_id,
                    Conversation.tenant_id == Message.tenant_id,
                ),
            )
            .where(
                Message.conversation_id == turn.conversation_id,
                *conversations_bl.owned(turn.tenant_id, turn.created_by),
                Message.id != turn.user_message_id,
            )
            .order_by(Message.seq.desc())
            .limit(settings.history_max_messages)
        ).all()
    return to_llm_messages(_newest(rows, settings.history_max_chars))


def _newest(rows: Sequence[Any], max_chars: int) -> list[Any]:
    """Newest first in, oldest first out, cut where the characters run out."""
    kept, used = [], 0
    for row in rows:
        used += len(row.content or "")
        if row.tool_calls:
            used += len(json.dumps(row.tool_calls))
        if used > max_chars:
            break
        kept.append(row)
    return kept[::-1]


def to_llm_messages(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """Rows in order, from the first user message on."""
    messages: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if row.role == "user":
            messages.append({"role": "user", "content": row.content or ""})
        elif not messages:
            continue
        elif row.role == "event":
            messages.append(
                {"role": "system", "content": f"Draft outcome: {row.content or ''}"}
            )
        elif row.role == "assistant":
            messages.extend(_assistant(row, _replies(rows, index)))
        # Tool rows are taken by their assistant row; the others have none.
    return messages


def _replies(rows: Sequence[Any], index: int) -> list[Any]:
    """The tool rows of the assistant row at `index`. Events can be among them."""
    replies = []
    for row in rows[index + 1 :]:
        if row.role in ("user", "assistant"):
            break
        if row.role == "tool":
            replies.append(row)
    return replies


def _assistant(row: Any, replies: list[Any]) -> list[dict[str, Any]]:
    calls, answers = [], []
    for call in row.tool_calls or ():
        reply = next((r for r in replies if r.tool_call_id == call["id"]), None)
        if reply is None:
            continue
        replies.remove(reply)
        calls.append(tool_call(call["id"], call["name"], call["arguments"]))
        answers.append(tool_reply(call["id"], reply.content or ""))
    if not calls and not row.content:
        return []
    return [assistant_message(row.content, calls), *answers]


def assistant_message(
    content: str | None, calls: list[dict[str, Any]]
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = calls
    return message


def tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    try:
        json.loads(arguments)
    except (ValueError, RecursionError):
        arguments = "{}"  # cut short by the model; an endpoint may refuse it
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def tool_reply(call_id: str, result: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": untrusted(result)}
