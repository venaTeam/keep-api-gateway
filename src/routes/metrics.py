from typing import List

import chevron
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    generate_latest,
    multiprocess,
)

from src.config.config import KEEP_METRICS_LIMIT
from src.repositories.db import (
    get_last_alerts_for_incidents,
    get_last_incidents,
)
from src.repositories.dependencies import SINGLE_TENANT_UUID
from src.utils.limiter import limiter
from src.models.alert import AlertDto

router = APIRouter()

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


@router.get("")
def get_metrics(
    labels: List[str] = Query(None),
):
    """
    This endpoint is used by Prometheus to scrape such metrics from the application:
    - alerts_total {incident_name, incident_id} - The total number of alerts per incident.
    - open_incidents_total - The total number of open incidents.
    - open_incidents_total - The total number of open incidents.

    Please note that those metrics are per-tenant and are not designed to be used for the monitoring of the application itself.

    Example prometheus configuration:
    ```
    scrape_configs:
    - job_name: "scrape_keep"
    scrape_interval: 5m  # It's important to scrape not too often to avoid rate limiting.
    static_configs:
    - targets: ["https://api.keephq.dev"]  # Or your own domain.

    # Optional, you can add labels to exported incidents.
    # Label values will be equal to the last incident's alert payload value matching the label.
    # Attention! Don't add "flaky" labels which could change from alert to alert within the same incident.
    # Good labels: ['labels.department', 'labels.team'], bad labels: ['labels.severity', 'labels.pod_id']
    # Check Keep -> Feed -> "extraPayload" column, it will help in writing labels.

    params:
        labels: ['labels.service', 'labels.queue']
    # Will resuld as: "labels_service" and "labels_queue".
    ```
    """
    # We don't use im-memory metrics countrs here which is typical for prometheus exporters,
    # they would make us expose our app's pod id's. This is a customer-facing endpoint
    # we're deploying to SaaS, and we want to hide our internal infra.

    tenant_id = SINGLE_TENANT_UUID

    export = str()

    # Exporting alerts per incidents
    export += "# HELP alerts_total The total number of alerts per incident.\n"
    export += "# TYPE alerts_total counter\n"
    incidents, incidents_total = get_last_incidents(
        tenant_id=tenant_id,
        limit=1000,
        is_candidate=False,
    )

    last_alerts_for_incidents = get_last_alerts_for_incidents(
        [incident.id for incident in incidents]
    )

    for incident in incidents:
        incident_name = (
            incident.user_generated_name
            if incident.user_generated_name
            else incident.ai_generated_name
        )
        extra_labels = ""
        try:
            last_alert = last_alerts_for_incidents[str(incident.id)][0]
            payload = last_alert.dict()

            last_alert_dto = AlertDto(**payload)
        except IndexError:
            last_alert_dto = None

        if labels is not None:
            for label in labels:
                label_value = chevron.render("{{ " + label + " }}", last_alert_dto)
                label = label.replace(".", "_")
                extra_labels += f',{label}="{label_value}"'

        export += f'alerts_total{{incident_name="{incident_name}",incident_id="{incident.id}"{extra_labels}}} {incident.alerts_count}\n'

    # Exporting stats about open incidents
    export += "\n\n"
    export += "# HELP open_incidents_total The total number of open incidents.\r\n"
    export += "# TYPE open_incidents_total counter\n"
    export += f"open_incidents_total {incidents_total}\n"



    # Exporting standard application metrics (prometheus_client)
    # Exporting standard application metrics (prometheus_client)
    try:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        # generate_latest returns bytes, so we decode to string to append to export
        export += generate_latest(registry).decode("utf-8")
    except Exception:
        # Fallback to default registry if multiprocess collection fails
        # This is useful for local development or if configuration is improper
        from prometheus_client import REGISTRY
        export += generate_latest(REGISTRY).decode("utf-8")

    return Response(content=export, media_type=CONTENT_TYPE_LATEST)


@router.get("/dumb", include_in_schema=False)
@limiter.limit(KEEP_METRICS_LIMIT)
async def get_dumb(request: Request) -> JSONResponse:
    """
    This endpoint is used to test the rate limiting.

    Args:
        request (Request): The request object.

    Returns:
        JSONResponse: A JSON response with the message "hello world" ({"hello": "world"}).
    """
    # await asyncio.sleep(5)
    return JSONResponse(content={"hello": "world"})

