"""Every draft kind this version knows. A kind that is not here (a newer version's,
after a rollback) is never confirmed or looked at; only the 24 h sweep closes it."""
from collections.abc import Mapping
from types import MappingProxyType
from typing import Optional

from pydantic import BaseModel

from src.services.assistant.kinds import alert_status
from src.services.assistant.kinds.base import DraftKind

KINDS: Mapping[str, DraftKind] = MappingProxyType(
    {kind.name: kind for kind in (alert_status.KIND,)}
)

# kind -> payload model.
PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {name: kind.payload_model for name, kind in KINDS.items()}
)


def kind_of(name: str) -> Optional[DraftKind]:
    """None: a kind this version does not know."""
    return KINDS.get(name)
