"""Client of the gateway's own routes, which are the only authority on permissions:
every call carries the end user's own token and nothing else of the caller's request.

A call runs in a task of its own. A deadline or a disconnected user ends the wait for
it and never the call: a write must not be cut off half way through its rows.
"""
import asyncio
import json
import logging
import re
import time
from collections.abc import Collection, Coroutine
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
from src.services.assistant.gateway.transport import CLIENT_EXTENSION, UNKNOWN_CLIENT
from src.services.assistant.models.api.draft import GatewayResult, TargetStatus

logger = logging.getLogger(__name__)

KEEP_SOURCE = "assistant"

# Running calls of the whole process, abandoned ones included: the gateway's sync
# routes share one threadpool (40 threads) with every other request of the UI.
MAX_RUNNING_CALLS = 8

# Of those, calls whose time depends on a filter the model wrote (or on every alert of
# the tenant). They may be slow, and go on when nobody waits any more: the others - who
# is asking, the alerts of a draft, and the write - must still find a place.
MAX_FILTER_CALLS = 4

# Single-alert reads of the whole process: each one makes the gateway call its
# identity provider. Below the cap above, so that a chat's own lookup fits in it.
MAX_CONCURRENT_ALERT_READS = 4

# A blocked event loop fires a deadline late, and the answer is then only a few loop
# iterations away.
LATE_ANSWER_SECONDS = 0.1

_WRITE_ENDPOINT = "batch_enrich"

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
    # The address of the user's own request: what the gateway's rate limiter counts.
    client: tuple[str, int] = UNKNOWN_CLIENT


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
        self._filter_calls = asyncio.Semaphore(MAX_FILTER_CALLS)
        # Strong references: the event loop itself only holds weak ones.
        self._running: set[asyncio.Task[httpx.Response]] = set()
        self._writes: set[asyncio.Task[httpx.Response]] = set()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def wait_for_writes(self, timeout: float) -> int:
        """Give writes that nobody waits for any more the time to finish. How many
        are still running."""
        if self._writes:
            await asyncio.wait(set(self._writes), timeout=timeout)
        return len(self._writes)

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
        slot = await self._filter_place(caller, "query")
        return await self._query(caller, cel, limit, slot)

    async def _query(
        self,
        caller: Caller,
        cel: str,
        limit: int,
        slot: asyncio.Semaphore | None = None,
    ) -> list[FetchedAlert]:
        response = await self._read(
            caller,
            "query",
            "POST",
            "/alerts/query",
            body={"cel": cel, "limit": limit},
            slot=slot,
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
                caller,
                "count",
                "POST",
                "/alerts/query/count",
                body={"cel": cel},
                slot=await self._filter_place(caller, "count"),
            )
        except GatewayError:
            return None  # counted and logged by _read
        body = _json(response)
        if isinstance(body, int) and not isinstance(body, bool):
            return body
        self._bad_response("count")
        return None

    async def alert_fields(self, caller: Caller) -> list[str]:
        response = await self._read(
            caller,
            "fields",
            "GET",
            "/alerts/facets/fields",
            slot=await self._filter_place(caller, "fields"),
        )
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
                alerts = await self._query(
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
            # These only wait: a read that is already running has its own task.
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
        await self._alert_reads.acquire()
        response = await self._read(
            caller,
            "alert",
            "GET",
            f"/alerts/{quote(fingerprint, safe='')}",
            not_found_ok=True,
            slot=self._alert_reads,
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
                _WRITE_ENDPOINT,
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

    async def _filter_place(self, caller: Caller, endpoint: str) -> asyncio.Semaphore:
        """A place among the filter calls, for `slot`. Waits for one as long as for an
        answer."""
        try:
            async with asyncio.timeout(get_settings().gateway_read_timeout_seconds):
                await self._filter_calls.acquire()
        except TimeoutError:
            metrics.gateway_requests_total.labels(
                endpoint=endpoint, status_class="not_sent"
            ).inc()
            raise self._unsuccessful(
                caller, endpoint, "not_sent", "TooManyCalls", sent=False
            ) from None
        return self._filter_calls

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
        slot: asyncio.Semaphore | None = None,
    ) -> httpx.Response:
        """A read whose failures are GatewayErrors."""
        response = await self._send(
            caller,
            endpoint,
            method,
            path,
            body=body,
            slot=slot,
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
        slot: asyncio.Semaphore | None = None,
    ) -> httpx.Response:
        """`slot` is released when the call itself ends, however long nobody has been
        waiting for it."""
        started = time.monotonic()
        status_class = "cancelled"
        try:
            # The last place is for the write: without it a Confirm ends in nothing.
            places = MAX_RUNNING_CALLS - (endpoint != _WRITE_ENDPOINT)
            # No await between the check and the start, so the cap holds.
            if len(self._running) >= places:
                if slot is not None:
                    slot.release()
                status_class = "not_sent"
                raise self._unsuccessful(
                    caller, endpoint, status_class, "TooManyCalls", sent=False
                )
            task = self._start(
                self._request(caller, method, path, timeout, body, params, headers),
                endpoint,
                slot,
            )
            try:
                await asyncio.wait({task}, timeout=timeout)
                if not task.done():
                    await asyncio.wait({task}, timeout=LATE_ANSWER_SECONDS)
            finally:
                if not task.done():
                    self._log_when_done(task, caller, endpoint, started)
            if not task.done():
                status_class = "timeout"
                raise self._unsuccessful(
                    caller, endpoint, status_class, "TimeoutError", sent=True
                )
            exc = asyncio.CancelledError() if task.cancelled() else task.exception()
            if exc is None:
                response = task.result()
                status_class = f"{response.status_code // 100}xx"
                return response
            sent = not isinstance(exc, _NEVER_SENT)
            if not sent:
                status_class = "not_sent"
            elif isinstance(exc, httpx.TimeoutException):
                status_class = "timeout"
            else:
                status_class = "dropped"
            raise self._unsuccessful(
                caller, endpoint, status_class, type(exc).__name__, sent=sent
            )
        finally:
            metrics.gateway_requests_total.labels(
                endpoint=endpoint, status_class=status_class
            ).inc()
            metrics.gateway_request_seconds.labels(endpoint=endpoint).observe(
                time.monotonic() - started
            )

    async def _request(
        self,
        caller: Caller,
        method: str,
        path: str,
        timeout: float,
        body: dict[str, Any] | None,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        return await self._http.request(
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
            # Only a network transport looks at it. The pool and connect limits are
            # shorter, so a write that was never sent is reported as such and not as
            # an unknown result.
            timeout=httpx.Timeout(
                timeout, connect=min(3.0, timeout), pool=min(2.0, timeout)
            ),
            extensions={CLIENT_EXTENSION: caller.client},
        )

    def _start(
        self,
        request: Coroutine[Any, Any, httpx.Response],
        endpoint: str,
        slot: asyncio.Semaphore | None,
    ) -> asyncio.Task[httpx.Response]:
        task = asyncio.create_task(request)
        self._running.add(task)
        if endpoint == _WRITE_ENDPOINT:
            self._writes.add(task)

        def ended(task: asyncio.Task[httpx.Response]) -> None:
            self._running.discard(task)
            self._writes.discard(task)
            if slot is not None:
                slot.release()
            if not task.cancelled():
                # Marks it as retrieved: asyncio would log the text of the exception
                # of an abandoned call.
                task.exception()

        task.add_done_callback(ended)
        return task

    def _unsuccessful(
        self, caller: Caller, endpoint: str, reason: str, error_type: str, *, sent: bool
    ) -> GatewayUnavailable:
        logger.warning(
            "gateway: request error",
            extra={
                "gateway_request_id": caller.request_id,
                "endpoint": endpoint,
                "error_type": error_type,
                "sent": sent,
            },
        )
        return GatewayUnavailable(reason, sent=sent)

    def _log_when_done(
        self,
        task: asyncio.Task[httpx.Response],
        caller: Caller,
        endpoint: str,
        started: float,
    ) -> None:
        def ended(task: asyncio.Task[httpx.Response]) -> None:
            exc = asyncio.CancelledError() if task.cancelled() else task.exception()
            logger.warning(
                "gateway: abandoned call ended",
                extra={
                    "gateway_request_id": caller.request_id,
                    "endpoint": endpoint,
                    "status": task.result().status_code if exc is None else None,
                    "error_type": None if exc is None else type(exc).__name__,
                    "seconds": round(time.monotonic() - started, 1),
                },
            )

        task.add_done_callback(ended)

    def _bad_response(self, endpoint: str) -> GatewayUnavailable:
        logger.warning("gateway: unusable answer", extra={"endpoint": endpoint})
        metrics.internal_errors_total.labels(site=f"gateway_{endpoint}").inc()
        return GatewayUnavailable("bad_response")
