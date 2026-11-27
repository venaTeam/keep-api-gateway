"""Every tool the model has. Nothing here, or ever, may delete, change settings,
workflows, providers or users, or write anything without the user's confirmation."""
from src.services.assistant.kinds.registry import disabled_kinds
from src.services.assistant.tools import change_alert_status
from src.services.assistant.tools.base import ToolSpec

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in (change_alert_status.SPEC,)}


def offered_tools() -> dict[str, ToolSpec]:
    """What a turn offers the model: every tool but those whose `draft_kind` is
    switched off."""
    off = disabled_kinds()
    return {
        name: spec
        for name, spec in TOOLS.items()
        if spec.draft_kind is None or spec.draft_kind not in off
    }
