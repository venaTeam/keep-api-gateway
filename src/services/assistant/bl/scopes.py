"""Scope hint for the draft card. The gateway decides on Confirm."""
from collections.abc import Collection


def has_scope(scopes: Collection[str] | None, scope: str) -> bool:
    """Mirrors the gateway's `rbac.Role.has_scopes`: an exact match, or `verb:*`.

    None means the gateway could not name the caller's scopes: not a refusal.
    """
    if scopes is None or scope in scopes:
        return True
    parts = scope.split(":")
    return len(parts) == 2 and f"{parts[0]}:*" in scopes
