"""Kong Admin API integration for operator API-key provisioning.

Mirrors pandora-backend/integrations/kong.py, adapted to keep-api-gateway's
config and logging conventions. The key returned from
:meth:`KongIntegration.create_or_update_api_key` is the credential Kong stored;
callers store that value in `Operator.apikey`.
"""

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from src.config.core import config

logger = logging.getLogger(__name__)

# Match prior app service length when Kong omits generated keys from Admin API responses.
_FALLBACK_KEY_BYTES: int = 32


@dataclass(frozen=True)
class KongProvisioningResult:
    consumer_id: Optional[str] = None
    api_key_id: Optional[str] = None


def _admin_base() -> str:
    return (config("KONG_API_URL", default="") or "").rstrip("/")


def _require_admin_base() -> str:
    base = _admin_base()
    if not base:
        raise ValueError("KONG_API_URL is required")
    return base


def _path_segment(value: str) -> str:
    return quote(str(value), safe="")


def _admin_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = (config("KONG_ADMIN_TOKEN", default="") or "").strip()
    if token:
        headers["Kong-Admin-Token"] = token
    return headers


def _kong_tag_fragment(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9.\-_]+", "-", value.strip()).strip("-")
    return (slug or "group")[:64]


def _consumer_tags(mail_group: str) -> List[str]:
    return ["alerts-gateway", f"mail-group-{_kong_tag_fragment(mail_group)}"]


def _consumer_url(base: str, username_segment: str) -> str:
    return f"{base}/consumers/{username_segment}"


def _create_consumer_or_fetch_existing(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    operator: str,
    mail_group: str,
    operator_seg: str,
) -> KongProvisioningResult:
    created = client.post(
        f"{base}/consumers",
        headers=headers,
        json={"username": operator, "tags": _consumer_tags(mail_group)},
    )
    if created.status_code == httpx.codes.CONFLICT:
        logger.warning(
            "Kong consumer create conflict; re-fetching operator=%r",
            operator,
        )
        retry = client.get(_consumer_url(base, operator_seg), headers=headers)
        retry.raise_for_status()
        return KongProvisioningResult(consumer_id=retry.json()["id"])

    created.raise_for_status()
    body = created.json()
    logger.info(
        "Kong consumer created operator=%r consumer_id=%r",
        operator,
        body["id"],
    )
    return KongProvisioningResult(consumer_id=body["id"])


def _ensure_consumer(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    operator: str,
    mail_group: str,
) -> KongProvisioningResult:
    operator_seg = _path_segment(operator)
    got = client.get(_consumer_url(base, operator_seg), headers=headers)
    if got.status_code == httpx.codes.OK:
        body = got.json()
        logger.info(
            "Kong consumer already exists operator=%r consumer_id=%r",
            operator,
            body["id"],
        )
        return KongProvisioningResult(consumer_id=body["id"])
    if got.status_code != httpx.codes.NOT_FOUND:
        got.raise_for_status()

    return _create_consumer_or_fetch_existing(
        client,
        base=base,
        headers=headers,
        operator=operator,
        mail_group=mail_group,
        operator_seg=operator_seg,
    )


def _next_paged_admin_url(base: str, nxt: Any) -> Optional[str]:
    if not nxt or not isinstance(nxt, str):
        return None
    if nxt.startswith("http://") or nxt.startswith("https://"):
        return nxt
    return f"{base}{nxt}" if nxt.startswith("/") else f"{base}/{nxt}"


def _delete_all_consumer_key_auths(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    consumer_seg: str,
) -> None:
    url: Optional[str] = f"{base}/consumers/{consumer_seg}/key-auth"
    while url:
        listed = client.get(url, headers=headers)
        listed.raise_for_status()
        payload = listed.json()
        for cred in payload.get("data") or []:
            cid = cred.get("id")
            if not cid:
                continue
            deleted = client.delete(
                f"{base}/consumers/{consumer_seg}/key-auth/{_path_segment(str(cid))}",
                headers=headers,
            )
            deleted.raise_for_status()
        url = _next_paged_admin_url(base, payload.get("next"))


def _post_key_auth(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    consumer_seg: str,
    api_key: Optional[str],
) -> Dict[str, Any]:
    # Omit `key` so Kong generates one; include `key` to set an explicit credential.
    payload: Dict[str, str] = {}
    if api_key is not None:
        payload["key"] = api_key
    posted = client.post(
        f"{base}/consumers/{consumer_seg}/key-auth",
        headers=headers,
        json=payload,
    )
    posted.raise_for_status()
    return posted.json()


def _resolve_consumer_id_after_key_post(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    operator: str,
    consumer_id: Optional[str],
) -> Optional[str]:
    if consumer_id:
        return consumer_id
    meta = client.get(_consumer_url(base, _path_segment(operator)), headers=headers)
    meta.raise_for_status()
    return meta.json().get("id")


def _sync_key_auth(
    client: httpx.Client,
    *,
    base: str,
    headers: Dict[str, str],
    consumer_id: Optional[str],
    api_key: Optional[str],
    operator: str,
) -> str:
    ref = consumer_id or operator
    consumer_seg = _path_segment(ref)
    _delete_all_consumer_key_auths(
        client, base=base, headers=headers, consumer_seg=consumer_seg
    )

    body = _post_key_auth(
        client,
        base=base,
        headers=headers,
        consumer_seg=consumer_seg,
        api_key=api_key,
    )
    kong_key = body.get("key")
    if not isinstance(kong_key, str) or not kong_key:
        if api_key is not None:
            # Some Kong builds omit echoing 'key' even when we set it explicitly.
            kong_key = api_key
        else:
            logger.warning(
                "Kong key-auth create omitted key; re-provisioning with explicit secret operator=%r",
                operator,
            )
            _delete_all_consumer_key_auths(
                client, base=base, headers=headers, consumer_seg=consumer_seg
            )
            explicit = secrets.token_urlsafe(_FALLBACK_KEY_BYTES)
            body = _post_key_auth(
                client,
                base=base,
                headers=headers,
                consumer_seg=consumer_seg,
                api_key=explicit,
            )
            kong_key = body.get("key")
            if not isinstance(kong_key, str) or not kong_key:
                kong_key = explicit

    resolved_consumer_id = _resolve_consumer_id_after_key_post(
        client,
        base=base,
        headers=headers,
        operator=operator,
        consumer_id=consumer_id,
    )
    cred_id = body.get("id")
    logger.info(
        "Kong key-auth synced operator=%r consumer_id=%r credential_id=%r",
        operator,
        resolved_consumer_id,
        cred_id,
    )
    return kong_key


class KongIntegration:
    """
    Kong Admin API: ensure a Consumer per operator (username = operator) and sync
    key-auth credentials. The key returned from :meth:`create_or_update_api_key`
    is the credential Kong stored (including auto-generated keys); callers should
    store that same value in `Operator.apikey`.
    """

    def ensure_consumer(
        self, *, operator: str, mail_group: str
    ) -> KongProvisioningResult:
        base = _require_admin_base()
        headers = _admin_headers()
        with httpx.Client(timeout=30.0, verify=False) as client:
            return _ensure_consumer(
                client,
                base=base,
                headers=headers,
                operator=operator,
                mail_group=mail_group,
            )

    def create_or_update_api_key(
        self,
        *,
        consumer_id: Optional[str],
        operator: str,
        api_key: Optional[str] = None,
    ) -> str:
        """
        Replace consumer key-auth credentials with a single key.

        `api_key=None` lets Kong generate the key; otherwise the given value is
        sent to Kong. The returned string is always the credential Kong stored.
        """
        base = _require_admin_base()
        headers = _admin_headers()
        with httpx.Client(timeout=30.0, verify=False) as client:
            return _sync_key_auth(
                client,
                base=base,
                headers=headers,
                consumer_id=consumer_id,
                api_key=api_key,
                operator=operator,
            )

    def delete_consumer(self, *, operator: str) -> None:
        """Delete a consumer and all its credentials from Kong (for rollback)."""
        base = _require_admin_base()
        headers = _admin_headers()
        operator_seg = _path_segment(operator)
        with httpx.Client(timeout=30.0, verify=False) as client:
            # First delete all key-auth credentials
            _delete_all_consumer_key_auths(
                client, base=base, headers=headers, consumer_seg=operator_seg
            )
            # Then delete the consumer
            deleted = client.delete(
                f"{base}/consumers/{operator_seg}",
                headers=headers,
            )
            if deleted.status_code not in (
                httpx.codes.OK,
                httpx.codes.NO_CONTENT,
                httpx.codes.NOT_FOUND,
            ):
                deleted.raise_for_status()
            logger.info("Deleted Kong consumer operator=%r", operator)

    def delete_api_key(self, *, operator: str) -> None:
        """Delete all API key credentials for a consumer (for rollback)."""
        base = _require_admin_base()
        headers = _admin_headers()
        operator_seg = _path_segment(operator)
        with httpx.Client(timeout=30.0, verify=False) as client:
            _delete_all_consumer_key_auths(
                client, base=base, headers=headers, consumer_seg=operator_seg
            )
            logger.info("Deleted Kong API keys for operator=%r", operator)