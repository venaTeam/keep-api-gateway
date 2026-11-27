"""The earlier messages of a conversation, as the LLM accepts them.

A turn can end anywhere (disconnect, timeout, a worker that is killed), so the rows may
hold tool calls without an answer and answers without a call. Both are refused by the
LLM and would make the conversation unusable for good: they are left out here.

The tool replies of earlier turns come back as digests: in full, a turn of large reads
would push the question they answered out of the history. The turn that made them sees
them in full (chat_turn appends them itself).
"""
import json
from collections.abc import Iterator, Sequence
from typing import Any, NamedTuple

import sqlalchemy as sa

from src.services.assistant.bl import conversations_bl
from src.services.assistant.bl.conversations_bl import TurnRef
from src.services.assistant.bl.projection import to_json
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

MAX_REPLAYED_RESULT_CHARS = 1000
"""An earlier turn's tool reply is replayed in at most this many characters."""
_DIGEST_ITEMS = (5, 2, 1, 0)
_DIGEST_TEXT_CHARS = 80
_DIGEST_SUMMARY_KEYS = ("status", "code", "count")
_TRUNCATED = '{"truncated":true}'


class _Row(NamedTuple):
    """A message row as `_COLUMNS` reads it."""

    role: str
    content: str | None
    tool_calls: Any
    tool_call_id: str | None


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
    replayed = [_replayed(_Row(*row)) for row in rows]
    return to_llm_messages(_newest(replayed, settings.history_max_chars))


def _replayed(row: _Row) -> _Row:
    """Every row here is from an earlier turn: its tool reply becomes a digest, and is
    counted as one."""
    if row.role != "tool" or row.content is None:
        return row
    return row._replace(content=digest(row.content))


def digest(result: str) -> str:
    """A tool reply in at most MAX_REPLAYED_RESULT_CHARS, as JSON marked truncated when
    it was cut: its lists get fewer items and its texts are clipped, then only its plain
    fields stay, then only its status. The reply is our own JSON; anything else that is
    too long keeps only the mark."""
    if len(result) <= MAX_REPLAYED_RESULT_CHARS:
        return result
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            for candidate in _shorter(parsed):
                encoded = to_json({**candidate, "truncated": True})
                if len(encoded) <= MAX_REPLAYED_RESULT_CHARS:
                    return encoded
    except (ValueError, RecursionError):
        pass
    return _TRUNCATED


def _shorter(result: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for items in _DIGEST_ITEMS:
        yield _clipped(result, items)
    plain = {k: _clipped(v, 0) for k, v in result.items() if _is_plain(v)}
    yield plain
    yield {k: v for k, v in plain.items() if k in _DIGEST_SUMMARY_KEYS}


def _clipped(value: Any, items: int) -> Any:
    if isinstance(value, str):
        if len(value) <= _DIGEST_TEXT_CHARS:
            return value
        return value[: _DIGEST_TEXT_CHARS - 1] + "…"
    if isinstance(value, list):
        return [_clipped(v, items) for v in value[:items]]
    if isinstance(value, dict):
        return {k: _clipped(v, items) for k, v in value.items()}
    return value


def _is_plain(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


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
