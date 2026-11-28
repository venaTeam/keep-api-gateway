"""A link to a page of Keep, which the user clicks: nothing opens by itself.

The model names a page and what it shows; the path is built by navigation.py alone,
after the page's object was found with the user's own reads. A NOC user is offered
the alerts pages only, as keep-ui sends them back to the feed from anywhere else. A
rule's page costs one unit of the turn's filter budget (its read counts incidents);
the other checks cost none.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, constr, validator

from src.services.assistant import navigation
from src.services.assistant.bl.projection import visible_text
from src.services.assistant.bl.scopes import has_scope
from src.services.assistant.models.api.chat import storable
from src.services.assistant.models.api.stream import LinkPage
from src.services.assistant.models.identity import Identity
from src.services.assistant.tools.base import (
    READ_FAILURES,
    FilterBudgetSpent,
    NewLink,
    ToolContext,
    ToolOutcome,
    ToolSpec,
    tool_error,
)
from src.services.assistant.tools.read_common import (
    budget_spent,
    diagnostics,
    parse,
    read_failure,
)

NAME = "open_page"
PAGES = tuple(navigation.PAGES)
MAX_LABEL_CHARS = 80
MAX_PARAM_CHARS = navigation.MAX_CEL_CHARS
NOC_ROLE = "noc"
# A write of any of these makes a user more than a NOC user, when whoami names no role.
_NOT_ONLY_ALERTS = (
    "write:presets",
    "write:rules",
    "update:rules",
    "write:dashboards",
    "write:incident",
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page", "param", "label"],
    "properties": {
        "page": {
            "type": "string",
            "enum": list(PAGES),
            "description": (
                "alerts_feed: every alert. alerts_preset: a preset's alerts. "
                "alerts_search: the alerts a CEL filter matches. incidents: the "
                "incidents. rules: the correlation rules. rule: one correlation rule. "
                "dashboard: one dashboard."
            ),
        },
        "param": {
            "type": ["string", "null"],
            "description": (
                "alerts_preset: the preset's name. alerts_search: the CEL filter. "
                "rule: the rule's id. dashboard: the dashboard's name. Null for the "
                "other pages."
            ),
        },
        "label": {
            "type": "string",
            "description": f"The link's text, at most {MAX_LABEL_CHARS} chars.",
        },
    },
}

PROMPT_GUIDANCE = """\
open_page:
- Make a link to a page of Keep that the user can click, when they ask to go to or \
see a page, or after you found something they will want to look at. Never write a \
path or a URL yourself. Names and ids must be ones a list tool or the context gave \
you. A NOC user can only be sent to the alerts pages (page_not_available). After it, \
say what the link opens; do not repeat its path."""


class OpenPageArgs(BaseModel):
    page: Literal[PAGES]  # type: ignore[valid-type]
    param: Optional[constr(max_length=MAX_PARAM_CHARS)] = None
    label: constr(strip_whitespace=True, min_length=1, max_length=MAX_LABEL_CHARS)

    class Config:
        extra = "forbid"

    @validator("param", "label")
    def _storable(cls, value):
        return storable(value) if value is not None else None


def is_noc(identity: Identity) -> bool:
    """keep-ui's own rule (its middleware): the role "noc". When whoami names no role,
    scopes that allow no write beyond alerts count the same."""
    if identity.role is not None:
        return identity.role == NOC_ROLE
    scopes = identity.scopes
    return scopes is not None and not any(
        has_scope(scopes, scope) for scope in _NOT_ONLY_ALERTS
    )


def may_open(identity: Identity, page: str) -> bool:
    return navigation.noc_allowed(page) or not is_noc(identity)


def _not_found() -> ToolOutcome:
    return ToolOutcome("not_found", {"status": "not_found"})


async def _checked_param(
    context: ToolContext, page: str, param: str
) -> str | ToolOutcome:
    """The parameter as the page takes it, once its object is found."""
    reader = context.gateway
    if page == "alerts_preset":
        # keep-ui finds the preset by its lowercased name, as build writes it.
        wanted = param.lower()
        for preset in await context.read_once(reader.presets):
            name = preset.get("name")
            if isinstance(name, str) and name.lower() == wanted:
                return name
        return _not_found()
    if page == "dashboard":
        for dashboard in await context.read_once(reader.dashboards):
            if dashboard.get("dashboard_name") == param:
                return param
        return _not_found()
    if page == "rule":
        rules = await context.read_once(reader.rules, filter_units=1)
        if any(str(rule.fields.get("id")) == param for rule in rules):
            return param
        return _not_found()
    if page == "alerts_search":
        answer = await context.read_once(reader.check_cel, param, "alerts")
        if not answer["valid"]:
            return ToolOutcome(
                "invalid_cel",
                {
                    "status": "invalid_cel",
                    "diagnostics": diagnostics(answer.get("diagnostics")),
                },
            )
        return param
    raise ValueError(page)  # pragma: no cover - every page with a param is above


async def run(context: ToolContext, arguments: str) -> ToolOutcome:
    args = parse(OpenPageArgs, arguments)
    if isinstance(args, ToolOutcome):
        return args
    page: LinkPage = args.page
    if not may_open(context.identity, page):
        return tool_error("page_not_available")
    takes_param = navigation.PAGES[page].param is not None
    if not takes_param:
        if args.param is not None:
            return tool_error("param_not_used")
        param = None
    else:
        try:
            # Checked as the page reads it before anything is read.
            navigation.build(page, args.param)
        except ValueError:
            return tool_error("param_not_usable")
        try:
            checked = await _checked_param(context, page, args.param)
        except FilterBudgetSpent:
            return budget_spent()
        except READ_FAILURES as exc:
            return read_failure(exc)
        if isinstance(checked, ToolOutcome):
            return checked
        param = checked
    path = navigation.build(page, param)
    # The parameter as the page reads it back (a preset's name lowercased), so that
    # the stored link and the event say the same as the path.
    page, param = navigation.parse(path)  # type: ignore[misc]
    label = visible_text(args.label).strip()
    if not label:
        return tool_error("label_not_visible")
    link = NewLink(page=page, param=param, path=path, label=label)
    return ToolOutcome(
        "link_ready",
        {
            "status": "link_ready",
            "link": {"page": page, "param": param, "path": path, "label": label},
        },
        link=link,
    )


SPEC = ToolSpec(
    name=NAME,
    description=(
        "Make a link to a page of Keep for the user to click. Opens nothing by itself "
        "and changes nothing."
    ),
    parameters=PARAMETERS,
    label="Preparing a link",
    prompt_guidance=PROMPT_GUIDANCE,
    run=run,
    makes_link=True,
)
