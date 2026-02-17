import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.repositories.db import (
    calc_incidents_mttr,
    get_incidents_created_distribution,
    get_provider_distribution,
)
from src.repositories.db import (
    create_dashboard as create_dashboard_db,
)
from src.repositories.db import delete_dashboard as delete_dashboard_db
from src.repositories.db import get_dashboards as get_dashboards_db
from src.repositories.db import update_dashboard as update_dashboard_db
from src.models.time_stamp import TimeStampFilter, _get_time_stamp_filter
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory
from src.providers.providers_factory import ProvidersFactory


class DashboardCreateDTO(BaseModel):
    dashboard_name: str
    dashboard_config: Dict


class DashboardUpdateDTO(BaseModel):
    dashboard_config: Optional[Dict] = None  # Allow partial updates
    dashboard_name: Optional[str] = None


class DashboardResponseDTO(BaseModel):
    id: str
    dashboard_name: str
    dashboard_config: Dict
    created_at: datetime
    updated_at: datetime


router = APIRouter()
logger = logging.getLogger(__name__)


def provision_dashboards(tenant_id: str):
    try:
        dashboards_raw = json.loads(os.environ.get("KEEP_DASHBOARDS", "[]"))
    except Exception:
        logger.exception("Failed to load dashboards from environment variable")
        return
    if not dashboards_raw:
        logger.debug("No dashboards to provision")
        return
    logger.info(
        "Provisioning Dashboards", extra={"num_of_dashboards": len(dashboards_raw)}
    )
    dashboards_to_provision = [
        DashboardCreateDTO.parse_obj(dashboard) for dashboard in dashboards_raw
    ]
    for dashboard in dashboards_to_provision:
        logger.info(
            "Provisioning Dashboard",
            extra={"dashboard_name": dashboard.dashboard_name},
        )
        try:
            create_dashboard_db(
                tenant_id,
                dashboard.dashboard_name,
                "system",
                dashboard.dashboard_config,
            )
            logger.info(
                "Provisioned Dashboard",
                extra={"dashboard_name": dashboard.dashboard_name},
            )
        except Exception:
            logger.exception(
                "Failed to provision dashboard",
                extra={"dashboard_name": dashboard.dashboard_name},
            )
    logger.info(
        "Provisioned Dashboards", extra={"num_of_dashboards": len(dashboards_raw)}
    )


@router.get("", response_model=List[DashboardResponseDTO])
def read_dashboards(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:dashboards"])
    ),
):
    dashboards = get_dashboards_db(authenticated_entity.tenant_id)
    return dashboards


@router.post("", response_model=DashboardResponseDTO)
def create_dashboard(
    dashboard_dto: DashboardCreateDTO,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:dashboards"])
    ),
):
    email = authenticated_entity.email
    dashboard = create_dashboard_db(
        tenant_id=authenticated_entity.tenant_id,
        dashboard_name=dashboard_dto.dashboard_name,
        dashboard_config=dashboard_dto.dashboard_config,
        created_by=email,
    )
    return dashboard


@router.put("/{dashboard_id}", response_model=DashboardResponseDTO)
def update_dashboard(
    dashboard_id: str,
    dashboard_dto: DashboardUpdateDTO,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:dashboards"])
    ),
):
    # update the dashboard in the database
    dashboard = update_dashboard_db(
        tenant_id=authenticated_entity.tenant_id,
        dashboard_id=dashboard_id,
        dashboard_name=dashboard_dto.dashboard_name,
        dashboard_config=dashboard_dto.dashboard_config,
        updated_by=authenticated_entity.email,
    )
    return dashboard


@router.delete("/{dashboard_id}")
def delete_dashboard(
    dashboard_id: str,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:dashboards"])
    ),
):
    # delete the dashboard from the database
    dashboard = delete_dashboard_db(authenticated_entity.tenant_id, dashboard_id)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return {"ok": True}


@router.get("/metric-widgets")
def get_metric_widgets(
    time_stamp: TimeStampFilter = Depends(_get_time_stamp_filter),
    mttr: bool = True,
    apd: bool = True,
    ipd: bool = True,
    wpd: bool = True,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:dashboards"])
    ),
):
    data = {}
    tenant_id = authenticated_entity.tenant_id
    if not time_stamp.lower_timestamp or not time_stamp.upper_timestamp:
        time_stamp = TimeStampFilter(
            upper_timestamp=datetime.utcnow(),
            lower_timestamp=datetime.utcnow() - timedelta(hours=24),
        )
    if apd:
        data["apd"] = get_provider_distribution(
            tenant_id=tenant_id, aggregate_all=True, timestamp_filter=time_stamp
        )
    if ipd:
        data["ipd"] = get_incidents_created_distribution(
            tenant_id=tenant_id, timestamp_filter=time_stamp
        )

    if mttr:
        data["mttr"] = calc_incidents_mttr(
            tenant_id=tenant_id, timestamp_filter=time_stamp
        )
    return data


@router.get("/ticket-count")
def get_ticket_count(
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:dashboards"])
    ),
    team: str | None = None,
    state: str | None = None,  # expected: open, in_progress, all
    detection: str | None = None,  # expected: direct, hamal, all
):
    """
    Get ticket count from ticket_count provider.
    Returns the count from the provider's count_url endpoint.
    """
    tenant_id = authenticated_entity.tenant_id

    installed_providers = ProvidersFactory.get_installed_providers(
        tenant_id, include_details=True
    )

    ticket_count_provider = None
    for provider in installed_providers:
        if provider.type == "ticket_count":
            ticket_count_provider = provider
            break

    if not ticket_count_provider:
        raise HTTPException(status_code=404, detail="ticket_count provider not found")

    ticket_url = ticket_count_provider.details.get("authentication", {}).get(
        "count_url"
    )

    if not ticket_url:
        raise HTTPException(
            status_code=400,
            detail="ticket_count provider missing count_url configuration",
        )

    try:
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        params: dict[str, str] = {}
        if team:
            params["team"] = team
        if state:
            params["state"] = state
        if detection and detection in ("direct", "hamal"):
            params["system_failure"] = "true" if detection == "hamal" else "false"

        parsed = urlparse(ticket_url)
        existing_qs = dict(parse_qsl(parsed.query)) if parsed.query else {}
        merged_qs = {**existing_qs, **params}
        new_query = urlencode(merged_qs)
        composed_url = urlunparse(parsed._replace(query=new_query))

        response = requests.get(composed_url, timeout=10)
        response.raise_for_status()
        data = response.json()

        try:
            if isinstance(data, dict) and "Team not found" in data:
                return data
        except Exception:
            pass

        count = data.get("count", 0)
        return {"count": count}
    except requests.exceptions.RequestException as e:
        logger.exception(
            "Failed to fetch ticket count from provider URL",
            extra={"tenant_id": tenant_id, "url": ticket_url},
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch ticket count: {str(e)}",
        )


