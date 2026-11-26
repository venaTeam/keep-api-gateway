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
import uuid
from collections.abc import Callable, Collection, Coroutine, Mapping
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from types import MappingProxyType
from typing import Any, Literal
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
from src.services.assistant.models.api.draft import (
    MAX_ALERTS_PER_DRAFT,
    GatewayResult,
)

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


def _is_invalid_cel(detail: Any) -> bool:
    return isinstance(detail, dict) and detail.get("code") == "INVALID_CEL"


def _is_canonical_uuid(value: Any) -> bool:
    """Lower case with hyphens and nothing else: no braces, no `urn:uuid:`."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def classify_batch_enrich(response: httpx.Response) -> GatewayResult:
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


# --- the writes ----------------------------------------------------------------------

EMPTY: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class WriteRoute:
    """One write the assistant may send, and everything it may carry. `write()` checks
    a request against its row before it takes a place: what does not fit is never
    sent."""

    method: Literal["POST", "PUT"]
    # A template: one `{name}` per path parameter.
    path: str
    # Their values must be canonical UUIDs.
    path_params: tuple[str, ...]
    # Allowed keys, each with its allowed values.
    query: Mapping[str, frozenset[str]]
    # The body's exact top-level keys. None only for a route whose body is a list.
    allowed_keys: frozenset[str] | None
    # Raises on a body outside the route's shape; sees the body only after the keys.
    check_body: Callable[[Any], None]
    classify: Callable[[httpx.Response], GatewayResult]
    # The id of what the write made or changed, from a 2xx body.
    object_id: Callable[[Any], str | None]
    # None: ASSISTANT_GATEWAY_WRITE_TIMEOUT_SECONDS.
    timeout_seconds: float | None = None

    def timeout(self) -> float:
        if self.timeout_seconds is None:
            return get_settings().gateway_write_timeout_seconds
        return self.timeout_seconds


@dataclass(frozen=True)
class WriteAnswer:
    result: GatewayResult
    # The parsed answer of a 2xx only: an error body never travels any further.
    body: Any = None
    object_id: str | None = None
    # A 400 whose detail says the CEL is invalid.
    invalid_cel: bool = False


_ENRICHMENT_KEYS = frozenset({"status", "dismiss_mode", "dismissed_until", "note"})


def _check_batch_enrich(body: dict[str, Any]) -> None:
    fingerprints = body["fingerprints"]
    if not (
        isinstance(fingerprints, list)
        and 1 <= len(fingerprints) <= MAX_ALERTS_PER_DRAFT
        and all(isinstance(fp, str) and fp for fp in fingerprints)
    ):
        raise ValueError("fingerprints")
    enrichments = body["enrichments"]
    if not (
        isinstance(enrichments, dict)
        and enrichments.keys() <= _ENRICHMENT_KEYS
        and all(value is None or isinstance(value, str) for value in enrichments.values())
    ):
        raise ValueError("enrichments")


def _no_object(body: Any) -> None:
    return None


_TRUE_OR_FALSE = frozenset({"true", "false"})

WRITE_ROUTES: Mapping[str, WriteRoute] = MappingProxyType(
    {
        # The alerts table's own status change, by fingerprint only: never a `cel`.
        "batch_enrich": WriteRoute(
            method="POST",
            path="/alerts/batch_enrich",
            path_params=(),
            query=MappingProxyType({"dispose_on_new_alert": _TRUE_OR_FALSE}),
            allowed_keys=frozenset({"fingerprints", "enrichments"}),
            check_body=_check_batch_enrich,
            classify=classify_batch_enrich,
            object_id=_no_object,
        ),
    }
)


def _path_of(
    route: WriteRoute,
    body: Any,
    path_params: Mapping[str, str],
    query: Mapping[str, str],
) -> str:
    """The path of a request that fits its route. Raises otherwise."""
    if set(path_params) != set(route.path_params):
        raise ValueError("path parameters")
    if not all(_is_canonical_uuid(value) for value in path_params.values()):
        raise ValueError("path parameter")
    for key, value in query.items():
        if value not in route.query.get(key, ()):
            raise ValueError("query")
    if route.allowed_keys is None:
        if not isinstance(body, list):
            raise ValueError("body")
    elif not isinstance(body, dict) or body.keys() != route.allowed_keys:
        raise ValueError("body keys")
    route.check_body(body)
    return route.path.format_map(dict(path_params))


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

    async def write(
        self,
        caller: Caller,
        endpoint: str,
        *,
        body: dict[str, Any] | list[Any],
        path_params: Mapping[str, str] = EMPTY,
        query: Mapping[str, str] = EMPTY,
        timeout: float | None = None,
    ) -> WriteAnswer:
        """Send one write of `WRITE_ROUTES`. Never retried: a retry would duplicate
        what it made, and the audit rows.

        Raises GatewayUnavailable(sent=False) when nothing can have been written: a
        request outside its route is one. Every other failure is a result, `unknown`
        when the gateway may still be writing. `timeout` can only shorten the
        route's own.
        """
        route = WRITE_ROUTES.get(endpoint)
        try:
            if route is None:
                raise ValueError("endpoint")
            path = _path_of(route, body, path_params, query)
        except Exception:
            # What was asked is not logged: the request is the problem.
            logger.error(
                "gateway: write outside its route, not sent",
                extra={
                    "gateway_request_id": caller.request_id,
                    "endpoint": endpoint if route is not None else None,
                },
            )
            raise GatewayUnavailable("refused", sent=False) from None
        seconds = route.timeout() if timeout is None else min(timeout, route.timeout())
        try:
            response = await self._send(
                caller,
                endpoint,
                route.method,
                path,
                body=body,
                params=dict(query) or None,
                headers={"X-Keep-Source": KEEP_SOURCE},
                timeout=seconds,
                route=route,
            )
        except GatewayUnavailable as exc:
            if not exc.sent:
                raise
            return WriteAnswer("unknown")
        result = route.classify(response)
        status = response.status_code
        if result != "ok":
            logger.warning(
                "gateway: write not ok",
                extra={
                    "gateway_request_id": caller.request_id,
                    "endpoint": endpoint,
                    "status": status,
                    "gateway_result": result,
                },
            )
        parsed = _json(response) if 200 <= status < 300 else None
        return WriteAnswer(
            result=result,
            body=parsed,
            object_id=route.object_id(parsed) if result == "ok" else None,
            invalid_cel=status == 400 and _is_invalid_cel(_detail(response)),
        )

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
        if status == 400 and _is_invalid_cel(detail):
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
        body: dict[str, Any] | list[Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        slot: asyncio.Semaphore | None = None,
        route: WriteRoute | None = None,
    ) -> httpx.Response:
        """`slot` is released when the call itself ends, however long nobody has been
        waiting for it. `route`: the row of a write, checked by `write`."""
        started = time.monotonic()
        status_class = "cancelled"
        try:
            # The last place is for a write: without it a Confirm ends in nothing.
            places = MAX_RUNNING_CALLS - (route is None)
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
                slot,
                is_write=route is not None,
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
        body: dict[str, Any] | list[Any] | None,
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
        slot: asyncio.Semaphore | None,
        *,
        is_write: bool,
    ) -> asyncio.Task[httpx.Response]:
        task = asyncio.create_task(request)
        self._running.add(task)
        if is_write:
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
