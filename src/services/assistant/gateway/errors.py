"""Failures of a gateway call. None of them carries response text."""
from typing import Any


class GatewayError(Exception):
    pass


class GatewayUnauthorized(GatewayError):
    """401: the forwarded token is not (or no longer) valid."""


class GatewayForbidden(GatewayError):
    """403. `read_only` when the whole instance refuses writes."""

    def __init__(self, read_only: bool = False):
        super().__init__("read_only" if read_only else "forbidden")
        self.read_only = read_only


class InvalidCel(GatewayError):
    """The gateway rejected a CEL filter. Diagnostics are untrusted text."""

    def __init__(self, diagnostics: list[dict[str, Any]]):
        super().__init__("invalid_cel")
        self.diagnostics = diagnostics


class GatewayUnavailable(GatewayError):
    """Timeout, transport failure or an unusable answer.

    `sent` is False only when the request certainly never reached the gateway.
    """

    def __init__(self, reason: str, sent: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.sent = sent


class QueryFailed(GatewayUnavailable):
    """500 on a CEL query: the expression itself can cause it."""

    def __init__(self):
        super().__init__("query_failed")


class GatewayIncompatible(GatewayError):
    """The gateway answered with a shape this version does not understand."""
