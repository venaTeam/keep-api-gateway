"""Response body of GET /assistant/conversations/{id}."""
from datetime import datetime
from typing import List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel

from src.services.assistant.models.api.draft import DraftView
from src.services.assistant.models.api.stream import LinkPage

# Items per view, newest last, ordered by message `seq`.
TIMELINE_LIMIT = 100

MessageStatus = Literal["complete", "interrupted", "error"]


class TimelineMessage(BaseModel):
    """Assistant rows without text (tool calls only) are omitted from a view."""

    type: Literal["message"] = "message"
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: MessageStatus
    created_at: datetime


class TimelineDraft(BaseModel):
    """At the position of the tool message that created the draft."""

    type: Literal["draft"] = "draft"
    draft: DraftView


class TimelineLink(BaseModel):
    """At the position of the tool message that made the link: the `link` event's
    fields, and that message's id. A stored link this version cannot read, or that
    the user may no longer open, is left out."""

    type: Literal["link"] = "link"
    id: UUID
    page: LinkPage
    param: Optional[str]
    path: str
    label: str
    created_at: datetime


class ConversationView(BaseModel):
    id: UUID
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    # The UI stops polling when this turns false.
    turn_in_progress: bool
    # Older items exist that this view leaves out.
    has_more: bool
    items: List[Union[TimelineMessage, TimelineDraft, TimelineLink]]
