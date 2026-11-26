"""Request body of POST /assistant/chat."""
import re
from typing import List, Optional

from pydantic import BaseModel, Field, conint, conlist, constr, validator

from src.services.assistant.config import get_settings

MAX_PAGE_CHARS = 200
MAX_SELECTED_FINGERPRINTS = 200
MAX_FINGERPRINT_CHARS = 512

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def storable(value: str) -> str:
    """Postgres text holds neither NUL nor a lone surrogate, and both survive
    JSON parsing: refuse them here instead of failing on the insert."""
    if "\x00" in value:
        raise ValueError("NUL character")
    value.encode("utf-8")  # UnicodeEncodeError is a ValueError
    return value


def has_control_characters(value: str) -> bool:
    return bool(_CONTROL.search(value))


class ChatContext(BaseModel):
    """What the user is looking at. Ids only: the server re-reads the alerts and
    never trusts what a client says about them."""

    page: constr(max_length=MAX_PAGE_CHARS) = ""
    selected_fingerprints: conlist(
        constr(min_length=1, max_length=MAX_FINGERPRINT_CHARS),
        max_items=MAX_SELECTED_FINGERPRINTS,
    ) = Field(default_factory=list)
    # The true size of the selection; the list carries the first 200.
    selected_count: conint(ge=0, strict=True) = 0

    _page_storable = validator("page", allow_reuse=True)(storable)

    @validator("selected_fingerprints")
    def _clean_and_distinct(cls, value: List[str]) -> List[str]:
        for fingerprint in value:
            if has_control_characters(fingerprint):
                raise ValueError("control character")
            storable(fingerprint)
        return list(dict.fromkeys(value))


class ChatRequest(BaseModel):
    # A plain string on purpose: the route turns a malformed id into the same 404
    # as an unknown one, which makes the UI drop a bad stored id.
    conversation_id: Optional[str] = None
    message: constr(strip_whitespace=True, min_length=1)
    context: ChatContext = Field(default_factory=ChatContext)

    @validator("message")
    def _bounded_and_storable(cls, value: str) -> str:
        if len(value) > get_settings().max_user_message_chars:
            raise ValueError("too long")
        return storable(value)
