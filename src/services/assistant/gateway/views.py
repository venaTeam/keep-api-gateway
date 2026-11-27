"""What the reader hands on of the tenant's lists: never who made or changed a preset,
a rule or a dashboard, a rule's assignee, or the tenant. They are dropped at any depth
(a dashboard embeds presets). What a preset or a rule says about the user is `mine`.
"""
import dataclasses
from typing import Any

# Emails and the tenant: dropped from everything the reader hands on, at any depth.
HIDDEN_KEYS = frozenset({"created_by", "updated_by", "assignee", "tenant_id"})
# Of a rule: what only the gateway's own pages use (a chart and a parse tree).
_RULE_DROPPED = frozenset({"distribution", "definition_cel_ast"})


@dataclasses.dataclass(frozen=True)
class CarriedRuleFields:
    """A rule's fields that an update sends back as they are, and that no tool shows.
    Not JSON, and its repr names no value."""

    assignee: Any = dataclasses.field(default=None, repr=False)
    group_description: Any = dataclasses.field(default=None, repr=False)
    multi_level: Any = dataclasses.field(default=None, repr=False)
    multi_level_property_name: Any = dataclasses.field(default=None, repr=False)


CARRIED_RULE_KEYS = frozenset(f.name for f in dataclasses.fields(CarriedRuleFields))


@dataclasses.dataclass(frozen=True)
class RuleRead:
    # What a tool may show: no email, no tenant, nothing of `carried`.
    fields: dict[str, Any]
    carried: CarriedRuleFields = dataclasses.field(repr=False)


def without_people(value: Any) -> Any:
    """`value` without HIDDEN_KEYS, at any depth."""
    if isinstance(value, dict):
        return {
            key: without_people(item)
            for key, item in value.items()
            if key not in HIDDEN_KEYS
        }
    if isinstance(value, list):
        return [without_people(item) for item in value]
    return value


def _mine(row: dict[str, Any], email: str | None) -> bool:
    return bool(email) and row.get("created_by") == email


def preset_view(row: dict[str, Any], email: str | None) -> dict[str, Any]:
    """A preset without people, and whether the user made it."""
    return {**without_people(row), "mine": _mine(row, email)}


def rule_read(row: dict[str, Any], email: str | None) -> RuleRead:
    shown = {
        key: value
        for key, value in row.items()
        if key not in _RULE_DROPPED and key not in CARRIED_RULE_KEYS
    }
    return RuleRead(
        fields={**without_people(shown), "mine": _mine(row, email)},
        carried=CarriedRuleFields(
            assignee=row.get("assignee"),
            group_description=row.get("group_description"),
            multi_level=row.get("multi_level"),
            multi_level_property_name=row.get("multi_level_property_name"),
        ),
    )
