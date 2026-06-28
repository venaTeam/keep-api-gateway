from fastapi import FastAPI

from src.routes import (
    ai,
    alerts,
    cel,
    dashboard,
    deduplications,
    extraction,
    facets,
    healthcheck,
    incidents,
    maintenance,
    mapping,
    metrics,
    preset,
    provider_images,
    providers,
    rules,
    rum,
    settings,
    sse_routes,
    status,
    tags,
    ui_events,
    whoami,
)
from src.routes.auth import groups as auth_groups
from src.routes.auth import permissions, roles, users


def setup_routers(app: FastAPI):
    app.include_router(providers.router, prefix="/providers", tags=["providers"])
    # app.include_router(actions.router, prefix="/actions", tags=["actions"])
    app.include_router(ai.router, prefix="/ai", tags=["ai"])
    app.include_router(healthcheck.router, prefix="/healthcheck", tags=["healthcheck"])
    app.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
    app.include_router(incidents.router, prefix="/incidents", tags=["incidents"])
    app.include_router(settings.router, prefix="/settings", tags=["settings"])
    app.include_router(whoami.router, prefix="/whoami", tags=["whoami"])
    app.include_router(sse_routes.router, prefix="/sse", tags=["sse"])
    app.include_router(ui_events.router, prefix="/ui", tags=["ui"])
    app.include_router(status.router, prefix="/status", tags=["status"])
    app.include_router(rules.router, prefix="/rules", tags=["rules"])
    app.include_router(preset.router, prefix="/preset", tags=["preset"])
    app.include_router(
        mapping.router, prefix="/mapping", tags=["enrichment", "mapping"]
    )
    app.include_router(
        auth_groups.router, prefix="/auth/groups", tags=["auth", "groups"]
    )
    app.include_router(
        permissions.router, prefix="/auth/permissions", tags=["auth", "permissions"]
    )
    app.include_router(roles.router, prefix="/auth/roles", tags=["auth", "roles"])
    app.include_router(users.router, prefix="/auth/users", tags=["auth", "users"])
    app.include_router(rum.router, prefix="/rum", tags=["rum"])
    app.include_router(metrics.router, prefix="/metrics", tags=["metrics"])
    app.include_router(
        extraction.router, prefix="/extraction", tags=["enrichment", "extraction"]
    )
    app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
    app.include_router(tags.router, prefix="/tags", tags=["tags"])
    app.include_router(maintenance.router, prefix="/maintenance", tags=["maintenance"])
    # app.include_router(topology.router, prefix="/topology", tags=["topology"])
    app.include_router(
        deduplications.router, prefix="/deduplications", tags=["deduplications"]
    )
    app.include_router(facets.router, prefix="/{entity_name}/facets", tags=["facets"])
    app.include_router(cel.router, prefix="/cel", tags=["cel"])
    app.include_router(
        provider_images.router, prefix="/provider-images", tags=["provider-images"]
    )