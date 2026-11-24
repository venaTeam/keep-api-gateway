"""Keep Assistant's routes. Imported and mounted only with KEEP_ASSISTANT_ENABLED."""
from fastapi import APIRouter, Depends

from src.services.assistant.api.deps import require_ready
from src.services.assistant.api.route import AssistantRoute

router = APIRouter(route_class=AssistantRoute, dependencies=[Depends(require_ready)])
