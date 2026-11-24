import uuid
from dataclasses import dataclass, field

from src.services.assistant.gateway.client import Caller


@dataclass(frozen=True)
class Identity:
    """Who the gateway says the caller is. Rows are owned by (tenant_id, email)."""

    tenant_id: str
    email: str
    # A hint for the draft card. None: the gateway could not name them.
    scopes: frozenset[str] | None
    # The caller's token, forwarded verbatim to the gateway and nowhere else.
    authorization: str = field(repr=False)

    def caller(self, request_id: uuid.UUID | str) -> Caller:
        return Caller(authorization=self.authorization, request_id=str(request_id))
