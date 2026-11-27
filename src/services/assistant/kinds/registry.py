"""Every draft kind this version knows. A kind that is not here (a newer version's,
after a rollback) is never confirmed or looked at; only the 24 h sweep closes it.

A kind can also be switched off for the life of a process: by ASSISTANT_DISABLED_KINDS,
or by `disable_kinds` from the lifespan. It stays here, so its drafts still reconcile,
expire and can be discarded, but its tool is not offered and Confirm refuses it with
409 draft_unsupported. `disabled_kinds()` alone says which kinds are off.
"""
import functools
import logging
import re
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Optional

from pydantic import BaseModel

from src.services.assistant.config import get_settings
from src.services.assistant.core import metrics
from src.services.assistant.kinds import alert_status
from src.services.assistant.kinds.base import DraftKind

logger = logging.getLogger(__name__)

KINDS: Mapping[str, DraftKind] = MappingProxyType(
    {kind.name: kind for kind in (alert_status.KIND,)}
)

# kind -> payload model.
PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {name: kind.payload_model for name, kind in KINDS.items()}
)

DISABLED_KINDS_SETTING = "ASSISTANT_DISABLED_KINDS"
_REASON = re.compile(r"[a-z_]{1,40}")
_SEPARATORS = re.compile(r"[\s,]+")

# kind -> the reason it was switched off by `disable_kinds`. It only ever grows.
_disabled_by_code: dict[str, str] = {}


def kind_of(name: str) -> Optional[DraftKind]:
    """None: a kind this version does not know."""
    return KINDS.get(name)


def disabled_kinds() -> frozenset[str]:
    """The kinds switched off in this process: those ASSISTANT_DISABLED_KINDS names and
    those `disable_kinds` was given. The chat's tools, Confirm and the
    keep_assistant_kind_enabled gauge all read this."""
    return _kinds_in_setting(get_settings().disabled_kinds) | frozenset(
        _disabled_by_code
    )


def is_disabled(name: str) -> bool:
    return name in disabled_kinds()


def disable_kinds(names: Collection[str], reason: str) -> None:
    """Switch these kinds off for the rest of this process, as ASSISTANT_DISABLED_KINDS
    does: for a kind that cannot run on this deployment. Called from the lifespan
    (lifecycle.start), before serving.

    Registered kind names only. `reason` is a fixed identifier, never free text; it is
    logged once per kind. There is no way to switch a kind on again: a later call can
    only add kinds. Raises, and changes nothing, on an unknown name or a bad reason.
    """
    if isinstance(names, str):
        raise TypeError("names: a collection of kind names, not one name")
    names = list(names)
    unknown = sorted(name for name in names if name not in KINDS)
    if unknown:
        raise ValueError(f"not a registered draft kind: {unknown}")
    if not isinstance(reason, str) or not _REASON.fullmatch(reason):
        raise ValueError("reason: 1 to 40 of a-z and _")
    for name in names:
        if name in _disabled_by_code:
            continue
        _disabled_by_code[name] = reason
        metrics.kind_enabled.labels(kind=name).set(0)
        logger.warning(
            "assistant: a draft kind is switched off",
            extra={"kind": name, "reason": reason},
        )


@functools.lru_cache(maxsize=8)
def _kinds_in_setting(raw: str) -> frozenset[str]:
    """The kinds a value of ASSISTANT_DISABLED_KINDS names, separated by commas or
    spaces. A name that is no kind is logged, once, and ignored: a typo must not take
    the gateway down."""
    kinds: set[str] = set()
    ignored: set[str] = set()
    for name in _SEPARATORS.split(raw):
        if not name or name in kinds or name in ignored:
            continue
        if name in KINDS:
            kinds.add(name)
            continue
        ignored.add(name)
        logger.error(
            "assistant: a name in a setting is no draft kind, ignored",
            extra={"setting": DISABLED_KINDS_SETTING, "kind": name[:64]},
        )
    return frozenset(kinds)
