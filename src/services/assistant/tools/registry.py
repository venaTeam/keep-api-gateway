"""Every tool the model has. Nothing here, or ever, may delete, change settings,
workflows, providers or users, or write anything without the user's confirmation."""
from src.services.assistant.tools import change_alert_status
from src.services.assistant.tools.base import ToolSpec

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in (change_alert_status.SPEC,)}
