"""Client of the gateway's own routes, which are the only authority on permissions:
every call carries the end user's own token and nothing else of the caller's request.
"""
import asyncio
import json
import logging
import re
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import quote

import httpx

from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.gateway.errors import (
    GatewayError,
    GatewayForbidden,
    GatewayIncompatible,
    GatewayUnauthorized,
    GatewayUnavailable,
    InvalidCel,
    QueryFailed,
)
from src.services.assistant.models.api.draft import GatewayResult, TargetStatus

logger = logging.getLogger(__name__)

KEEP_SOURCE = "assistant"

# Single-alert reads of the whole process: each one makes the gateway call its
# identity provider.
MAX_CONCURRENT_ALERT_READS = 10

_LIMITS = httpx.Limits(
    max_connections=50,
    max_keepalive_connections=20,
    # Below the gateway's own 5 s: a write must not go out on a connection the
    # gateway is just closing, because its result would be unknown.
    keepalive_expiry=2.0,
)

# These happen before the first byte of the request is written.
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
    UnicodeEncodeError,
)

# Fingerprints that mean the same inside a CEL string literal as outside it. A
# colon only after a word character: elsewhere the gateway's SQL layer would take
# it for a bind parameter.
_CEL_SAFE = re.compile(r"(?:[0-9A-Za-z_./-]|(?<=[0-9A-Za-z_]):){1,512}")
# The gateway casts date-like literals to timestamps.
_DATE_PREFIX = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Slashes, control characters, and lone surrogates that UTF-8 cannot carry.
_NOT_IN_PATH = re.compile(r"[/\\\x00-\x1f\x7f-\x9f\ud800-\udfff]")


@dataclass(frozen=True)
class Caller:
    """On whose behalf a call is made. `request_id` must be a bare UUID."""

    authorization: str = field(repr=False)
    request_id: str


@dataclass(frozen=True)
class FetchedAlert:
    data: dict[str, Any]
    # The single-alert endpoint never returns incidents, so "no incident" in its
    # answer means "unknown".
    incident_known: bool

    @property
    def fingerprint(self) -> str:
        return self.data["fingerprint"]


@dataclass(frozen=True)
class FetchResult:
    found: dict[str, FetchedAlert]
    not_found: list[str]
    # No safe way to look these up.
    unresolvable: list[str]


def can_use_cel(fingerprint: str) -> bool:
    return bool(
        _CEL_SAFE.fullmatch(fingerprint) and not _DATE_PREFIX.match(fingerprint)
    )


def can_use_path(fingerprint: str) -> bool:
    """`%2F` is decoded before routing, and dot segments are collapsed: such a
    fingerprint would reach another route."""
    return fingerprint not in ("", ".", "..") and not _NOT_IN_PATH.search(fingerprint)


def _json(response: httpx.Response) -> Any:
    """The body as JSON, or None when it is not JSON (plain text, empty, HTML)."""
    try:
        return json.loads(response.content)
    except ValueError:
        return None


def _detail(response: httpx.Response) -> Any:
    """`detail` may be a string, a dict, a list or absent: check before using."""
    body = _json(response)
    return body.get("detail") if isinstance(body, dict) else None


def _is_read_only(detail: Any) -> bool:
    return isinstance(detail, str) and "Read only" in detail


def classify_write(response: httpx.Response) -> GatewayResult:
    """What a batch_enrich answer says about the write."""
    status = response.status_code
    if status == 200:
        body = _json(response)
        outcome = body.get("status") if isinstance(body, dict) else None
        if outcome == "ok":
            return "ok"
        if outcome == "failed":
            return "failed_response"
        return "http_error"
    if status == 400:
        return "rejected"
    if status == 401:
        return "unauthenticated"
    if status == 403:
        return "read_only" if _is_read_only(_detail(response)) else "not_permitted"
    if status == 422:
        # A list is FastAPI's own validation, before any write. Anything else can
        # follow rows the gateway has already committed.
        if isinstance(_detail(response), list):
            return "rejected"
        return "rejected_maybe_written"
    if status in (502, 503, 504):
        return "unknown"
    return "http_error"


class GatewayClient:
    def __init__(
        self, base_url: str, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._base_url = base_url
        # The client is shared by all users: a cookie jar that accepts nothing.
        no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            limits=_LIMITS,
            cookies=no_cookies,
            headers={"User-Agent": "keep-gateway-assistant"},
        )
        self._alert_reads = asyncio.Semaphore(MAX_CONCURRENT_ALERT_READS)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- reads ---------------------------------------------------------------

    async def whoami(self, caller: Caller) -> dict[str, Any]:
        response = await self._read(
            caller,
            "whoami",
            "GET",
            "/whoami",
            timeout=get_settings().gateway_whoami_timeout_seconds,
        )
        body = _json(response)
        if not isinstance(body, dict):
            raise GatewayIncompatible()
        return body

    async def query_alerts(
        self, caller: Caller, cel: str, limit: int
    ) -> list[FetchedAlert]:
        response = await self._read(
            caller,
            "query",
            "POST",
            "/alerts/query",
            body={"cel": cel, "limit": limit},
        )
        body = _json(response)
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or not all(
            isinstance(item, dict) and isinstance(item.get("fingerprint"), str)
            for item in results
        ):
            raise self._bad_response("query")
        return [FetchedAlert(item, incident_known=True) for item in results]

    async def count_alerts(self, caller: Caller, cel: str) -> int | None:
        """The exact number of matches, or None when it cannot be had."""
        try:
            response = await self._read(
                caller, "count", "POST", "/alerts/query/count", body={"cel": cel}
            )
        except GatewayError:
            return None  # counted and logged by _read
        body = _json(response)
        if isinstance(body, int) and not isinstance(body, bool):
            return body
        self._bad_response("count")
        return None

    async def alert_fields(self, caller: Caller) -> list[str]:
        response = await self._read(caller, "fields", "GET", "/alerts/facets/fields")
        body = _json(response)
        if not isinstance(body, list):
            raise self._bad_response("fields")
        return [name for name in body if isinstance(name, str)]

    async def fetch_alerts_by_fingerprints(
        self,
        caller: Caller,
        fingerprints: Collection[str],
        *,
        deadline_seconds: float | None = None,
        known_missing: Collection[str] = (),
    ) -> FetchResult:
        """Current state of these alerts, under one overall deadline.

        One CEL query for the fingerprints it can carry safely, then single reads
        for the rest and for whatever the query did not return (a "Hide From Feed"
        window hides an alert from queries). `known_missing`: fingerprints an
        earlier read already reported missing; they are not read singly again.
        """
        deadline = (
            get_settings().gateway_read_timeout_seconds
            if deadline_seconds is None
            else deadline_seconds
        )
        try:
            async with asyncio.timeout(deadline):
                return await self._fetch(
                    caller, list(dict.fromkeys(fingerprints)), set(known_missing)
                )
        except TimeoutError:
            logger.warning(
                "gateway: alert lookup deadline exceeded",
                extra={
                    "gateway_request_id": caller.request_id,
                    "fingerprint_count": len(fingerprints),
                    "deadline_seconds": deadline,
                },
            )
            raise GatewayUnavailable("timeout") from None

    async def _fetch(
        self, caller: Caller, fingerprints: list[str], known_missing: set[str]
    ) -> FetchResult:
        found: dict[str, FetchedAlert] = {}
        in_query = [fp for fp in fingerprints if can_use_cel(fp)]
        if in_query:
            wanted = set(in_query)
            try:
                alerts = await self.query_alerts(
                    # The charset makes a JSON string a CEL string.
                    caller,
                    f"fingerprint in {json.dumps(in_query)}",
                    len(in_query),
                )
            except (QueryFailed, InvalidCel):
                alerts = []  # every one of them goes through a single read
            for alert in alerts:
                if alert.fingerprint in wanted:
                    found[alert.fingerprint] = alert

        unresolvable: list[str] = []
        to_read: list[str] = []
        for fp in fingerprints:
            if fp in found or fp in known_missing:
                continue
            (to_read if can_use_path(fp) else unresolvable).append(fp)

        tasks = [asyncio.create_task(self._get_alert(caller, fp)) for fp in to_read]
        try:
            answers = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        missing = known_missing - found.keys()
        for fp, answer in zip(to_read, answers):
            if answer is None:
                missing.add(fp)
            elif answer.get("fingerprint") != fp:
                # Another route's answer.
                unresolvable.append(fp)
            else:
                found[fp] = FetchedAlert(answer, incident_known=False)
        return FetchResult(
            found={fp: found[fp] for fp in fingerprints if fp in found},
            not_found=[fp for fp in fingerprints if fp in missing],
            unresolvable=[fp for fp in fingerprints if fp in unresolvable],
        )

    async def _get_alert(
        self, caller: Caller, fingerprint: str
    ) -> dict[str, Any] | None:
        """One alert; None for a 404, an empty dict for a foreign answer."""
        async with self._alert_reads:
            response = await self._read(
                caller,
                "alert",
                "GET",
                f"/alerts/{quote(fingerprint, safe='')}",
                not_found_ok=True,
            )
        if response.status_code == 404:
            return None
        body = _json(response)
        return body if isinstance(body, dict) else {}

    # --- the write -----------------------------------------------------------

    async def batch_enrich_status(
        self,
        caller: Caller,
        fingerprints: list[str],
        status: TargetStatus,
        note: str | None,
        dispose_on_new_alert: bool,
        *,
        timeout: float | None = None,
    ) -> GatewayResult:
        """Change the status of these alerts. Never retried: a retry would
        duplicate the audit rows.

        Raises GatewayUnavailable(sent=False) when nothing can have been written.
        Every other failure is a result, `unknown` when the gateway may still be
        writing.
        """
        enrichments: dict[str, Any] = {"status": status}
        if status != "suppressed":
            enrichments["dismiss_mode"] = None
            enrichments["dismissed_until"] = None
        if note:
            enrichments["note"] = note
        try:
            response = await self._send(
                caller,
                "batch_enrich",
                "POST",
                "/alerts/batch_enrich",
                body={"fingerprints": fingerprints, "enrichments": enrichments},
                params={
                    "dispose_on_new_alert": "true" if dispose_on_new_alert else "false"
                },
                headers={"X-Keep-Source": KEEP_SOURCE},
                timeout=(
                    get_settings().gateway_write_timeout_seconds
                    if timeout is None
                    else timeout
                ),
            )
        except GatewayUnavailable as exc:
            if not exc.sent:
                raise
            return "unknown"
        result = classify_write(response)
        if result != "ok":
            logger.warning(
                "gateway: batch_enrich not ok",
                extra={
                    "gateway_request_id": caller.request_id,
                    "status": response.status_code,
                    "gateway_result": result,
                },
            )
        return result

    # --- transport -----------------------------------------------------------

    async def _read(
        self,
        caller: Caller,
        endpoint: str,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
        not_found_ok: bool = False,
    ) -> httpx.Response:
        """A read whose failures are GatewayErrors."""
        response = await self._send(
            caller,
            endpoint,
            method,
            path,
            body=body,
            timeout=(
                get_settings().gateway_read_timeout_seconds
                if timeout is None
                else timeout
            ),
        )
        status = response.status_code
        if status == 200 or (status == 404 and not_found_ok):
            return response
        logger.warning(
            "gateway: request failed",
            extra={
                "gateway_request_id": caller.request_id,
                "endpoint": endpoint,
                "status": status,
            },
        )
        if status == 401:
            raise GatewayUnauthorized()
        detail = _detail(response)
        if status == 403:
            raise GatewayForbidden(read_only=_is_read_only(detail))
        if (
            status == 400
            and isinstance(detail, dict)
            and detail.get("code") == "INVALID_CEL"
        ):
            diagnostics = detail.get("diagnostics")
            raise InvalidCel(
                [item for item in diagnostics if isinstance(item, dict)]
                if isinstance(diagnostics, list)
                else []
            )
        if status == 500 and endpoint in ("query", "count"):
            raise QueryFailed()
        raise GatewayUnavailable(f"status_{status}")

    async def _send(
        self,
        caller: Caller,
        endpoint: str,
        method: str,
        path: str,
        *,
        timeout: float,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        started = time.monotonic()
        status_class = "dropped"
        try:
            # httpx timeouts are per operation; this bounds the whole call. The
            # pool and connect limits are shorter, so a write that was never sent
            # is reported as such and not as an unknown result.
            async with asyncio.timeout(timeout):
                response = await self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    json=body,
                    params=params,
                    headers={
                        # Opaque, byte for byte (ASGI decodes headers as latin-1).
                        "Authorization": caller.authorization.encode("latin-1"),
                        "X-Request-ID": caller.request_id,
                        **(headers or {}),
                    },
                    timeout=httpx.Timeout(
                        timeout, connect=min(3.0, timeout), pool=min(2.0, timeout)
                    ),
                )
            status_class = f"{response.status_code // 100}xx"
            return response
        except (
            TimeoutError,
            httpx.HTTPError,
            httpx.InvalidURL,
            UnicodeEncodeError,
        ) as exc:
            sent = not isinstance(exc, _NEVER_SENT)
            if not sent:
                status_class = "not_sent"
            elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                status_class = "timeout"
            logger.warning(
                "gateway: request error",
                extra={
                    "gateway_request_id": caller.request_id,
                    "endpoint": endpoint,
                    "error_type": type(exc).__name__,
                    "sent": sent,
                },
            )
            raise GatewayUnavailable(status_class, sent=sent) from None
        except asyncio.CancelledError:
            status_class = "cancelled"
            raise
        finally:
            metrics.gateway_requests_total.labels(
                endpoint=endpoint, status_class=status_class
            ).inc()
            metrics.gateway_request_seconds.labels(endpoint=endpoint).observe(
                time.monotonic() - started
            )

    def _bad_response(self, endpoint: str) -> GatewayUnavailable:
        logger.warning("gateway: unusable answer", extra={"endpoint": endpoint})
        metrics.internal_errors_total.labels(site=f"gateway_{endpoint}").inc()
        return GatewayUnavailable("bad_response")
