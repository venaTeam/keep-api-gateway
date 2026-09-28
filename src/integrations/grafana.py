"""
Mirrors pandora-backend/integrations/grafana.py, adapted to keep-api-gateway's
config and logging conventions. Ensures a webhook contact point and a
notification-policy route per operator.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.config.core import config

logger = logging.getLogger(__name__)

_PROVISIONING_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    # Lets operators edit this contact point in the Grafana UI if needed.
    "X-Disable-Provenance": "true",
}


@dataclass(frozen=True)
class GrafanaProvisioningResult:
    contact_point_id: Optional[str] = None
    policy_id: Optional[str] = None


def _contact_point_uid(operator: str) -> str:
    digest = hashlib.sha256(operator.encode()).hexdigest()[:16]
    return f"agw-{digest}"


def _require_contact_point_settings() -> Tuple[str, str, str]:
    """Return `(grafana_api_base, api_token, webhook_url)`. Raises `ValueError` if any are unset."""

    base = (config("GRAFANA_API_URL", default="") or "").rstrip("/")
    if not base:
        raise ValueError("GRAFANA_API_URL is required")

    token = (config("GRAFANA_API_TOKEN", default="") or "").strip()
    if not token:
        raise ValueError("GRAFANA_API_TOKEN is required")

    webhook_url = (config("ALERTS_TO_CONSOLE_URL", default="") or "").strip()
    if not webhook_url:
        raise ValueError("ALERTS_TO_CONSOLE_URL is required")

    return base, token, webhook_url


def _provisioning_auth_headers(token: str) -> Dict[str, str]:
    return {**_PROVISIONING_HEADERS, "Authorization": f"Bearer {token}"}


def _contact_point_list_url(base: str) -> str:
    return f"{base.rstrip('/')}/api/v1/provisioning/contact-points"


def _policy_url(base: str) -> str:
    return f"{base.rstrip('/')}/api/v1/provisioning/policies"


def _build_sub_policy_data(*, contact_point_name: str) -> Dict[str, Any]:
    repeat_interval = config("GRAFANA_POLICY_REPEAT_INTERVAL", default="52w")
    return {
        "receiver": f"{contact_point_name}",
        "object_matchers": [[f"{contact_point_name}", "=", "true"]],
        "continue": True,
        "repeat_interval": repeat_interval,
        "group_by": ["..."],
    }


def _get_policy_tree(
    client: httpx.Client, *, base: str, headers: Dict[str, str]
) -> Dict[str, Any]:
    """Get the current notification policy tree from Grafana using provisioning API."""
    policy_url = _policy_url(base)
    response = client.get(policy_url, headers=headers)
    response.raise_for_status()
    return response.json()


def _build_contact_point_payload(
    *, uid: str, operator: str, webhook_url: str, api_key: str
) -> Dict[str, Any]:
    return {
        "uid": uid,
        "name": f"Appchi - {operator}",
        "type": "webhook",
        "settings": {
            "url": webhook_url,
            "httpMethod": "POST",
            "authorization_scheme": " ",
            "authorization_credentials": api_key,
        },
        "disableResolveMessage": False,
    }


def _parse_contact_points_list(raw: object) -> List[Any]:
    if not isinstance(raw, list):
        raise RuntimeError(
            f"Unexpected Grafana contact points response (expected JSON array): {raw!r}"
        )
    return raw


def _find_contact_point_by_uid(
    contact_points: List[Any], uid: str
) -> Optional[Dict[str, Any]]:
    return next(
        (cp for cp in contact_points if isinstance(cp, dict) and cp.get("uid") == uid),
        None,
    )


def _webhook_contact_point_unchanged(
    existing: Dict[str, Any], webhook_url: str
) -> bool:
    settings_existing = existing.get("settings") or {}
    if not isinstance(settings_existing, dict):
        return False
    method = settings_existing.get("httpMethod") or "POST"
    if not isinstance(method, str):
        return False
    return (
        existing.get("type") == "webhook"
        and settings_existing.get("url") == webhook_url
        and method.upper() == "POST"
    )


def _provision_contact_point(
    client: httpx.Client,
    *,
    list_url: str,
    headers: Dict[str, str],
    uid: str,
    operator: str,
    webhook_url: str,
    payload: Dict[str, Any],
) -> GrafanaProvisioningResult:
    listed = client.get(list_url, headers=headers)
    listed.raise_for_status()
    raw = _parse_contact_points_list(listed.json())
    existing = _find_contact_point_by_uid(raw, uid)

    if existing is not None:
        if _webhook_contact_point_unchanged(existing, webhook_url):
            logger.info(
                "Grafana contact point unchanged uid=%r operator=%r", uid, operator
            )
            return GrafanaProvisioningResult(contact_point_id=uid)

        put = client.put(f"{list_url}/{uid}", headers=headers, json=payload)
        put.raise_for_status()
        logger.info(
            "Grafana contact point updated uid=%r operator=%r status=%s",
            uid,
            operator,
            put.status_code,
        )
        return GrafanaProvisioningResult(contact_point_id=uid)

    post = client.post(list_url, headers=headers, json=payload)
    post.raise_for_status()
    logger.info(
        "Grafana contact point created uid=%r operator=%r status=%s",
        uid,
        operator,
        post.status_code,
    )
    return GrafanaProvisioningResult(contact_point_id=uid)


def _find_policy_node(
    policy_tree: Dict[str, Any], policy_name: str
) -> Optional[Dict[str, Any]]:
    """Find a policy node by name in the policy tree."""
    routes = policy_tree.get("routes", [])
    for route in routes:
        object_matchers = route.get("object_matchers", [])
        for matcher in object_matchers:
            if matcher == ["Send-Alert", "=", "true"]:
                return route
    return None


def _add_sub_policy_to_policy_routes(
    *, contact_point_name: str, policy_routes: Dict[str, Any]
) -> Dict[str, Any]:
    if "routes" not in policy_routes:
        policy_routes["routes"] = []
    policy_routes["routes"].append(
        _build_sub_policy_data(contact_point_name=contact_point_name)
    )
    return policy_routes


def _edit_policy_tree(
    new_routes_list: List[Any], policy_tree: Dict[str, Any]
) -> Dict[str, Any]:
    routes = policy_tree.get("routes", [])
    for route in routes:
        object_matchers = route.get("object_matchers", [])
        for matcher in object_matchers:
            if matcher == ["Send-Alert", "=", "true"]:
                route["routes"] = list(new_routes_list)
                break
    return policy_tree


def _provision_sub_policy(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    contact_point_name: str,
) -> str:
    """Create a sub-policy under the Send-Alert policy using provisioning API.

    Gets the policy tree, modifies it to add the sub-policy,
    then PUTs it back using the provisioning API.
    """

    try:
        logger.info(
            "Fetching policy tree from Grafana for contact_point=%r",
            contact_point_name,
        )
        policy_tree = _get_policy_tree(client, base=base, headers=headers)

        logger.info(
            "Finding Send-Alert policy node for contact_point=%r",
            contact_point_name,
        )
        policy_routes = _find_policy_node(
            policy_tree=policy_tree, policy_name="Send-Alerts"
        )
        if not policy_routes:
            raise RuntimeError("Could not find Send-Alert policy in Grafana")

        logger.info(
            "Adding sub-policy for contact_point=%r",
            contact_point_name,
        )
        edited_policy_routes = _add_sub_policy_to_policy_routes(
            policy_routes=policy_routes, contact_point_name=contact_point_name
        )
        new_policy = _edit_policy_tree(
            edited_policy_routes.get("routes", []), policy_tree
        )

        logger.info(
            "Updating policy tree in Grafana for contact_point=%r",
            contact_point_name,
        )
        put = client.put(_policy_url(base), headers=headers, json=new_policy)
        put.raise_for_status()

        logger.info(
            "Grafana sub-policy created for contact_point=%r status=%s",
            contact_point_name,
            put.status_code,
        )
        return contact_point_name

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error provisioning sub-policy: %s response=%s",
            e,
            e.response.text,
        )
        raise RuntimeError(f"Failed to provision sub-policy: {e}") from e
    except Exception as e:
        logger.error(
            "Failed to provision sub-policy for contact_point=%r: %s",
            contact_point_name,
            e,
        )
        raise RuntimeError(f"Failed to provision sub-policy: {e}") from e


class GrafanaIntegration:
    """
    Ensures a Grafana unified-alerting webhook contact point exists per operator.
    Every contact point uses the same webhook URL from ALERTS_TO_CONSOLE_URL.
    """

    def ensure_grafana_connections(
        self, *, operator: str, api_key: str
    ) -> GrafanaProvisioningResult:
        logger.info(
            "Ensuring Grafana contact point and policy for operator=%r", operator
        )
        base, token, webhook_url = _require_contact_point_settings()
        uid = _contact_point_uid(operator)
        contact_point_name = f"Appchi - {operator}"
        contact_point_payload = _build_contact_point_payload(
            uid=uid, operator=operator, webhook_url=webhook_url, api_key=api_key
        )
        headers = _provisioning_auth_headers(token)
        list_url = _contact_point_list_url(base)

        with httpx.Client(timeout=30.0, verify=False) as client:
            # Create/update contact point
            _provision_contact_point(
                client,
                list_url=list_url,
                headers=headers,
                uid=uid,
                operator=operator,
                webhook_url=webhook_url,
                payload=contact_point_payload,
            )

            # Create/update sub-policy under Send-Alert using provisioning API
            policy_id = _provision_sub_policy(
                client,
                base=base,
                headers=headers,
                contact_point_name=contact_point_name,
            )

            return GrafanaProvisioningResult(
                contact_point_id=uid,
                policy_id=policy_id,
            )

    def delete_grafana_connections(self, *, operator: str) -> None:
        """Delete Grafana contact point for an operator (for rollback)."""
        logger.info("Deleting Grafana connections for operator=%r", operator)
        try:
            base, token, _ = _require_contact_point_settings()
            uid = _contact_point_uid(operator)
            headers = _provisioning_auth_headers(token)
            list_url = _contact_point_list_url(base)

            with httpx.Client(timeout=30.0, verify=False) as client:
                # Delete contact point
                deleted = client.delete(f"{list_url}/{uid}", headers=headers)
                if deleted.status_code == httpx.codes.NO_CONTENT:
                    logger.info(
                        "Deleted Grafana contact point uid=%r operator=%r",
                        uid,
                        operator,
                    )
                elif deleted.status_code == httpx.codes.NOT_FOUND:
                    logger.info(
                        "Grafana contact point not found (already deleted) uid=%r operator=%r",
                        uid,
                        operator,
                    )
                else:
                    deleted.raise_for_status()
        except Exception as e:
            # Log but don't fail - best effort cleanup during rollback
            logger.warning(
                "Failed to delete Grafana connections for operator=%r: %s",
                operator,
                e,
            )