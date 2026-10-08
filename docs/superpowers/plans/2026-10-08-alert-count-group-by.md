# Count-by for the Alert Count Panel and Alert Table widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the preset dashboard widgets (Alert Count Panel and Alert Table) count distinct values of an allowlisted field instead of alerts, with distinct live incidents as the headline case.

**Architecture:** The gateway's `POST /alerts/query/count` gains an optional `group_by` (closed enum) and `incident_status`; a grouped request runs `COUNT(DISTINCT …)` over a dedicated, correct incident join. keep-ui stores the choice as `countBy` in the widget JSON, adds a segmented "Count" control to the preset widget form, and renders the grouped number with a dot caption (tile) or a separate count line (table widget). No migrations.

**Tech Stack:** Python 3 / FastAPI / SQLModel + SQLAlchemy 2.0 / Pydantic v1.10 / pytest (gateway); Next.js 15, React 19, TypeScript, Tremor 3.18.7, SWR, Jest + Testing Library (UI).

**Spec:** `docs/superpowers/specs/2026-10-08-alert-count-group-by-design.md` in the gateway worktree, with the visual mock `docs/superpowers/specs/2026-10-08-alert-count-group-by-mock.html` next to it. Read both first; the plan argues from them.

## Environment (already prepared)

- **Gateway worktree:** `/Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway`, branch `feat/alert-count-group-by`, cut from `origin/dev`. Holds the spec, the mock and this plan. Interpreter: `GW_PY=/Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python`; no `.venv` symlink is needed. Baseline (probe): `tests/test_cel_validation_routes.py` + `tests/test_incident_cel_enum_casing.py` = 78 passed.
- **UI worktree:** `/Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui`, branch `feat/alert-count-group-by`, cut from `origin/dev` (`be5993d1`). `node_modules` was cloned with `cp -cR /Users/yarin/keep-namespace/keep-ui/node_modules <worktree>/node_modules` (about 1 minute; a symlink breaks Turbopack and Jest). Baselines: `npm run typecheck` reports exactly 2 pre-existing errors (`src/app/(health)/layout.tsx` TS2304 `mulish`; `src/widgets/workflow-builder/__tests__/workflow-builder.test.tsx` TS2739); `npx eslint "src/app/(keep)/dashboard/widget-types/preset"` reports 0 errors and 4 pre-existing `react-hooks/exhaustive-deps` warnings; the two dashboard suites `widget-type-fields.test.ts` and `GridItem.test.tsx` pass (11 tests).
- **If a worktree has to be recreated:** `git -C <repo> worktree add -b feat/alert-count-group-by /Users/yarin/keep-namespace/.worktrees/count-group-by/<repo> origin/dev` (base on `origin/dev`, never the stale local `dev`).
- **Jest quirks (UI):** `jest.setup.ts` already mocks `next/navigation` (`useSearchParams()` is always empty, `usePathname()` is `/alerts/feed`), `useApi`, `useConfig`, and several icon packages. `user-event` is not installed: use `fireEvent` plus `findBy*`/`waitFor` (raw timeouts cause `act()` warnings). A Tremor `Select` is driven by clicking the button inside the element carrying its `data-cy`, then picking from the portaled `listbox`; it also renders a hidden native `<select>`, so always scope option queries to the listbox.

## Global Constraints

Copied from the spec; every task's requirements include them.

- API: `POST /alerts/query/count` body fields `group_by` ∈ {`incident`, `name`, `service`, `node_name`, `application`, `site`, `assignee`} and `incident_status` ∈ {`active`, `firing`, `acknowledged`}; `incident_status` defaults to `active` and is valid only with `group_by=incident`; any other value or combination is a 422; the response stays a bare JSON integer; without `group_by` behavior is unchanged.
- `active` means `IncidentStatus.get_active()` (firing + acknowledged). Incident status is evaluated with the Incidents page's own expression (an `IncidentEnrichment` status overrides `incident.status`). Soft-deleted `LastAlertToIncident` links (`deleted_at != NULL_FOR_DELETED_AT`) never count. Empty (NULL) field values are not counted.
- Excluded from `group_by` on purpose: `severity`, `status`, `environment`, `source`, and any `labels.*` / JSON field.
- Widget config is stored as `countBy: { field, incidentStatus? }` inside the widget JSON in `dashboard_config`; a widget without `countBy` counts alerts exactly as today. **No migrations, no new columns or tables.**
- UI copy, verbatim: segmented labels `Alerts` | `Incidents` | `Other field`; status options `Active (firing + acknowledged)` | `Firing only` | `Acknowledged only`; field options `Alert name`, `Service`, `Host`, `Application`, `Site`, `Assignee` (API values `name`, `service`, `node_name`, `application`, `site`, `assignee`); helper copy `Distinct incidents with at least one alert matching the preset.` and `Counts distinct values of that field. Empty values aren't counted.`; captions `Active incidents` / `Firing incidents` / `Acknowledged incidents` / `Alert names` / `Services` / `Hosts` / `Applications` / `Sites` / `Assignees`, singular when the count is 1; error value `—` with the text `Couldn't load count`.
- A failed grouped request shows `—` on a neutral grey tile (`#9ca3af`) and never `0`. Ungrouped counters (including the sidebar preset counters) keep their current behavior. Ungrouped widgets add no requests; a grouped Alert Table widget adds exactly one.
- Thresholds, tile colour and the table tint follow the grouped number. The fire icon stays tied to "Show Firing Alerts Only". Existing alert-counting tiles get no caption.
- Rollout order: the gateway PR ships before the keep-ui PR (an old gateway silently ignores `group_by` and returns the alert count).
- Gateway: Pydantic v1 (not v2), SQLAlchemy 2.0; must work on PostgreSQL, MySQL and SQLite. Run pytest as `PYTHONPATH=. $GW_PY -m pytest …` from the worktree root; without `PYTHONPATH=.` the suite imports `src/` from the main checkout.
- Process (standing instructions from the user): branch from `origin/dev` only; never work on, commit to, or fast-forward `dev` or `main`; **nothing is pushed and no PR is opened** unless explicitly asked; commit messages contain no URLs and end with exactly `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (no session trailer); no inline explanatory comments in added code (docstrings/JSDoc only); keep changes minimal and justified (no speculative caches or options); suites are run by a sonnet subagent, judgment stays in the main session; lint only the changed paths (never run bare `black src/` or `isort src/`); in zsh pass explicit file lists and quote paths containing parentheses.

## Review Focus

Inputs and conditions the spec implies that a person using this would hit, most likely first. Each line names the task whose tests pin it.

1. An alert linked to both a resolved and a firing incident must count only the firing one (the existing alert-query join gets this wrong). Pinned in Task 2 (`test_a_resolved_incident_on_a_shared_fingerprint_is_not_counted`, `test_incident_status_modes`).
2. An incident whose status was changed through an `IncidentEnrichment` override must follow the override, exactly like the Incidents page; an acknowledged incident must count as live. Pinned in Task 2 (`test_incident_status_follows_the_enrichment_override`, `test_incident_status_modes`).
3. An alert unlinked from an incident (soft-deleted link) must stop counting toward that incident. Pinned in Task 2 (`test_a_soft_deleted_link_is_not_counted`).
4. Another tenant's incidents and alerts must never be counted. Pinned in Task 2 (`test_other_tenants_are_not_counted`).
5. A widget that was grouped and is edited back to "Alerts" must save without `countBy` (not stay grouped), and a saved `countBy` that the gateway now rejects (422) must show `—`, not `0`. Pinned in Task 8 (`drops a saved countBy when the user goes back to counting alerts`), Task 6 (`reports a failed grouped request as an error`) and Task 3 (`test_invalid_grouping_is_a_validation_error`).

Also covered where it belongs: "Show Firing Alerts Only" composing with grouping (Task 2 `test_cel_restricts_which_alerts_are_considered`, Task 6 `composes the firing-only filter into the cel`), preset CEL that references `incident.*` while grouped by incident (Task 2), two widgets with different groupings sharing a cache key (Task 6), and a long caption in a narrow tile (truncates with a title; checked visually in Task 12).

## File Structure

| File | Responsibility |
|---|---|
| `keep-api-gateway/src/models/query.py` | `CountGroupBy`, `CountIncidentStatus`, `CountQueryDto` (validation of the request body) |
| `keep-api-gateway/src/repositories/alerts.py` | `_join_incidents_with_status`, `incident_statuses` option on `__build_query_for_filtering`, `build_distinct_count_query`, `query_distinct_alerts_count` |
| `keep-api-gateway/src/routes/alerts.py` | `POST /alerts/query/count` dispatches to the plain or the distinct count |
| `keep-api-gateway/tests/fixtures/alert_graph.py` | `seed_alert`, `seed_incident`, `link` helpers shared by the repository and route tests |
| `keep-api-gateway/tests/test_count_query_dto.py`, `test_alerts_distinct_count.py`, `test_alerts_count_route.py` | DTO, repository and HTTP contract tests |
| `keep-ui/src/entities/presets/model/count-by.ts` | `CountBy` types, option lists, `getCountMode`, `getCountUnitLabel` |
| `keep-ui/src/features/presets/custom-preset-links/model/usePresetAlertCount.ts` | grouped request + `isError` |
| `keep-ui/src/app/(keep)/dashboard/types.tsx`, `widget-type-fields.ts` | `WidgetData.countBy`; type-switch stripping |
| `keep-ui/src/app/(keep)/dashboard/widget-types/preset/count-by-control.tsx` | the segmented Count control and its conditional selects |
| `keep-ui/src/app/(keep)/dashboard/widget-types/preset/preset-widget-form.tsx` | holds `countBy` state and emits it |
| `keep-ui/src/app/(keep)/dashboard/widget-types/preset/widget-alert-count-panel.tsx`, `preset-grid-item.tsx` | counter tile rendering and prop pass-through |
| `keep-ui/src/app/(keep)/dashboard/widget-types/preset/preset-alert-table-panel.tsx` | group-count line on the table widget |

---

## Part A: keep-api-gateway

All gateway work happens in the worktree `/Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway` (branch `feat/alert-count-group-by`, based on `origin/dev`). Run every gateway command from that directory. Use the shared interpreter `GW_PY=/Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python`; no `.venv` symlink is needed. Always prefix pytest with `PYTHONPATH=.` (see Global Constraints).

### Task 1: `CountQueryDto` with an allowlisted `group_by`

**Files:**
- Modify: `src/models/query.py`
- Test: `tests/test_count_query_dto.py`

**Interfaces:**
- Consumes: the existing `QueryDto` (fields `cel`, `limit`, `offset`, `sort_by`, `sort_dir`, `sort_options`).
- Produces (used by Tasks 2 and 3): `CountGroupBy` (`str` Enum: `INCIDENT="incident"`, `NAME="name"`, `SERVICE="service"`, `NODE_NAME="node_name"`, `APPLICATION="application"`, `SITE="site"`, `ASSIGNEE="assignee"`); `CountIncidentStatus` (`str` Enum: `ACTIVE="active"`, `FIRING="firing"`, `ACKNOWLEDGED="acknowledged"`); `CountQueryDto(QueryDto)` with `group_by: Optional[CountGroupBy] = None` and `incident_status: Optional[CountIncidentStatus] = None`. After validation, `incident_status` is `CountIncidentStatus.ACTIVE` when `group_by == CountGroupBy.INCIDENT` and none was sent; it is `None` for every other `group_by`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_count_query_dto.py`:

```python
import pytest
from pydantic import ValidationError

from src.models.query import (
    CountGroupBy,
    CountIncidentStatus,
    CountQueryDto,
    QueryDto,
)


def test_ungrouped_query_has_no_group_by_or_status():
    query = CountQueryDto(cel="severity == 'critical'")

    assert query.group_by is None
    assert query.incident_status is None


def test_incident_group_defaults_to_active_status():
    query = CountQueryDto(group_by="incident")

    assert query.group_by == CountGroupBy.INCIDENT
    assert query.incident_status == CountIncidentStatus.ACTIVE


@pytest.mark.parametrize("status", ["active", "firing", "acknowledged"])
def test_incident_group_accepts_each_status(status):
    query = CountQueryDto(group_by="incident", incident_status=status)

    assert query.incident_status == CountIncidentStatus(status)


@pytest.mark.parametrize(
    "group_by",
    ["name", "service", "node_name", "application", "site", "assignee"],
)
def test_allowlisted_fields_are_accepted(group_by):
    query = CountQueryDto(group_by=group_by)

    assert query.group_by == CountGroupBy(group_by)
    assert query.incident_status is None


@pytest.mark.parametrize(
    "group_by",
    ["severity", "status", "environment", "source", "labels.team", "", "INCIDENT"],
)
def test_fields_outside_the_allowlist_are_rejected(group_by):
    with pytest.raises(ValidationError):
        CountQueryDto(group_by=group_by)


def test_incident_status_without_a_group_is_rejected():
    with pytest.raises(ValidationError):
        CountQueryDto(incident_status="firing")


def test_incident_status_with_a_non_incident_group_is_rejected():
    with pytest.raises(ValidationError):
        CountQueryDto(group_by="service", incident_status="active")


def test_unknown_incident_status_is_rejected():
    with pytest.raises(ValidationError):
        CountQueryDto(group_by="incident", incident_status="resolved")


def test_plain_query_dto_does_not_gain_the_new_fields():
    assert issubclass(CountQueryDto, QueryDto)
    assert "group_by" not in QueryDto.__fields__
    assert "incident_status" not in QueryDto.__fields__
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_count_query_dto.py -q`
Expected: collection error `ImportError: cannot import name 'CountGroupBy' from 'src.models.query'`.

- [ ] **Step 3: Write the implementation**

Replace the contents of `src/models/query.py` with (the first two classes are unchanged, including their existing comments):

```python
from enum import Enum
from typing import Optional

from pydantic import BaseModel, root_validator

class SortOptionsDto(BaseModel):
    sort_by: Optional[str]
    sort_dir: Optional[str]

class QueryDto(BaseModel):
    cel: Optional[str] = ""
    limit: Optional[int] = 1000
    offset: Optional[int] = 0
    sort_by: Optional[str]  # must be deprecated because we have sort_options
    sort_dir: Optional[str]  # must be deprecated because we have sort_options
    sort_options: Optional[list[SortOptionsDto]]


class CountGroupBy(str, Enum):
    """Fields a count may take distinct values of."""

    INCIDENT = "incident"
    NAME = "name"
    SERVICE = "service"
    NODE_NAME = "node_name"
    APPLICATION = "application"
    SITE = "site"
    ASSIGNEE = "assignee"


class CountIncidentStatus(str, Enum):
    """Which incidents count as live when grouping by incident."""

    ACTIVE = "active"
    FIRING = "firing"
    ACKNOWLEDGED = "acknowledged"


class CountQueryDto(QueryDto):
    """Body of ``POST /alerts/query/count``.

    Without ``group_by`` it counts alerts. With ``group_by`` it counts distinct
    values of that field; ``incident_status`` applies only to ``group_by=incident``
    and defaults to ``active`` there.
    """

    group_by: Optional[CountGroupBy] = None
    incident_status: Optional[CountIncidentStatus] = None

    @root_validator(skip_on_failure=True)
    def validate_incident_status(cls, values):
        group_by = values.get("group_by")
        incident_status = values.get("incident_status")

        if incident_status is not None and group_by != CountGroupBy.INCIDENT:
            raise ValueError("incident_status is only valid with group_by=incident")

        if group_by == CountGroupBy.INCIDENT and incident_status is None:
            values["incident_status"] = CountIncidentStatus.ACTIVE

        return values
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_count_query_dto.py -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
git add src/models/query.py tests/test_count_query_dto.py
git commit -m "feat: add CountQueryDto with an allowlisted group_by" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 2: Distinct-count query and its dedicated incident join

**Files:**
- Create: `tests/fixtures/alert_graph.py`
- Create: `tests/test_alerts_distinct_count.py`
- Modify: `src/repositories/alerts.py` (imports; `__build_query_for_filtering`; new functions after `query_total_alerts_count`)

**Interfaces:**
- Consumes (Task 1): `CountQueryDto`, `CountGroupBy`, `CountIncidentStatus`.
- Produces (used by Task 3): `build_distinct_count_query(tenant_id: str, query: CountQueryDto)` returning a SQLAlchemy `select` that yields one row with one integer column, and `query_distinct_alerts_count(tenant_id: str, query: CountQueryDto) -> int`. `__build_query_for_filtering` gains a trailing keyword parameter `incident_statuses: list[str] | None = None`; when it is not `None` the incident join is replaced by the dedicated one below, when `None` (every existing caller) behavior is unchanged.

Why a dedicated join (verified by a probe on the SQLite test DB against `origin/dev`): the existing incident join in `__build_query_for_filtering` attaches a *resolved* incident whenever the alert's fingerprint also belongs to some firing incident, attaches incidents through *soft-deleted* links, and decides "firing" from the raw `incident.status` column, so it ignores `IncidentEnrichment` status overrides and misses acknowledged incidents. Counting distinct incidents through it returned the wrong membership. The dedicated join fixes all four.

- [ ] **Step 1: Create the shared seed helpers**

Create `tests/fixtures/alert_graph.py`:

```python
from datetime import datetime, timedelta

from src.models.db.alert import (
    Alert,
    IncidentEnrichment,
    LastAlert,
    LastAlertToIncident,
)
from src.models.db.incident import Incident, IncidentStatus
from src.repositories.dependencies import SINGLE_TENANT_UUID


def seed_alert(
    session,
    fingerprint,
    *,
    name="alert",
    service=None,
    node_name=None,
    application=None,
    site=None,
    assignee=None,
    status="firing",
    severity="critical",
    tenant_id=SINGLE_TENANT_UUID,
    minutes_ago=1,
):
    timestamp = datetime.utcnow() - timedelta(minutes=minutes_ago)
    alert = Alert(
        tenant_id=tenant_id,
        provider_type="test",
        provider_id="test-provider",
        fingerprint=fingerprint,
        name=name,
        service=service,
        node_name=node_name,
        application=application,
        site=site,
        status=status,
        severity=severity,
        timestamp=timestamp,
    )
    session.add(alert)
    session.commit()
    session.add(
        LastAlert(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            alert_id=alert.id,
            timestamp=timestamp,
            first_timestamp=timestamp,
            assignee=assignee,
        )
    )
    session.commit()
    return alert


def seed_incident(
    session,
    name,
    status=IncidentStatus.FIRING,
    tenant_id=SINGLE_TENANT_UUID,
    enrichment_status=None,
):
    incident = Incident(
        tenant_id=tenant_id,
        user_generated_name=name,
        user_summary=name,
        generated_summary=name,
        status=status.value,
    )
    session.add(incident)
    session.commit()
    if enrichment_status is not None:
        session.add(
            IncidentEnrichment(
                tenant_id=tenant_id,
                incident_id=incident.id,
                enrichments={"status": enrichment_status.value},
            )
        )
        session.commit()
    return incident


def link(session, fingerprint, incident, *, tenant_id=SINGLE_TENANT_UUID, deleted_at=None):
    kwargs = {} if deleted_at is None else {"deleted_at": deleted_at}
    session.add(
        LastAlertToIncident(
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            incident_id=incident.id,
            **kwargs,
        )
    )
    session.commit()
```

- [ ] **Step 2: Write the failing repository tests**

Create `tests/test_alerts_distinct_count.py`:

```python
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from src.models.db.incident import IncidentStatus
from src.models.query import CountQueryDto
from src.repositories.alerts import query_distinct_alerts_count
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.alert_graph import link, seed_alert, seed_incident

OTHER_TENANT = "other-tenant"


def count(tenant_id=SINGLE_TENANT_UUID, **query):
    return query_distinct_alerts_count(
        tenant_id=tenant_id, query=CountQueryDto(**query)
    )


@pytest.fixture
def graph(db_session):
    """Seven live incidents, one resolved, one unlinked, one overridden each way.

    active:       busy, two-firing-a, two-firing-b, current-firing,
                  override-firing, acknowledged, quiet            -> 7
    firing only:  the same without acknowledged                    -> 6
    acknowledged: acknowledged                                     -> 1
    """
    busy = seed_incident(db_session, "busy")
    for index in range(40):
        fingerprint = f"fp-busy-{index}"
        seed_alert(
            db_session,
            fingerprint,
            name="n1",
            service="s1",
            assignee="u1" if index == 0 else None,
        )
        link(db_session, fingerprint, busy)

    two_firing_a = seed_incident(db_session, "two-firing-a")
    two_firing_b = seed_incident(db_session, "two-firing-b")
    seed_alert(
        db_session,
        "fp-two",
        name="n2",
        service="s2",
        assignee="u2",
        node_name="h1",
        application="app1",
        site="eu",
    )
    link(db_session, "fp-two", two_firing_a)
    link(db_session, "fp-two", two_firing_b)

    old_resolved = seed_incident(db_session, "old-resolved", IncidentStatus.RESOLVED)
    current_firing = seed_incident(db_session, "current-firing")
    seed_alert(db_session, "fp-mixed", name="n2", node_name="h2")
    link(db_session, "fp-mixed", old_resolved)
    link(db_session, "fp-mixed", current_firing)

    unlinked = seed_incident(db_session, "unlinked")
    seed_alert(db_session, "fp-unlinked", name="n2")
    link(db_session, "fp-unlinked", unlinked, deleted_at=datetime(2025, 1, 1))

    override_resolved = seed_incident(
        db_session,
        "override-resolved",
        IncidentStatus.FIRING,
        enrichment_status=IncidentStatus.RESOLVED,
    )
    seed_alert(db_session, "fp-override-resolved", name="n2")
    link(db_session, "fp-override-resolved", override_resolved)

    override_firing = seed_incident(
        db_session,
        "override-firing",
        IncidentStatus.RESOLVED,
        enrichment_status=IncidentStatus.FIRING,
    )
    seed_alert(db_session, "fp-override-firing", name="n2")
    link(db_session, "fp-override-firing", override_firing)

    acknowledged = seed_incident(db_session, "acknowledged", IncidentStatus.ACKNOWLEDGED)
    seed_alert(db_session, "fp-ack", name="n2")
    link(db_session, "fp-ack", acknowledged)

    quiet = seed_incident(db_session, "quiet")
    seed_alert(db_session, "fp-quiet", name="n2", status="resolved")
    link(db_session, "fp-quiet", quiet)

    seed_alert(db_session, "fp-no-incident", name="n2")


def test_forty_alerts_in_one_incident_count_once(db_session):
    incident = seed_incident(db_session, "busy")
    for index in range(40):
        seed_alert(db_session, f"fp-{index}")
        link(db_session, f"fp-{index}", incident)

    assert count(group_by="incident") == 1


def test_an_alert_in_two_incidents_counts_both(db_session):
    first = seed_incident(db_session, "first")
    second = seed_incident(db_session, "second")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", first)
    link(db_session, "fp-1", second)

    assert count(group_by="incident") == 2


@pytest.mark.parametrize(
    "incident_status, expected",
    [(None, 7), ("active", 7), ("firing", 6), ("acknowledged", 1)],
)
def test_incident_status_modes(graph, incident_status, expected):
    query = {"group_by": "incident"}
    if incident_status:
        query["incident_status"] = incident_status

    assert count(**query) == expected


def test_a_resolved_incident_on_a_shared_fingerprint_is_not_counted(db_session):
    resolved = seed_incident(db_session, "resolved", IncidentStatus.RESOLVED)
    firing = seed_incident(db_session, "firing")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", resolved)
    link(db_session, "fp-1", firing)

    assert count(group_by="incident", incident_status="firing") == 1
    assert count(group_by="incident", incident_status="active") == 1


def test_a_soft_deleted_link_is_not_counted(db_session):
    incident = seed_incident(db_session, "unlinked")
    seed_alert(db_session, "fp-1")
    link(db_session, "fp-1", incident, deleted_at=datetime(2025, 1, 1))

    assert count(group_by="incident") == 0


def test_incident_status_follows_the_enrichment_override(db_session):
    resolved_by_override = seed_incident(
        db_session,
        "resolved-by-override",
        IncidentStatus.FIRING,
        enrichment_status=IncidentStatus.RESOLVED,
    )
    firing_by_override = seed_incident(
        db_session,
        "firing-by-override",
        IncidentStatus.RESOLVED,
        enrichment_status=IncidentStatus.FIRING,
    )
    seed_alert(db_session, "fp-1")
    seed_alert(db_session, "fp-2")
    link(db_session, "fp-1", resolved_by_override)
    link(db_session, "fp-2", firing_by_override)

    assert count(group_by="incident", incident_status="firing") == 1


def test_alerts_without_an_incident_contribute_nothing(db_session):
    seed_alert(db_session, "fp-1")

    assert count(group_by="incident") == 0


def test_cel_restricts_which_alerts_are_considered(graph):
    assert count(group_by="incident", cel="service == 's1'") == 1
    assert count(group_by="incident", cel="status == 'firing'") == 6
    assert count(group_by="incident", cel="status == 'resolved'") == 1


def test_cel_may_reference_incident_fields_while_grouped_by_incident(graph):
    assert count(group_by="incident", cel="incident.name == 'busy'") == 1
    assert count(group_by="incident", cel="incident.name == 'old-resolved'") == 0


@pytest.mark.parametrize(
    "group_by, expected",
    [
        ("name", 2),
        ("service", 2),
        ("assignee", 2),
        ("node_name", 2),
        ("application", 1),
        ("site", 1),
    ],
)
def test_distinct_values_ignore_nulls(graph, group_by, expected):
    assert count(group_by=group_by) == expected


def test_nothing_to_count_is_zero(db_session):
    assert count(group_by="incident") == 0
    assert count(group_by="service") == 0


def test_other_tenants_are_not_counted(graph, db_session):
    foreign = seed_incident(db_session, "foreign", tenant_id=OTHER_TENANT)
    seed_alert(
        db_session, "fp-foreign", service="s9", tenant_id=OTHER_TENANT
    )
    link(db_session, "fp-foreign", foreign, tenant_id=OTHER_TENANT)

    assert count(group_by="incident") == 7
    assert count(group_by="service") == 2
    assert count(tenant_id=OTHER_TENANT, group_by="incident") == 1
    assert count(tenant_id=OTHER_TENANT, group_by="service") == 1


def test_a_database_failure_is_raised_not_hidden_as_zero(db_session):
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with patch(
        "src.repositories.alerts.build_distinct_count_query", side_effect=failure
    ):
        with pytest.raises(OperationalError):
            count(group_by="incident")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_alerts_distinct_count.py -q`
Expected: collection error `ImportError: cannot import name 'query_distinct_alerts_count' from 'src.repositories.alerts'`.

- [ ] **Step 4: Update the imports in `src/repositories/alerts.py`**

Replace the line `from sqlalchemy import and_, func, select` with:

```python
from sqlalchemy import and_, func, select
from sqlalchemy.orm import aliased
```

Replace the import block

```python
from src.models.db.alert import (
    Alert,
    AlertField,
    Incident,
    LastAlert,
    LastAlertToIncident,
)
from src.models.db.facet import FacetType
```

with:

```python
from src.models.db.alert import (
    Alert,
    AlertField,
    Incident,
    IncidentEnrichment,
    LastAlert,
    LastAlertToIncident,
)
from src.models.db.facet import FacetType
from src.models.db.helpers import NULL_FOR_DELETED_AT
```

Replace `from src.models.query import QueryDto, SortOptionsDto` with:

```python
from src.models.query import (
    CountGroupBy,
    CountIncidentStatus,
    CountQueryDto,
    QueryDto,
    SortOptionsDto,
)
```

- [ ] **Step 5: Add the status map and the dedicated join**

Directly below the line `ALERTS_HARD_LIMIT = int(os.environ.get("KEEP_LAST_ALERTS_LIMIT", 50000))` add:

```python
_COUNT_INCIDENT_STATUSES = {
    CountIncidentStatus.ACTIVE: IncidentStatus.get_active(return_values=True),
    CountIncidentStatus.FIRING: [IncidentStatus.FIRING.value],
    CountIncidentStatus.ACKNOWLEDGED: [IncidentStatus.ACKNOWLEDGED.value],
}
```

Directly above `def __build_query_for_filtering(` add:

```python
def _join_incidents_with_status(sql_query, statuses: list[str]):
    """Inner-join live incident links and keep incidents whose status is in ``statuses``.

    The status expression comes from the incidents repository so that an
    ``IncidentEnrichment`` override wins over ``incident.status`` exactly as it does
    on the Incidents page. The import is local because ``incidents`` already
    imports this module.
    """
    from src.repositories.incidents import (
        properties_metadata as incident_properties_metadata,
    )

    status_expression = get_cel_to_sql_provider(
        incident_properties_metadata
    ).get_field_expression("status")
    quoted_statuses = ", ".join(f"'{status}'" for status in statuses)
    incident_enrichment = aliased(IncidentEnrichment, name="incidentenrichment")

    return (
        sql_query.join(
            LastAlertToIncident,
            and_(
                LastAlert.tenant_id == LastAlertToIncident.tenant_id,
                LastAlert.fingerprint == LastAlertToIncident.fingerprint,
                LastAlertToIncident.deleted_at == NULL_FOR_DELETED_AT,
            ),
        )
        .join(
            Incident,
            and_(
                LastAlertToIncident.tenant_id == Incident.tenant_id,
                LastAlertToIncident.incident_id == Incident.id,
            ),
        )
        .outerjoin(
            incident_enrichment,
            and_(
                Incident.tenant_id == incident_enrichment.tenant_id,
                Incident.id == incident_enrichment.incident_id,
            ),
        )
        .where(text(f"({status_expression}) IN ({quoted_statuses})"))
    )


```

- [ ] **Step 6: Thread `incident_statuses` through `__build_query_for_filtering`**

In the signature of `__build_query_for_filtering`, after `force_fetch=False,` add `incident_statuses: list[str] | None = None,` so it reads:

```python
def __build_query_for_filtering(
    tenant_id: str,
    select_args: list,
    cel=None,
    limit=None,
    fetch_alerts_data=True,
    fetch_incidents=False,
    force_fetch=False,
    incident_statuses: list[str] | None = None,
):
```

Then change the existing incident-join branch. Replace

```python
    if fetch_incidents or force_fetch:
        # Fingerprint with active incidents subquery, i.e  in Firing status
```

with

```python
    if incident_statuses is not None:
        sql_query = _join_incidents_with_status(sql_query, incident_statuses)
    elif fetch_incidents or force_fetch:
        # Fingerprint with active incidents subquery, i.e  in Firing status
```

(the rest of that existing block stays exactly as it is).

- [ ] **Step 7: Add the query builder and the repository function**

Directly below `query_total_alerts_count` (it ends with `raise` followed by two blank lines before `def query_last_alerts`) add:

```python
def build_distinct_count_query(tenant_id: str, query: CountQueryDto):
    if query.group_by == CountGroupBy.INCIDENT:
        count_expression = func.count(func.distinct(Incident.id))
        incident_statuses = _COUNT_INCIDENT_STATUSES[query.incident_status]
    else:
        field_expression = get_cel_to_sql_provider(
            properties_metadata
        ).get_field_expression(query.group_by.value)
        count_expression = func.count(func.distinct(text(field_expression)))
        incident_statuses = None

    built_query_result = __build_query_for_filtering(
        tenant_id=tenant_id,
        cel=query.cel,
        select_args=[count_expression],
        limit=query.limit,
        incident_statuses=incident_statuses,
    )
    return built_query_result["query"]


def query_distinct_alerts_count(tenant_id, query: CountQueryDto) -> int:
    with Session(engine) as session:
        try:
            distinct_count_query = build_distinct_count_query(
                tenant_id=tenant_id, query=query
            )
            return session.exec(distinct_count_query).one()[0]
        except OperationalError as e:
            logger.exception(
                f"Failed to query distinct alerts count for query object '{json.dumps(query.dict(exclude_unset=True), default=str)}': {e}"
            )
            raise
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_alerts_distinct_count.py tests/test_count_query_dto.py -q`
Expected: all pass. If `test_cel_restricts_which_alerts_are_considered` fails on the `status == 'firing'` expectation, print `count(group_by="incident", cel="status == 'firing'")` and compare with the fixture docstring before changing any code: `status` maps to `lastalert.status` then `alert.status`, and only `fp-quiet` is seeded with a non-firing alert status.

- [ ] **Step 9: Regression run on the neighbouring suites**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_cel_validation_routes.py tests/test_incident_cel_enum_casing.py tests/test_search_alerts.py -q`
Expected: pass (the first two were 78 passing on the untouched branch). Any failure here is a regression from Step 6; fix it before continuing.

- [ ] **Step 10: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
git add src/repositories/alerts.py tests/fixtures/alert_graph.py tests/test_alerts_distinct_count.py
git commit -m "feat: count distinct incidents and alert fields in the gateway" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 3: Wire `group_by` into `POST /alerts/query/count`

**Files:**
- Modify: `src/routes/alerts.py`
- Test: `tests/test_alerts_count_route.py`

**Interfaces:**
- Consumes (Tasks 1 and 2): `CountQueryDto`, `query_distinct_alerts_count`.
- Produces: the HTTP contract the UI relies on. `POST /alerts/query/count` with body `{"cel": str, "group_by"?: "incident"|"name"|"service"|"node_name"|"application"|"site"|"assignee", "incident_status"?: "active"|"firing"|"acknowledged"}` returns a bare JSON integer; invalid `group_by` or `incident_status` combinations return 422; invalid CEL returns 400 with `detail.code == "INVALID_CEL"`; a database failure returns 5xx.

- [ ] **Step 1: Write the failing route tests**

Create `tests/test_alerts_count_route.py`:

```python
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from src.models.db.incident import IncidentStatus
from tests.fixtures.alert_graph import link, seed_alert, seed_incident
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

AUTH = {"x-api-key": "some-key"}
COUNT_URL = "/alerts/query/count"


@pytest.fixture
def two_incidents(db_session):
    first = seed_incident(db_session, "first")
    second = seed_incident(db_session, "second", IncidentStatus.RESOLVED)
    for index in range(3):
        fingerprint = f"fp-first-{index}"
        seed_alert(db_session, fingerprint, service="checkout")
        link(db_session, fingerprint, first)
    seed_alert(db_session, "fp-second", service="search")
    link(db_session, "fp-second", second)
    seed_alert(db_session, "fp-loose", service="search")


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_ungrouped_count_still_counts_alerts(
    db_session, client, test_app, two_incidents
):
    response = client.post(COUNT_URL, headers=AUTH, json={"cel": ""})

    assert response.status_code == 200
    assert response.json() == 5


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "body, expected",
    [
        ({"group_by": "incident"}, 1),
        ({"group_by": "incident", "incident_status": "firing"}, 1),
        ({"group_by": "incident", "incident_status": "acknowledged"}, 0),
        ({"group_by": "service"}, 2),
        ({"group_by": "service", "cel": "service == 'search'"}, 1),
    ],
)
def test_grouped_count_returns_a_bare_integer(
    db_session, client, test_app, two_incidents, body, expected
):
    response = client.post(COUNT_URL, headers=AUTH, json={"cel": "", **body})

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "body",
    [
        {"group_by": "severity"},
        {"group_by": "labels.team"},
        {"incident_status": "active"},
        {"group_by": "service", "incident_status": "active"},
        {"group_by": "incident", "incident_status": "resolved"},
    ],
)
def test_invalid_grouping_is_a_validation_error(db_session, client, test_app, body):
    response = client.post(COUNT_URL, headers=AUTH, json={"cel": "", **body})

    assert response.status_code == 422
    assert "INVALID_CEL" not in response.text


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_invalid_cel_with_grouping_is_still_a_cel_error(db_session, client, test_app):
    response = client.post(
        COUNT_URL,
        headers=AUTH,
        json={"cel": "no_such_field == 'x'", "group_by": "incident"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_CEL"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_grouped_count_surfaces_a_database_failure(db_session, client, test_app):
    failing_client = TestClient(client.app, raise_server_exceptions=False)
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with patch(
        "src.repositories.alerts.build_distinct_count_query", side_effect=failure
    ):
        response = failing_client.post(
            COUNT_URL, headers=AUTH, json={"cel": "", "group_by": "incident"}
        )

    assert response.status_code == 500
    assert "INVALID_CEL" not in response.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_alerts_count_route.py -q`
Expected: the grouped-count and validation tests fail (the route still takes `QueryDto`, ignores `group_by`, returns the alert count `5`); `test_ungrouped_count_still_counts_alerts` already passes.

- [ ] **Step 3: Update the route**

In `src/routes/alerts.py` replace

```python
    query_last_alerts,
    query_total_alerts_count
)
```

with

```python
    query_distinct_alerts_count,
    query_last_alerts,
    query_total_alerts_count
)
```

Replace `from src.models.query import QueryDto` with `from src.models.query import CountQueryDto, QueryDto`.

Replace the head of `query_alerts_count`:

```python
def query_alerts_count(
    query: QueryDto,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:alert"])
    ),
):
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        msg="Fetching alerts count from DB",
        extra={"tenant_id": tenant_id, "cel_expression": query.cel},
    )

    try:
        total_count = query_total_alerts_count(tenant_id=tenant_id, query=query)
```

with

```python
def query_alerts_count(
    query: CountQueryDto,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:alert"])
    ),
):
    tenant_id = authenticated_entity.tenant_id
    logger.info(
        msg="Fetching alerts count from DB",
        extra={
            "tenant_id": tenant_id,
            "cel_expression": query.cel,
            "group_by": query.group_by.value if query.group_by else None,
            "incident_status": (
                query.incident_status.value if query.incident_status else None
            ),
        },
    )

    try:
        if query.group_by is None:
            total_count = query_total_alerts_count(tenant_id=tenant_id, query=query)
        else:
            total_count = query_distinct_alerts_count(tenant_id=tenant_id, query=query)
```

(the remaining lines of the function, including the `except CelToSqlException` branch, are unchanged).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/test_alerts_count_route.py tests/test_alerts_distinct_count.py tests/test_count_query_dto.py tests/test_cel_validation_routes.py -q`
Expected: all pass, including the existing `TestDatabaseFailuresStayFailures` and `test_count_rejects_the_same_expressions`.

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
git add src/routes/alerts.py tests/test_alerts_count_route.py
git commit -m "feat: accept group_by and incident_status on the alerts count endpoint" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 4: Gateway lint and regression gate

**Files:** none modified unless lint finds a problem in lines this branch added.

- [ ] **Step 1: Lint only what changed, against the `origin/dev` baseline**

The repo-wide black/isort gate is already broken (running it on `src/` reformats ~170 files), so never run them on `src/`. Check ruff on the three changed source files and compare with the baseline from `origin/dev`:

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
GW_PY=/Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python
for f in src/models/query.py src/repositories/alerts.py src/routes/alerts.py; do
  echo "== $f baseline"; git show origin/dev:$f | $GW_PY -m ruff check --stdin-filename $f - | tail -3
  echo "== $f now";      $GW_PY -m ruff check $f | tail -3
done
$GW_PY -m black --check tests/fixtures/alert_graph.py tests/test_alerts_distinct_count.py tests/test_alerts_count_route.py tests/test_count_query_dto.py
$GW_PY -m isort --check tests/fixtures/alert_graph.py tests/test_alerts_distinct_count.py tests/test_alerts_count_route.py tests/test_count_query_dto.py
```

Expected: each "now" shows no more findings than its baseline; black and isort pass on the four new files. If black wants to reformat a new file, run `black`/`isort` on those four files only and re-run their tests. Do not reformat `src/` files.

- [ ] **Step 2: Wider regression run**

Run (delegated to a sonnet subagent, per the team rule that suites are not run from the main session): `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && PYTHONPATH=. /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m pytest tests/ --timeout 120 -q -x --ignore=tests/provision -p no:cacheprovider`
Expected: no new failures compared with `origin/dev`. If something fails, re-run that single test on a clean `origin/dev` worktree before treating it as caused by this branch (some suites need docker services and fail on `origin/dev` too).

- [ ] **Step 3: Commit any lint fixes**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
git status --short
git add tests/fixtures/alert_graph.py tests/test_alerts_distinct_count.py tests/test_alerts_count_route.py tests/test_count_query_dto.py
git commit -m "style: format count-by tests" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
Skip this commit if `git status --short` shows nothing to add.

## Part B: keep-ui

All UI work happens in the worktree `/Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui` (branch `feat/alert-count-group-by`, based on `origin/dev`). Run every UI command from that directory. UI tasks do not depend on the gateway tasks to compile or pass their tests (the API is mocked), so Part A and Part B can be implemented in either order; they only meet in Task 12.

Layering (Feature-Sliced Design): `features/` may import from `entities/`, never from `app/`. That is why the shared types and helpers live in `src/entities/presets/model/count-by.ts` and are imported by both the hook (`features/`) and the dashboard widgets (`app/`).

### Task 5: Shared `count-by` module, widget type and type-switch stripping

**Files:**
- Create: `src/entities/presets/model/count-by.ts`
- Create: `src/entities/presets/model/__tests__/count-by.test.ts`
- Modify: `src/entities/presets/model/index.ts`
- Modify: `src/app/(keep)/dashboard/types.tsx`
- Modify: `src/app/(keep)/dashboard/widget-type-fields.ts`
- Modify: `src/app/(keep)/dashboard/__tests__/widget-type-fields.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces (used by Tasks 6-10), all exported from `@/entities/presets/model/count-by` and re-exported from `@/entities/presets/model`:
  `type CountByField = "incident" | "name" | "service" | "node_name" | "application" | "site" | "assignee"`;
  `type CountByAlertField = Exclude<CountByField, "incident">`;
  `type IncidentStatusFilter = "active" | "firing" | "acknowledged"`;
  `interface CountBy { field: CountByField; incidentStatus?: IncidentStatusFilter }`;
  `type CountMode = "alerts" | "incidents" | "field"`;
  `const DEFAULT_INCIDENT_STATUS: IncidentStatusFilter` (`"active"`);
  `const COUNT_LOAD_ERROR_LABEL: string` (`"Couldn't load count"`);
  `const INCIDENT_STATUS_OPTIONS: ReadonlyArray<{ value: IncidentStatusFilter; label: string }>`;
  `const COUNT_BY_ALERT_FIELD_OPTIONS: ReadonlyArray<{ value: CountByAlertField; label: string }>`;
  `function getCountMode(countBy?: CountBy): CountMode`;
  `function getCountUnitLabel(countBy: CountBy, count: number): string`.
  `WidgetData` gains `countBy?: CountBy`.

- [ ] **Step 1: Write the failing tests**

Create `src/entities/presets/model/__tests__/count-by.test.ts`:

```ts
import {
  COUNT_BY_ALERT_FIELD_OPTIONS,
  CountBy,
  DEFAULT_INCIDENT_STATUS,
  INCIDENT_STATUS_OPTIONS,
  getCountMode,
  getCountUnitLabel,
} from "../count-by";

describe("getCountMode", () => {
  it("treats a missing countBy as counting alerts", () => {
    expect(getCountMode(undefined)).toBe("alerts");
  });

  it("maps the incident field to incidents", () => {
    expect(getCountMode({ field: "incident", incidentStatus: "active" })).toBe(
      "incidents"
    );
  });

  it.each(["name", "service", "node_name", "application", "site", "assignee"])(
    "maps %s to the other-field mode",
    (field) => {
      expect(getCountMode({ field } as CountBy)).toBe("field");
    }
  );
});

describe("getCountUnitLabel", () => {
  it.each<[CountBy, number, string]>([
    [{ field: "incident", incidentStatus: "active" }, 3, "Active incidents"],
    [{ field: "incident", incidentStatus: "active" }, 1, "Active incident"],
    [{ field: "incident", incidentStatus: "firing" }, 12, "Firing incidents"],
    [{ field: "incident", incidentStatus: "firing" }, 1, "Firing incident"],
    [
      { field: "incident", incidentStatus: "acknowledged" },
      2,
      "Acknowledged incidents",
    ],
    [{ field: "incident" }, 0, "Active incidents"],
    [{ field: "name" }, 2, "Alert names"],
    [{ field: "name" }, 1, "Alert name"],
    [{ field: "service" }, 5, "Services"],
    [{ field: "service" }, 1, "Service"],
    [{ field: "node_name" }, 4, "Hosts"],
    [{ field: "application" }, 4, "Applications"],
    [{ field: "site" }, 0, "Sites"],
    [{ field: "assignee" }, 2, "Assignees"],
    [{ field: "assignee" }, 1, "Assignee"],
  ])("labels %j with count %d as %s", (countBy, count, expected) => {
    expect(getCountUnitLabel(countBy, count)).toBe(expected);
  });
});

describe("option lists", () => {
  it("offers the other fields in the order shown in the form", () => {
    expect(COUNT_BY_ALERT_FIELD_OPTIONS).toEqual([
      { value: "name", label: "Alert name" },
      { value: "service", label: "Service" },
      { value: "node_name", label: "Host" },
      { value: "application", label: "Application" },
      { value: "site", label: "Site" },
      { value: "assignee", label: "Assignee" },
    ]);
  });

  it("offers the three incident statuses with active first", () => {
    expect(INCIDENT_STATUS_OPTIONS.map((option) => option.value)).toEqual([
      "active",
      "firing",
      "acknowledged",
    ]);
    expect(DEFAULT_INCIDENT_STATUS).toBe("active");
  });
});
```

Append to `src/app/(keep)/dashboard/__tests__/widget-type-fields.test.ts`, inside the existing `describe("stripForeignTypeFields", ...)` block, before its closing `});`:

```ts
  it("drops countBy when a preset widget becomes another type but keeps it for presets", () => {
    const grouped = {
      ...presetWidget,
      countBy: { field: "incident", incidentStatus: "active" },
    } as unknown as WidgetData;

    expect(stripForeignTypeFields(grouped, WidgetType.IMAGE)).not.toHaveProperty(
      "countBy"
    );
    expect(stripForeignTypeFields(grouped, WidgetType.PRESET)).toHaveProperty(
      "countBy"
    );
  });
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest src/entities/presets/model/__tests__/count-by.test.ts "src/app/\(keep\)/dashboard/__tests__/widget-type-fields.test.ts"`
Expected: `count-by.test.ts` fails with "Cannot find module '../count-by'"; the new `widget-type-fields` case fails because `countBy` is not owned by the preset type yet.

- [ ] **Step 3: Create the module**

Create `src/entities/presets/model/count-by.ts`:

```ts
export type CountByField =
  | "incident"
  | "name"
  | "service"
  | "node_name"
  | "application"
  | "site"
  | "assignee";

export type CountByAlertField = Exclude<CountByField, "incident">;

export type IncidentStatusFilter = "active" | "firing" | "acknowledged";

export interface CountBy {
  field: CountByField;
  incidentStatus?: IncidentStatusFilter;
}

export type CountMode = "alerts" | "incidents" | "field";

export const DEFAULT_INCIDENT_STATUS: IncidentStatusFilter = "active";

export const COUNT_LOAD_ERROR_LABEL = "Couldn't load count";

export const INCIDENT_STATUS_OPTIONS: ReadonlyArray<{
  value: IncidentStatusFilter;
  label: string;
}> = [
  { value: "active", label: "Active (firing + acknowledged)" },
  { value: "firing", label: "Firing only" },
  { value: "acknowledged", label: "Acknowledged only" },
];

const ALERT_FIELD_LABELS: Record<
  CountByAlertField,
  { option: string; singular: string; plural: string }
> = {
  name: { option: "Alert name", singular: "Alert name", plural: "Alert names" },
  service: { option: "Service", singular: "Service", plural: "Services" },
  node_name: { option: "Host", singular: "Host", plural: "Hosts" },
  application: {
    option: "Application",
    singular: "Application",
    plural: "Applications",
  },
  site: { option: "Site", singular: "Site", plural: "Sites" },
  assignee: { option: "Assignee", singular: "Assignee", plural: "Assignees" },
};

const INCIDENT_LABELS: Record<
  IncidentStatusFilter,
  { singular: string; plural: string }
> = {
  active: { singular: "Active incident", plural: "Active incidents" },
  firing: { singular: "Firing incident", plural: "Firing incidents" },
  acknowledged: {
    singular: "Acknowledged incident",
    plural: "Acknowledged incidents",
  },
};

export const COUNT_BY_ALERT_FIELD_OPTIONS: ReadonlyArray<{
  value: CountByAlertField;
  label: string;
}> = (Object.keys(ALERT_FIELD_LABELS) as CountByAlertField[]).map((value) => ({
  value,
  label: ALERT_FIELD_LABELS[value].option,
}));

export function getCountMode(countBy?: CountBy): CountMode {
  if (!countBy) {
    return "alerts";
  }
  return countBy.field === "incident" ? "incidents" : "field";
}

export function getCountUnitLabel(countBy: CountBy, count: number): string {
  if (countBy.field === "incident") {
    const labels =
      INCIDENT_LABELS[countBy.incidentStatus ?? DEFAULT_INCIDENT_STATUS];
    return count === 1 ? labels.singular : labels.plural;
  }
  const labels = ALERT_FIELD_LABELS[countBy.field];
  return count === 1 ? labels.singular : labels.plural;
}
```

- [ ] **Step 4: Export it, add the widget field, register it for type switching**

In `src/entities/presets/model/index.ts` add a line `export * from "./count-by";` after `export * from "./constants";`.

In `src/app/(keep)/dashboard/types.tsx` add the import below the existing two imports:

```ts
import { CountBy } from "@/entities/presets/model/count-by";
```

and in `interface WidgetData`, after `customLink?: string;`, add:

```ts
  countBy?: CountBy;
```

In `src/app/(keep)/dashboard/widget-type-fields.ts` add `"countBy",` to the `WidgetType.PRESET` list, after `"customLink",`:

```ts
  [WidgetType.PRESET]: [
    "preset",
    "presetColumns",
    "thresholds",
    "presetPanelType",
    "showFiringOnly",
    "customLink",
    "countBy",
  ],
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest src/entities/presets/model/__tests__/count-by.test.ts "src/app/\(keep\)/dashboard/__tests__/widget-type-fields.test.ts"`
Expected: all pass.

- [ ] **Step 6: Typecheck and commit**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npm run typecheck 2>&1 | tail -5` — expected: exactly the 2 pre-existing errors listed under "Environment (already prepared)" and nothing new (do not fix the unrelated ones).

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git add src/entities/presets/model/count-by.ts src/entities/presets/model/__tests__/count-by.test.ts src/entities/presets/model/index.ts "src/app/(keep)/dashboard/types.tsx" "src/app/(keep)/dashboard/widget-type-fields.ts" "src/app/(keep)/dashboard/__tests__/widget-type-fields.test.ts"
git commit -m "feat(dashboard): add the countBy widget setting and its labels" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 6: `usePresetAlertCount` requests the grouped count and reports grouped failures

**Files:**
- Modify: `src/features/presets/custom-preset-links/model/usePresetAlertCount.ts`
- Test: `src/features/presets/custom-preset-links/model/__tests__/usePresetAlertCount.test.tsx`

**Interfaces:**
- Consumes (Task 5): `CountByField`, `IncidentStatusFilter`, `DEFAULT_INCIDENT_STATUS` from `@/entities/presets/model/count-by`.
- Produces (used by Tasks 9 and 10): `usePresetAlertCount({ presetCel: string; counterShowsFiringOnly: boolean; groupBy?: CountByField; incidentStatus?: IncidentStatusFilter; refreshInterval?: number; enabled?: boolean })` returning `{ totalCount: number; isLoading: boolean; isError: boolean }`. The request body is `{ cel, group_by?, incident_status? }` where `incident_status` is only sent when `groupBy === "incident"` (defaulting to `"active"`). `isError` is `true` only for a failed request that was grouped; ungrouped callers (the sidebar counters) keep today's behaviour of a failed request reading as `totalCount: 0` with `isError: false`.

- [ ] **Step 1: Write the failing test**

Create `src/features/presets/custom-preset-links/model/__tests__/usePresetAlertCount.test.tsx`:

```tsx
import React from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { usePresetAlertCount } from "../usePresetAlertCount";

const mockPost = jest.fn();

jest.mock("@/shared/lib/hooks/useApi", () => ({
  useApi: () => ({ isReady: () => true, post: mockPost }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig
    value={{
      provider: () => new Map(),
      dedupingInterval: 0,
      shouldRetryOnError: false,
    }}
  >
    {children}
  </SWRConfig>
);

const CEL = "severity == 'critical'";
const WRAPPED_CEL = "(severity == 'critical')";
const COUNT_URL = "/alerts/query/count";

type HookParams = Parameters<typeof usePresetAlertCount>[0];

const renderCount = (params: Partial<HookParams> = {}) =>
  renderHook(
    () =>
      usePresetAlertCount({
        presetCel: CEL,
        counterShowsFiringOnly: false,
        ...params,
      }),
    { wrapper }
  );

beforeEach(() => {
  mockPost.mockReset();
});

describe("usePresetAlertCount request", () => {
  it("posts only the cel for an ungrouped count", async () => {
    mockPost.mockResolvedValue(7);
    const { result } = renderCount();

    await waitFor(() => expect(result.current.totalCount).toBe(7));
    expect(mockPost).toHaveBeenCalledWith(COUNT_URL, { cel: WRAPPED_CEL });
    expect(result.current.isError).toBe(false);
  });

  it("asks for distinct incidents with the active status by default", async () => {
    mockPost.mockResolvedValue(3);
    const { result } = renderCount({ groupBy: "incident" });

    await waitFor(() => expect(result.current.totalCount).toBe(3));
    expect(mockPost).toHaveBeenCalledWith(COUNT_URL, {
      cel: WRAPPED_CEL,
      group_by: "incident",
      incident_status: "active",
    });
  });

  it("forwards an explicit incident status", async () => {
    mockPost.mockResolvedValue(2);
    renderCount({ groupBy: "incident", incidentStatus: "acknowledged" });

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect(mockPost).toHaveBeenCalledWith(COUNT_URL, {
      cel: WRAPPED_CEL,
      group_by: "incident",
      incident_status: "acknowledged",
    });
  });

  it("never sends an incident status for another field", async () => {
    mockPost.mockResolvedValue(4);
    renderCount({ groupBy: "service", incidentStatus: "firing" });

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect(mockPost).toHaveBeenCalledWith(COUNT_URL, {
      cel: WRAPPED_CEL,
      group_by: "service",
    });
  });

  it("composes the firing-only filter into the cel", async () => {
    mockPost.mockResolvedValue(1);
    renderCount({ groupBy: "incident", counterShowsFiringOnly: true });

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect(mockPost).toHaveBeenCalledWith(COUNT_URL, {
      cel: `(status == 'firing') && ${WRAPPED_CEL}`,
      group_by: "incident",
      incident_status: "active",
    });
  });

  it("does not request anything when disabled", async () => {
    const { result } = renderCount({ groupBy: "incident", enabled: false });

    expect(result.current.totalCount).toBe(0);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("keeps different groupings of the same filter apart", async () => {
    mockPost.mockImplementation((_url: string, body: { group_by?: string }) =>
      Promise.resolve(body.group_by === "incident" ? 3 : 9)
    );
    const { result } = renderHook(
      () => ({
        incidents: usePresetAlertCount({
          presetCel: CEL,
          counterShowsFiringOnly: false,
          groupBy: "incident",
        }),
        services: usePresetAlertCount({
          presetCel: CEL,
          counterShowsFiringOnly: false,
          groupBy: "service",
        }),
      }),
      { wrapper }
    );

    await waitFor(() => {
      expect(result.current.incidents.totalCount).toBe(3);
      expect(result.current.services.totalCount).toBe(9);
    });
  });
});

describe("usePresetAlertCount failures", () => {
  it("reports a failed grouped request as an error", async () => {
    mockPost.mockRejectedValue(new Error("boom"));
    const { result } = renderCount({ groupBy: "incident" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.totalCount).toBe(0);
  });

  it("keeps an ungrouped failure reading as zero without an error flag", async () => {
    mockPost.mockRejectedValue(new Error("boom"));
    const { result } = renderCount();

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current).toEqual({
      totalCount: 0,
      isLoading: false,
      isError: false,
    });
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest src/features/presets/custom-preset-links/model/__tests__/usePresetAlertCount.test.tsx`
Expected: the grouped cases fail (the body has no `group_by`, there is no `isError`); the ungrouped payload case already passes.

- [ ] **Step 3: Write the implementation**

Replace the contents of `src/features/presets/custom-preset-links/model/usePresetAlertCount.ts` with:

```ts
import { useApi } from "@/shared/lib/hooks/useApi";
import useSWR from "swr";
import { useEffect, useMemo } from "react";
import { buildPresetAlertCel } from "./usePresetAlertsCount";
import {
  CountByField,
  DEFAULT_INCIDENT_STATUS,
  IncidentStatusFilter,
} from "@/entities/presets/model/count-by";

type UsePresetAlertCountParams = {
  presetCel: string;
  counterShowsFiringOnly: boolean;
  groupBy?: CountByField;
  incidentStatus?: IncidentStatusFilter;
  refreshInterval?: number;
  enabled?: boolean;
};

export const usePresetAlertCount = ({
  presetCel,
  counterShowsFiringOnly,
  groupBy,
  incidentStatus,
  refreshInterval,
  enabled = true,
}: UsePresetAlertCountParams) => {
  const api = useApi();
  const requestUrl = "/alerts/query/count";
  const query = useMemo(
    () =>
      enabled
        ? {
            cel: buildPresetAlertCel(presetCel, counterShowsFiringOnly),
            ...(groupBy ? { group_by: groupBy } : {}),
            ...(groupBy === "incident"
              ? { incident_status: incidentStatus ?? DEFAULT_INCIDENT_STATUS }
              : {}),
          }
        : undefined,
    [counterShowsFiringOnly, enabled, groupBy, incidentStatus, presetCel]
  );

  const swrKey = () =>
    api.isReady() && query
      ? requestUrl +
        Object.entries(query)
          .sort(([fstKey], [scdKey]) => fstKey.localeCompare(scdKey))
          .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
          .join("&")
      : null;

  const { data, error, isLoading, mutate } = useSWR<number>(
    swrKey,
    () => api.post(requestUrl, query),
    { revalidateOnFocus: false }
  );

  useEffect(() => {
    if (!refreshInterval || !enabled) {
      return;
    }

    const intervalId = setInterval(() => mutate(), refreshInterval);
    return () => clearInterval(intervalId);
  }, [enabled, mutate, refreshInterval]);

  return {
    totalCount: data ?? 0,
    isLoading,
    isError: Boolean(groupBy) && Boolean(error),
  };
};
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest src/features/presets/custom-preset-links`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git add src/features/presets/custom-preset-links/model/usePresetAlertCount.ts src/features/presets/custom-preset-links/model/__tests__/usePresetAlertCount.test.tsx
git commit -m "feat(presets): request grouped counts and expose grouped failures" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 7: `CountByControl` (segmented Count control with its conditional selects)

**Files:**
- Create: `src/app/(keep)/dashboard/widget-types/preset/count-by-control.tsx`
- Test: `src/app/(keep)/dashboard/widget-types/preset/__tests__/count-by-control.test.tsx`

**Interfaces:**
- Consumes (Task 5): `CountBy`, `CountMode`, `CountByAlertField`, `IncidentStatusFilter`, `COUNT_BY_ALERT_FIELD_OPTIONS`, `INCIDENT_STATUS_OPTIONS`, `DEFAULT_INCIDENT_STATUS`, `getCountMode`.
- Produces (used by Task 8): `CountByControl: React.FC<{ value?: CountBy; onChange: (value?: CountBy) => void }>`. It is fully controlled: choosing `Alerts` calls `onChange(undefined)`; `Incidents` calls `onChange({ field: "incident", incidentStatus: "active" })`; `Other field` calls `onChange({ field: "name" })`; the status select calls `onChange({ field: "incident", incidentStatus })`; the field select calls `onChange({ field })`; re-clicking the active mode calls nothing. DOM hooks: radiogroup labelled "Count" with radios named `Alerts`, `Incidents`, `Other field` (`data-cy="dashboard-widget-form-count-mode-alerts|incidents|field"`), status select `data-cy="dashboard-widget-form-incident-status-select"`, field select `data-cy="dashboard-widget-form-count-field-select"`.

- [ ] **Step 1: Write the failing test**

Create `src/app/(keep)/dashboard/widget-types/preset/__tests__/count-by-control.test.tsx`:

```tsx
import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { CountByControl } from "../count-by-control";
import { CountBy } from "@/entities/presets/model/count-by";

const setup = (value?: CountBy) => {
  const onChange = jest.fn();
  const utils = render(<CountByControl value={value} onChange={onChange} />);
  return { onChange, ...utils };
};

const radio = (name: string) => screen.getByRole("radio", { name });

const choose = async (
  container: HTMLElement,
  dataCy: string,
  optionName: string
) => {
  const root = container.querySelector(`[data-cy="${dataCy}"]`) as HTMLElement;
  fireEvent.click(within(root).getByRole("button"));
  const listbox = await screen.findByRole("listbox");
  fireEvent.click(within(listbox).getByRole("option", { name: optionName }));
};

describe("CountByControl modes", () => {
  it("counts alerts by default and shows no extra controls", () => {
    setup(undefined);

    expect(radio("Alerts")).toHaveAttribute("aria-checked", "true");
    expect(radio("Incidents")).toHaveAttribute("aria-checked", "false");
    expect(radio("Other field")).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByText("Incident status")).toBeNull();
    expect(screen.queryByText("Field")).toBeNull();
  });

  it("selects incidents with the active status", () => {
    const { onChange } = setup(undefined);

    fireEvent.click(radio("Incidents"));

    expect(onChange).toHaveBeenCalledWith({
      field: "incident",
      incidentStatus: "active",
    });
  });

  it("selects the first other field", () => {
    const { onChange } = setup(undefined);

    fireEvent.click(radio("Other field"));

    expect(onChange).toHaveBeenCalledWith({ field: "name" });
  });

  it("goes back to counting alerts by clearing the setting", () => {
    const { onChange } = setup({ field: "incident", incidentStatus: "firing" });

    fireEvent.click(radio("Alerts"));

    expect(onChange).toHaveBeenCalledWith(undefined);
  });

  it("does nothing when the selected mode is clicked again", () => {
    const { onChange } = setup({ field: "service" });

    fireEvent.click(radio("Other field"));

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("CountByControl incidents", () => {
  const value: CountBy = { field: "incident", incidentStatus: "acknowledged" };

  it("explains what is counted and shows the current status", () => {
    const { container } = setup(value);

    expect(radio("Incidents")).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByText(
        "Distinct incidents with at least one alert matching the preset."
      )
    ).toBeInTheDocument();
    const statusRoot = container.querySelector(
      '[data-cy="dashboard-widget-form-incident-status-select"]'
    ) as HTMLElement;
    expect(within(statusRoot).getByRole("button")).toHaveTextContent(
      "Acknowledged only"
    );
  });

  it("defaults the displayed status to active", () => {
    const { container } = setup({ field: "incident" });

    const statusRoot = container.querySelector(
      '[data-cy="dashboard-widget-form-incident-status-select"]'
    ) as HTMLElement;
    expect(within(statusRoot).getByRole("button")).toHaveTextContent(
      "Active (firing + acknowledged)"
    );
  });

  it("changes the incident status", async () => {
    const { onChange, container } = setup(value);

    await choose(
      container,
      "dashboard-widget-form-incident-status-select",
      "Firing only"
    );

    expect(onChange).toHaveBeenCalledWith({
      field: "incident",
      incidentStatus: "firing",
    });
  });
});

describe("CountByControl other fields", () => {
  it("explains what is counted and shows the current field", () => {
    const { container } = setup({ field: "service" });

    expect(radio("Other field")).toHaveAttribute("aria-checked", "true");
    expect(
      screen.getByText(
        "Counts distinct values of that field. Empty values aren't counted."
      )
    ).toBeInTheDocument();
    const fieldRoot = container.querySelector(
      '[data-cy="dashboard-widget-form-count-field-select"]'
    ) as HTMLElement;
    expect(within(fieldRoot).getByRole("button")).toHaveTextContent("Service");
  });

  it("changes the field", async () => {
    const { onChange, container } = setup({ field: "service" });

    await choose(container, "dashboard-widget-form-count-field-select", "Site");

    expect(onChange).toHaveBeenCalledWith({ field: "site" });
  });

  it("offers exactly the allowlisted fields", async () => {
    const { container } = setup({ field: "name" });
    const fieldRoot = container.querySelector(
      '[data-cy="dashboard-widget-form-count-field-select"]'
    ) as HTMLElement;

    fireEvent.click(within(fieldRoot).getByRole("button"));
    const listbox = await screen.findByRole("listbox");

    expect(
      within(listbox)
        .getAllByRole("option")
        .map((option) => option.textContent)
    ).toEqual([
      "Alert name",
      "Service",
      "Host",
      "Application",
      "Site",
      "Assignee",
    ]);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__/count-by-control.test.tsx"`
Expected: FAIL with "Cannot find module '../count-by-control'".

- [ ] **Step 3: Write the component**

Create `src/app/(keep)/dashboard/widget-types/preset/count-by-control.tsx`:

```tsx
import React from "react";
import { Select, SelectItem, Subtitle, Text } from "@tremor/react";
import {
  COUNT_BY_ALERT_FIELD_OPTIONS,
  CountBy,
  CountByAlertField,
  CountMode,
  DEFAULT_INCIDENT_STATUS,
  INCIDENT_STATUS_OPTIONS,
  IncidentStatusFilter,
  getCountMode,
} from "@/entities/presets/model/count-by";

interface CountByControlProps {
  value?: CountBy;
  onChange: (value?: CountBy) => void;
}

const MODES: ReadonlyArray<{ mode: CountMode; label: string }> = [
  { mode: "alerts", label: "Alerts" },
  { mode: "incidents", label: "Incidents" },
  { mode: "field", label: "Other field" },
];

/**
 * Chooses what a preset widget counts: alerts (the default), distinct
 * incidents, or distinct values of one allowlisted alert field.
 */
export const CountByControl: React.FC<CountByControlProps> = ({
  value,
  onChange,
}) => {
  const mode = getCountMode(value);

  const selectMode = (nextMode: CountMode) => {
    if (nextMode === mode) {
      return;
    }
    if (nextMode === "alerts") {
      onChange(undefined);
    } else if (nextMode === "incidents") {
      onChange({ field: "incident", incidentStatus: DEFAULT_INCIDENT_STATUS });
    } else {
      onChange({ field: COUNT_BY_ALERT_FIELD_OPTIONS[0].value });
    }
  };

  return (
    <div className="mb-4 mt-2" data-cy="dashboard-widget-form-count-by">
      <Subtitle>Count</Subtitle>
      <div
        role="radiogroup"
        aria-label="Count"
        className="mt-1 flex overflow-hidden rounded-md border border-gray-300 text-sm"
      >
        {MODES.map(({ mode: optionMode, label }) => {
          const selected = optionMode === mode;
          return (
            <button
              key={optionMode}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => selectMode(optionMode)}
              data-cy={`dashboard-widget-form-count-mode-${optionMode}`}
              className={`flex-1 border-r border-gray-200 py-1.5 last:border-r-0 ${
                selected
                  ? "bg-orange-50 font-semibold text-orange-700 ring-1 ring-inset ring-orange-500"
                  : "bg-white text-gray-700 hover:bg-gray-50"
              }`}
            >
              {label}
            </button>
          );
        })}
      </div>
      {mode === "incidents" && (
        <>
          <Text className="mt-1 text-xs">
            Distinct incidents with at least one alert matching the preset.
          </Text>
          <div className="mt-3">
            <Subtitle>Incident status</Subtitle>
            <Select
              value={value?.incidentStatus ?? DEFAULT_INCIDENT_STATUS}
              onValueChange={(status) =>
                onChange({
                  field: "incident",
                  incidentStatus: status as IncidentStatusFilter,
                })
              }
              data-cy="dashboard-widget-form-incident-status-select"
            >
              {INCIDENT_STATUS_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </Select>
          </div>
        </>
      )}
      {mode === "field" && (
        <>
          <div className="mt-3">
            <Subtitle>Field</Subtitle>
            <Select
              value={value?.field ?? COUNT_BY_ALERT_FIELD_OPTIONS[0].value}
              onValueChange={(field) =>
                onChange({ field: field as CountByAlertField })
              }
              data-cy="dashboard-widget-form-count-field-select"
            >
              {COUNT_BY_ALERT_FIELD_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </Select>
          </div>
          <Text className="mt-1 text-xs">
            Counts distinct values of that field. Empty values aren't counted.
          </Text>
        </>
      )}
    </div>
  );
};
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__/count-by-control.test.tsx"`
Expected: all pass. Tremor's `Select` renders a hidden native `<select>` next to the real control; always scope option queries to the `listbox` as the test does. If a Headless UI `getAnimations` warning prints, ignore it.

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git add "src/app/(keep)/dashboard/widget-types/preset/count-by-control.tsx" "src/app/(keep)/dashboard/widget-types/preset/__tests__/count-by-control.test.tsx"
git commit -m "feat(dashboard): add the Count control for preset widgets" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 8: Preset widget form emits `countBy`

**Files:**
- Modify: `src/app/(keep)/dashboard/widget-types/preset/preset-widget-form.tsx`
- Test: `src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-widget-form.test.tsx`

**Interfaces:**
- Consumes (Task 5/7): `CountBy`, `CountByControl`, `WidgetData.countBy`.
- Produces: every `onChange(formValue, isValid)` call from `PresetWidgetForm` now includes a `countBy` key (the saved value when editing, `undefined` when counting alerts). The key is always present so that `WidgetModal`'s `{ ...stripForeignTypeFields(editingItem, type), ...formValue }` overwrites a previously saved `countBy` instead of keeping it.

- [ ] **Step 1: Write the failing test**

Create `src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-widget-form.test.tsx`:

```tsx
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { PresetWidgetForm } from "../preset-widget-form";
import { Preset } from "@/entities/presets/model/types";
import { CountBy } from "@/entities/presets/model/count-by";
import { PresetPanelType, WidgetData } from "../../../types";

jest.mock("../columns-selection", () => ({
  __esModule: true,
  default: () => null,
}));

const presets = [{ id: "p1", name: "feed" }] as unknown as Preset[];

const savedWidget = (overrides: Partial<WidgetData> = {}) =>
  ({
    preset: { id: "p1", name: "feed", countOfLastAlerts: 5 },
    presetPanelType: PresetPanelType.ALERT_COUNT_PANEL,
    thresholds: [{ value: 0, color: "#10b981" }],
    showFiringOnly: false,
    customLink: "",
    ...overrides,
  }) as unknown as WidgetData;

const lastValue = (onChange: jest.Mock) =>
  onChange.mock.calls[onChange.mock.calls.length - 1][0];

const renderForm = (editingItem?: WidgetData) => {
  const onChange = jest.fn();
  render(
    <PresetWidgetForm
      presets={presets}
      editingItem={editingItem}
      onChange={onChange}
    />
  );
  return onChange;
};

describe("PresetWidgetForm countBy", () => {
  it("emits no countBy for a new widget", () => {
    const onChange = renderForm();

    expect(Object.keys(lastValue(onChange))).toContain("countBy");
    expect(lastValue(onChange).countBy).toBeUndefined();
  });

  it("emits the chosen incident count", () => {
    const onChange = renderForm();

    fireEvent.click(screen.getByRole("radio", { name: "Incidents" }));

    expect(lastValue(onChange).countBy).toEqual({
      field: "incident",
      incidentStatus: "active",
    });
  });

  it("loads the saved countBy when editing", () => {
    const saved: CountBy = { field: "incident", incidentStatus: "firing" };
    const onChange = renderForm(savedWidget({ countBy: saved }));

    expect(lastValue(onChange).countBy).toEqual(saved);
    expect(screen.getByRole("radio", { name: "Incidents" })).toHaveAttribute(
      "aria-checked",
      "true"
    );
  });

  it("drops a saved countBy when the user goes back to counting alerts", () => {
    const widget = savedWidget({
      countBy: { field: "incident", incidentStatus: "active" },
    });
    const onChange = renderForm(widget);

    fireEvent.click(screen.getByRole("radio", { name: "Alerts" }));

    const emitted = lastValue(onChange);
    expect(Object.keys(emitted)).toContain("countBy");
    expect(emitted.countBy).toBeUndefined();
    const merged = { ...widget, ...emitted };
    expect(merged.countBy).toBeUndefined();
  });

  it("offers the control for the alert table panel too", () => {
    renderForm(savedWidget({ presetPanelType: PresetPanelType.ALERT_TABLE }));

    expect(screen.getByRole("radiogroup", { name: "Count" })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__/preset-widget-form.test.tsx"`
Expected: FAIL (no `countBy` key emitted, no radiogroup rendered).

- [ ] **Step 3: Wire the control into the form**

In `preset-widget-form.tsx`:

1. After the line `import ColumnsSelection from "./columns-selection";` add:

```tsx
import { CountByControl } from "./count-by-control";
import { CountBy } from "@/entities/presets/model/count-by";
```

2. After

```tsx
  const [presetColumns, setPresetColumns] = useState<string[] | undefined>(
    editingItem ? editingItem.presetColumns : undefined
  );
```

add:

```tsx
  const [countBy, setCountBy] = useState<CountBy | undefined>(
    editingItem?.countBy
  );
```

3. Replace

```tsx
      showFiringOnly: formValues.showFiringOnly ?? false,
      customLink: formValues.customLink || "",
    };
  }, [formValues, presetColumns]);
```

with:

```tsx
      showFiringOnly: formValues.showFiringOnly ?? false,
      customLink: formValues.customLink || "",
      countBy,
    };
  }, [formValues, presetColumns, countBy]);
```

4. Replace

```tsx
        showFiringOnly: normalizedFormValues.showFiringOnly,
        customLink: normalizedFormValues.customLink,
      },
      isValid
```

with:

```tsx
        showFiringOnly: normalizedFormValues.showFiringOnly,
        customLink: normalizedFormValues.customLink,
        countBy: normalizedFormValues.countBy,
      },
      isValid
```

5. Replace `      {formValues.presetPanelType === PresetPanelType.ALERT_COUNT_PANEL && (` with:

```tsx
      <CountByControl value={countBy} onChange={setCountBy} />
      {formValues.presetPanelType === PresetPanelType.ALERT_COUNT_PANEL && (
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__"`
Expected: all pass (the form, control, panel and grid-item suites).

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git add "src/app/(keep)/dashboard/widget-types/preset/preset-widget-form.tsx" "src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-widget-form.test.tsx"
git commit -m "feat(dashboard): let preset widgets choose what they count" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 9: Counter tile renders the grouped number with its dot caption

**Files:**
- Modify: `src/app/(keep)/dashboard/widget-types/preset/widget-alert-count-panel.tsx`
- Modify: `src/app/(keep)/dashboard/widget-types/preset/preset-grid-item.tsx`
- Test: `src/app/(keep)/dashboard/widget-types/preset/__tests__/widget-alert-count-panel.test.tsx`
- Test: `src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-grid-item.test.tsx`

**Interfaces:**
- Consumes (Task 5): `CountBy`, `getCountUnitLabel(countBy, count)`, `COUNT_LOAD_ERROR_LABEL`. Consumes (Task 6): `usePresetAlertCount({ presetCel, counterShowsFiringOnly, groupBy?, incidentStatus?, refreshInterval?, enabled? })` returning `{ totalCount: number; isLoading: boolean; isError: boolean }`, where `isError` is only ever `true` for grouped requests.
- Produces: `WidgetAlertCountPanel` accepts `countBy?: CountBy`. DOM hooks used by tests: `data-cy="dashboard-widget-count-value"` (the number or `—`) and `data-cy="dashboard-widget-count-caption"` (the dot + label), both only when `countBy` is set. Ungrouped tiles render exactly the markup they render today.

- [ ] **Step 1: Write the failing panel test**

Create `src/app/(keep)/dashboard/widget-types/preset/__tests__/widget-alert-count-panel.test.tsx`:

```tsx
import React from "react";
import { render, screen } from "@testing-library/react";
import WidgetAlertCountPanel from "../widget-alert-count-panel";
import { CountBy } from "@/entities/presets/model/count-by";

const mockUsePresetAlertCount = jest.fn();

jest.mock("@/features/presets/custom-preset-links", () => ({
  usePresetAlertCount: (...args: unknown[]) => mockUsePresetAlertCount(...args),
}));

jest.mock("@/utils/hooks/useDashboardPresets", () => ({
  useDashboardPreset: () => [
    {
      id: "p1",
      name: "feed",
      options: [{ label: "CEL", value: "severity == 'critical'" }],
    },
  ],
}));

jest.mock("../../../MenuButton", () => ({
  __esModule: true,
  default: () => null,
}));

const thresholds = [
  { value: 0, color: "#10b981" },
  { value: 10, color: "#dc2626" },
];

const incidents: CountBy = { field: "incident", incidentStatus: "active" };

const hookState = (state: {
  totalCount?: number;
  isLoading?: boolean;
  isError?: boolean;
}) =>
  mockUsePresetAlertCount.mockReturnValue({
    totalCount: 0,
    isLoading: false,
    isError: false,
    ...state,
  });

const renderPanel = (props: { countBy?: CountBy } = {}) =>
  render(
    <WidgetAlertCountPanel presetName="feed" thresholds={thresholds} {...props} />
  );

const valueOf = (container: HTMLElement) =>
  container.querySelector('[data-cy="dashboard-widget-count-value"]');

const captionOf = (container: HTMLElement) =>
  container.querySelector('[data-cy="dashboard-widget-count-caption"]');

beforeEach(() => {
  mockUsePresetAlertCount.mockReset();
});

describe("WidgetAlertCountPanel counting alerts", () => {
  it("renders the plain number with no caption and no grouping", () => {
    hookState({ totalCount: 120 });
    const { container } = renderPanel();

    expect(screen.getByText("120")).toBeInTheDocument();
    expect(captionOf(container)).toBeNull();
    expect(valueOf(container)).toBeNull();
    expect(mockUsePresetAlertCount).toHaveBeenCalledWith(
      expect.objectContaining({ groupBy: undefined, incidentStatus: undefined })
    );
  });
});

describe("WidgetAlertCountPanel counting distinct values", () => {
  it("asks the hook for the grouped count", () => {
    hookState({ totalCount: 3 });
    renderPanel({ countBy: incidents });

    expect(mockUsePresetAlertCount).toHaveBeenCalledWith(
      expect.objectContaining({
        groupBy: "incident",
        incidentStatus: "active",
        enabled: true,
      })
    );
  });

  it("shows the number above a dot caption", () => {
    hookState({ totalCount: 3 });
    const { container } = renderPanel({ countBy: incidents });

    expect(valueOf(container)).toHaveTextContent("3");
    expect(captionOf(container)).toHaveTextContent("Active incidents");
  });

  it("uses the singular label at one", () => {
    hookState({ totalCount: 1 });
    const { container } = renderPanel({ countBy: { field: "service" } });

    expect(captionOf(container)).toHaveTextContent(/^Service$/);
  });

  it("colours the number from the grouped count, not the alert count", () => {
    hookState({ totalCount: 3 });
    const { container, rerender } = renderPanel({ countBy: incidents });
    expect(valueOf(container)).toHaveStyle({ color: "#10b981" });

    hookState({ totalCount: 27 });
    rerender(
      <WidgetAlertCountPanel
        presetName="feed"
        thresholds={thresholds}
        countBy={incidents}
      />
    );
    expect(valueOf(container)).toHaveStyle({ color: "#dc2626" });
  });

  it("shows zero as 0", () => {
    hookState({ totalCount: 0 });
    const { container } = renderPanel({ countBy: incidents });

    expect(valueOf(container)).toHaveTextContent("0");
  });

  it("keeps the caption while loading and shows no number", () => {
    hookState({ isLoading: true });
    const { container } = renderPanel({ countBy: incidents });

    expect(captionOf(container)).toHaveTextContent("Active incidents");
    expect(valueOf(container)).not.toHaveTextContent(/\d/);
  });

  it("shows a dash and a neutral tile when the count fails, never 0", () => {
    hookState({ totalCount: 0, isError: true });
    const { container } = renderPanel({ countBy: incidents });

    expect(valueOf(container)).toHaveTextContent("—");
    expect(valueOf(container)).toHaveStyle({ color: "#9ca3af" });
    expect(captionOf(container)).toHaveTextContent("Couldn't load count");
    expect(screen.queryByText("0")).toBeNull();
  });
});
```

Create `src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-grid-item.test.tsx`:

```tsx
import React from "react";
import { render, screen } from "@testing-library/react";
import PresetGridItem from "../preset-grid-item";
import { PresetPanelType, WidgetData, WidgetType } from "../../../types";

const mockCountPanel = jest.fn();

jest.mock("../widget-alert-count-panel", () => ({
  __esModule: true,
  default: (props: unknown) => {
    mockCountPanel(props);
    return <div data-testid="count-panel" />;
  },
}));

jest.mock("../preset-alert-table-panel", () => ({
  __esModule: true,
  default: () => <div data-testid="table-panel" />,
}));

jest.mock("@/utils/hooks/useDashboardPresets", () => ({
  useDashboardPreset: () => [{ id: "p1", name: "feed", options: [] }],
}));

jest.mock("next/navigation", () => ({
  useParams: () => ({ id: "ops" }),
  useSearchParams: () => new URLSearchParams(),
}));

const buildItem = (overrides: Partial<WidgetData>) =>
  ({
    i: "w1",
    x: 0,
    y: 0,
    w: 4,
    h: 4,
    name: "Prod Critical",
    widgetType: WidgetType.PRESET,
    preset: { id: "p1", name: "feed" },
    ...overrides,
  }) as unknown as WidgetData;

beforeEach(() => mockCountPanel.mockClear());

describe("PresetGridItem", () => {
  it("passes countBy to the counter tile", () => {
    const countBy = { field: "incident", incidentStatus: "firing" } as const;
    render(
      <PresetGridItem
        item={buildItem({
          presetPanelType: PresetPanelType.ALERT_COUNT_PANEL,
          countBy,
        })}
      />
    );

    expect(screen.getByTestId("count-panel")).toBeInTheDocument();
    expect(mockCountPanel).toHaveBeenCalledWith(
      expect.objectContaining({ countBy })
    );
  });

  it("renders the table panel for table widgets", () => {
    render(
      <PresetGridItem
        item={buildItem({ presetPanelType: PresetPanelType.ALERT_TABLE })}
      />
    );

    expect(screen.getByTestId("table-panel")).toBeInTheDocument();
    expect(mockCountPanel).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__/widget-alert-count-panel.test.tsx" "src/app/\(keep\)/dashboard/widget-types/preset/__tests__/preset-grid-item.test.tsx"`
Expected: the grouped-panel tests fail (no `data-cy` elements, `groupBy` never passed); the plain-count test fails only on the `groupBy: undefined` key check until Step 3 adds it; the grid-item pass-through test fails (`countBy` not forwarded).

- [ ] **Step 3: Edit the counter tile**

In `widget-alert-count-panel.tsx`:

1. After the line `import { usePresetAlertCount } from "@/features/presets/custom-preset-links";` add:

```tsx
import {
  COUNT_LOAD_ERROR_LABEL,
  CountBy,
  getCountUnitLabel,
} from "@/entities/presets/model/count-by";
```

2. Replace `interface WidgetAlertCountPanelProps {` with:

```tsx
const ERROR_COLOR = "#9ca3af";

interface WidgetAlertCountPanelProps {
```

3. In the props interface, replace `  onSave?: () => void;\n}` with:

```tsx
  onSave?: () => void;
  countBy?: CountBy;
}
```

4. In the destructuring, replace `  onDelete,\n  onSave,\n}) => {` with:

```tsx
  onDelete,
  onSave,
  countBy,
}) => {
```

5. Replace the hook call

```tsx
  const { totalCount: alertsCount, isLoading } = usePresetAlertCount({
    presetCel: filterCel,
    counterShowsFiringOnly: showFiringOnly,
    refreshInterval: 30000,
    enabled: !!preset,
  });
```

with:

```tsx
  const {
    totalCount: alertsCount,
    isLoading,
    isError,
  } = usePresetAlertCount({
    presetCel: filterCel,
    counterShowsFiringOnly: showFiringOnly,
    groupBy: countBy?.field,
    incidentStatus: countBy?.incidentStatus,
    refreshInterval: 30000,
    enabled: !!preset,
  });
```

6. Replace `  const color = getColor(isCountLoading ? 0 : alertsCount);` with:

```tsx
  const color = isError
    ? ERROR_COLOR
    : getColor(isCountLoading ? 0 : alertsCount);

  const caption = countBy
    ? isError
      ? COUNT_LOAD_ERROR_LABEL
      : getCountUnitLabel(countBy, alertsCount)
    : undefined;
```

7. Replace the number block

```tsx
        <div
          className="flex-1 flex items-center justify-center min-h-0 text-4xl font-black tracking-tight"
          style={{
            color,
            textShadow: "0 1px 2px rgba(0,0,0,0.1)",
          }}
        >
          {isCountLoading ? (
            <Skeleton containerClassName="h-8 w-16" />
          ) : (
            alertsCount
          )}
        </div>
```

with:

```tsx
        {countBy ? (
          <div className="flex-1 flex flex-col items-center justify-center min-h-0 gap-1.5">
            <div
              className="text-4xl font-black tracking-tight leading-none"
              style={{
                color,
                textShadow: "0 1px 2px rgba(0,0,0,0.1)",
              }}
              data-cy="dashboard-widget-count-value"
            >
              {isCountLoading ? (
                <Skeleton containerClassName="h-8 w-16" />
              ) : isError ? (
                "—"
              ) : (
                alertsCount
              )}
            </div>
            <div
              className="flex max-w-full items-center gap-1.5 text-xs font-semibold text-gray-700"
              data-cy="dashboard-widget-count-caption"
            >
              <span
                className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
                style={{
                  background: color,
                  boxShadow: `0 0 0 3px ${hexToRgb(color, 0.28)}`,
                }}
              />
              <span className="truncate" title={caption}>
                {caption}
              </span>
            </div>
          </div>
        ) : (
          <div
            className="flex-1 flex items-center justify-center min-h-0 text-4xl font-black tracking-tight"
            style={{
              color,
              textShadow: "0 1px 2px rgba(0,0,0,0.1)",
            }}
          >
            {isCountLoading ? (
              <Skeleton containerClassName="h-8 w-16" />
            ) : (
              alertsCount
            )}
          </div>
        )}
```

In `preset-grid-item.tsx`, replace `          customLink={item.customLink}` with:

```tsx
          customLink={item.customLink}
          countBy={item.countBy}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__" "src/app/\(keep\)/dashboard/__tests__/GridItem.test.tsx"`
Expected: all pass (the existing GridItem tests mock `PresetGridItem` and must be unaffected).

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git add "src/app/(keep)/dashboard/widget-types/preset/widget-alert-count-panel.tsx" "src/app/(keep)/dashboard/widget-types/preset/preset-grid-item.tsx" "src/app/(keep)/dashboard/widget-types/preset/__tests__/widget-alert-count-panel.test.tsx" "src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-grid-item.test.tsx"
git commit -m "feat(dashboard): show the distinct count with a dot caption on the counter tile" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 10: Alert Table widget shows a separate group-count line

**Files:**
- Modify: `src/app/(keep)/dashboard/widget-types/preset/preset-alert-table-panel.tsx`
- Test: `src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-alert-table-panel.test.tsx`

**Interfaces:**
- Consumes (Task 5): `getCountUnitLabel`, `COUNT_LOAD_ERROR_LABEL`; `WidgetData.countBy`. Consumes (Task 6): `usePresetAlertCount` as in Task 9, plus the existing `usePresetAlertsCount` (unchanged).
- Produces: when `item.countBy` is set the header shows `data-cy="dashboard-widget-group-count"` above the alerts-count line; thresholds and the table tint follow the grouped number; the alerts-count line is neutral grey. When `item.countBy` is unset nothing changes and the grouped hook is called with `enabled: false` (no request).

- [ ] **Step 1: Write the failing test**

Create `src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-alert-table-panel.test.tsx`:

```tsx
import React from "react";
import { render, screen } from "@testing-library/react";
import PresetAlertTablePanel from "../preset-alert-table-panel";
import { Preset } from "@/entities/presets/model/types";
import { PresetPanelType, WidgetData, WidgetType } from "../../../types";

const mockUsePresetAlertsCount = jest.fn();
const mockUsePresetAlertCount = jest.fn();
let mockLastTableBackground: string | undefined;

jest.mock("@/features/presets/custom-preset-links", () => ({
  usePresetAlertsCount: (...args: unknown[]) =>
    mockUsePresetAlertsCount(...args),
  usePresetAlertCount: (...args: unknown[]) => mockUsePresetAlertCount(...args),
}));

jest.mock("../widget-alerts-table", () => ({
  __esModule: true,
  default: ({ background }: { background?: string }) => {
    mockLastTableBackground = background;
    return <div data-testid="alerts-table" />;
  },
}));

const thresholds = [
  { value: 0, color: "#10b981" },
  { value: 10, color: "#dc2626" },
];

const buildItem = (overrides: Partial<WidgetData> = {}) =>
  ({
    i: "w1",
    x: 0,
    y: 0,
    w: 8,
    h: 6,
    name: "Prod Critical",
    widgetType: WidgetType.PRESET,
    preset: { id: "p1", name: "feed", countOfLastAlerts: 5 },
    presetPanelType: PresetPanelType.ALERT_TABLE,
    thresholds,
    ...overrides,
  }) as unknown as WidgetData;

const renderPanel = (item: WidgetData) =>
  render(
    <PresetAlertTablePanel
      item={item}
      preset={{ id: "p1", name: "feed" } as unknown as Preset}
      filterCel="severity == 'critical'"
    />
  );

const groupState = (state: {
  totalCount?: number;
  isLoading?: boolean;
  isError?: boolean;
}) =>
  mockUsePresetAlertCount.mockReturnValue({
    totalCount: 0,
    isLoading: false,
    isError: false,
    ...state,
  });

const incidents = { field: "incident", incidentStatus: "active" } as const;

beforeEach(() => {
  mockUsePresetAlertsCount.mockReset();
  mockUsePresetAlertCount.mockReset();
  mockLastTableBackground = undefined;
  mockUsePresetAlertsCount.mockReturnValue({
    alerts: [],
    totalCount: 120,
    isLoading: false,
  });
  groupState({});
});

describe("PresetAlertTablePanel without grouping", () => {
  it("renders today's header and does not request a grouped count", () => {
    const { container } = renderPanel(buildItem());

    expect(screen.getByText("showing 5 out of 120")).toBeInTheDocument();
    expect(
      container.querySelector('[data-cy="dashboard-widget-group-count"]')
    ).toBeNull();
    expect(mockUsePresetAlertCount).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: false })
    );
    expect(mockLastTableBackground).toBe("rgb(220, 38, 38, 0.1)");
  });
});

describe("PresetAlertTablePanel with grouping", () => {
  it("requests the grouped count and shows it above the alerts line", () => {
    groupState({ totalCount: 3 });
    const { container } = renderPanel(buildItem({ countBy: incidents }));

    expect(mockUsePresetAlertCount).toHaveBeenCalledWith(
      expect.objectContaining({
        groupBy: "incident",
        incidentStatus: "active",
        enabled: true,
      })
    );
    const line = container.querySelector(
      '[data-cy="dashboard-widget-group-count"]'
    );
    expect(line).toHaveTextContent("Active incidents:");
    expect(line).toHaveTextContent("3");
    expect(screen.getByText("showing 5 out of 120")).toBeInTheDocument();
  });

  it("makes the alerts line neutral and tints the table by the grouped count", () => {
    groupState({ totalCount: 3 });
    renderPanel(buildItem({ countBy: incidents }));

    expect(
      screen.getByText("showing 5 out of 120").parentElement
    ).toHaveClass("text-gray-500");
    expect(mockLastTableBackground).toBe("rgb(16, 185, 129, 0.1)");
  });

  it("uses the singular label at one", () => {
    groupState({ totalCount: 1 });
    const { container } = renderPanel(
      buildItem({ countBy: { field: "service" } })
    );

    expect(
      container.querySelector('[data-cy="dashboard-widget-group-count"]')
    ).toHaveTextContent("Service:");
  });

  it("shows a dash with an explanation when the grouped count fails", () => {
    groupState({ totalCount: 0, isError: true });
    const { container } = renderPanel(buildItem({ countBy: incidents }));

    const line = container.querySelector(
      '[data-cy="dashboard-widget-group-count"]'
    );
    expect(line).toHaveTextContent("—");
    expect(screen.getByTitle("Couldn't load count")).toBeInTheDocument();
    expect(line).not.toHaveTextContent(/\b0\b/);
  });

  it("does not tint the table until the grouped count has loaded", () => {
    groupState({ isLoading: true });
    renderPanel(buildItem({ countBy: incidents }));

    expect(mockLastTableBackground).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__/preset-alert-table-panel.test.tsx"`
Expected: the grouped tests fail (no group-count line); the "without grouping" test fails on `expect.objectContaining({ enabled: false })` because the grouped hook is not called yet.

- [ ] **Step 3: Edit the table panel**

In `preset-alert-table-panel.tsx`:

1. Replace `import { usePresetAlertsCount } from "@/features/presets/custom-preset-links";` with:

```tsx
import {
  usePresetAlertCount,
  usePresetAlertsCount,
} from "@/features/presets/custom-preset-links";
import {
  COUNT_LOAD_ERROR_LABEL,
  getCountUnitLabel,
} from "@/entities/presets/model/count-by";
```

2. Above `const hexToRgb = (hex: string, alpha: number = 1) => {` add:

```tsx
const ERROR_COLOR = "#9ca3af";

```

3. Directly after the existing `usePresetAlertsCount(...)` call (it ends with `    !!preset\n  );`) add:

```tsx
  const countBy = item.countBy;
  const {
    totalCount: groupCount,
    isLoading: isGroupLoading,
    isError: isGroupError,
  } = usePresetAlertCount({
    presetCel: filterCel,
    counterShowsFiringOnly: false,
    groupBy: countBy?.field,
    incidentStatus: countBy?.incidentStatus,
    refreshInterval: 30000,
    enabled: !!preset && !!countBy,
  });
```

4. Replace the head of `getColor`

```tsx
  const getColor = () => {
    let color = "#000000";
    if (
      item.widgetType === WidgetType.PRESET &&
      item.thresholds &&
      item.preset
    ) {
      for (let i = item.thresholds.length - 1; i >= 0; i--) {
        if (presetAlertsCount >= item.thresholds[i].value) {
```

with:

```tsx
  const getColor = () => {
    if (countBy && isGroupError) {
      return ERROR_COLOR;
    }
    const thresholdCount = countBy ? groupCount : presetAlertsCount;
    let color = "#000000";
    if (
      item.widgetType === WidgetType.PRESET &&
      item.thresholds &&
      item.preset
    ) {
      for (let i = item.thresholds.length - 1; i >= 0; i--) {
        if (thresholdCount >= item.thresholds[i].value) {
```

5. In `renderAlertsCountText`, replace

```tsx
        <div
          className="flex items-center text-base font-bold"
          style={{ color: getColor() }}
        >
```

with:

```tsx
        <div
          className={`flex items-center text-base font-bold ${countBy ? "text-gray-500" : ""}`}
          style={countBy ? undefined : { color: getColor() }}
        >
```

6. Directly above the component's final `  return (\n    <>`, add:

```tsx
  const renderGroupCountLine = () => {
    if (!countBy) {
      return null;
    }
    const color = getColor();

    return (
      <div
        className="flex gap-1.5 items-center"
        data-cy="dashboard-widget-group-count"
      >
        <span
          className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
          style={{
            background: color,
            boxShadow: `0 0 0 3px ${hexToRgb(color, 0.28)}`,
          }}
        />
        <div>{getCountUnitLabel(countBy, groupCount)}:</div>
        <div
          className="flex items-center text-base font-bold"
          style={{ color }}
          title={isGroupError ? COUNT_LOAD_ERROR_LABEL : undefined}
        >
          {isGroupLoading ? (
            <Skeleton containerClassName="h-4 w-8 relative -top-0.5" />
          ) : isGroupError ? (
            "—"
          ) : (
            groupCount
          )}
        </div>
      </div>
    );
  };

```

7. Replace

```tsx
        <div className="flex-1 min-w-0 overflow-hidden whitespace-nowrap">
          {renderAlertsCountText()}
```

with:

```tsx
        <div className="flex-1 min-w-0 overflow-hidden whitespace-nowrap">
          {renderGroupCountLine()}
          {renderAlertsCountText()}
```

8. Replace `          background={isLoading ? undefined : hexToRgb(getColor(), 0.1)}` with:

```tsx
          background={
            isLoading || (countBy && isGroupLoading)
              ? undefined
              : hexToRgb(getColor(), 0.1)
          }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/preset/__tests__"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git add "src/app/(keep)/dashboard/widget-types/preset/preset-alert-table-panel.tsx" "src/app/(keep)/dashboard/widget-types/preset/__tests__/preset-alert-table-panel.test.tsx"
git commit -m "feat(dashboard): add a group-count line to the alert table widget" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

### Task 11: keep-ui gate (types, lint, suites)

**Files:** none modified unless a check finds a problem in lines this branch added.

- [ ] **Step 1: Typecheck**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npm run typecheck 2>&1 | tail -10`
Expected: exactly the 2 pre-existing errors from "Environment (already prepared)" and nothing else. Any error mentioning `count-by`, `countBy`, `widget-alert-count-panel`, `preset-alert-table-panel`, `preset-widget-form`, `usePresetAlertCount` or `count-by-control` is ours and must be fixed.

- [ ] **Step 2: Lint the touched areas**

Run: `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx eslint "src/app/(keep)/dashboard" src/features/presets src/entities/presets 2>&1 | tail -30`
Expected: 0 errors. Warnings must all be pre-existing (the baseline for the preset widgets folder is 4 `react-hooks/exhaustive-deps` warnings in `columns-selection.tsx`, `preset-widget-form.tsx` (two) and `widget-alerts-table.tsx`; line numbers in `preset-widget-form.tsx` will have shifted). No warning may point at a line this branch added.

- [ ] **Step 3: Run the affected suites**

Run (delegated to a sonnet subagent): `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest "src/app/\(keep\)/dashboard" src/features/presets src/entities/presets 2>&1 | tail -40`
Expected: all pass.

- [ ] **Step 4: Run the whole UI suite and compare with `origin/dev`**

Run (delegated to a sonnet subagent): `cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && npx jest 2>&1 | tail -60`
Expected: no failures beyond those that also fail on a clean `origin/dev` checkout. If something fails, re-run that file alone and decide whether it is caused by this branch (our files appear in the stack trace) before touching anything.

- [ ] **Step 5: Commit any fixes**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
git status --short
```
If it lists files, `git add` them by explicit path and `git commit -m "fix(dashboard): address count-by gate findings" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"`. Skip when nothing changed.

### Task 12: End-to-end check against the real gateway and UI

Unit tests mock the API on one side and the UI on the other. This task proves the two halves agree with each other and with the Incidents page, on the data in the shared local Postgres.

**Files:** none (scratch logs go under `/private/tmp/claude-501/-Users-yarin-keep-namespace/2a7cad5e-d1db-4168-b6d3-d586a7755f17/scratchpad/`).

**Rules for this task:** do not stop, restart or reconfigure the services already running (UI :3000/:3001, gateway :8080, Postgres :5432, Kafka :29092); run your own gateway on :8090 and your own UI on :3002; create one clearly named scratch dashboard (`count-by-verify`) in the shared DB and delete only that dashboard at the end; if the stack cannot be brought up, stop and report instead of improvising. Execute with a sonnet subagent that has the Playwright MCP tools; the main session reviews the screenshots against the mock.

- [ ] **Step 1: Start the worktree gateway on :8090 against the shared Postgres**

```bash
lsof -iTCP:8090 -sTCP:LISTEN
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
cp -n /Users/yarin/keep-namespace/keep-api-gateway/.env .env
set -a; . ./.env; set +a
export DATABASE_CONNECTION_STRING="postgresql://keep:keep@localhost:5432/keep" AUTH_TYPE=noauth MESSAGING_TYPE=KAFKA KAFKA_BOOTSTRAP_SERVERS=localhost:29092 OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES KEEP_CORS_TRUSTED_ORIGINS=http://localhost:3002 CONSUMER=false SCHEDULER=false PYTHONPATH=. PYTHONFAULTHANDLER=1
SCRATCH=/private/tmp/claude-501/-Users-yarin-keep-namespace/2a7cad5e-d1db-4168-b6d3-d586a7755f17/scratchpad
nohup /Users/yarin/keep-namespace/keep-api-gateway/.venv/bin/python -m gunicorn src.main:get_app --bind 0.0.0.0:8090 --workers 1 -k uvicorn.workers.UvicornWorker -c src/config/config.py > $SCRATCH/gw-8090.out.log 2> $SCRATCH/gw-8090.err.log &
echo $! > $SCRATCH/gw-8090.pid
```
`lsof` must print nothing first. Wait until `curl -s -o /dev/null -w '%{http_code}' localhost:8090/healthcheck` (or `/docs`) answers. Gotcha: on macOS a gunicorn worker can crash-loop (SIGSEGV) so requests hang; if that happens read `gw-8090.err.log`, then restart this gateway only, keeping `PYTHONFAULTHANDLER=1`. The `.env` copy is gitignored.

- [ ] **Step 2: Hit the endpoint directly**

```bash
H='-H Content-Type:application/json -H x-api-key:local'
curl -s -X POST localhost:8090/alerts/query/count $H -d '{"cel":""}'
curl -s -X POST localhost:8090/alerts/query/count $H -d '{"cel":"","group_by":"incident"}'
curl -s -X POST localhost:8090/alerts/query/count $H -d '{"cel":"","group_by":"incident","incident_status":"firing"}'
curl -s -X POST localhost:8090/alerts/query/count $H -d '{"cel":"","group_by":"incident","incident_status":"acknowledged"}'
curl -s -X POST localhost:8090/alerts/query/count $H -d '{"cel":"","group_by":"service"}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8090/alerts/query/count $H -d '{"cel":"","group_by":"severity"}'
```
Expected: each of the first five prints a bare integer; the last prints `422`.

- [ ] **Step 3: Cross-check the incident numbers against the database**

```bash
docker exec tenant-columns-repro-postgres-1 psql -U keep -d keep -t -c "
SELECT s.mode, count(DISTINCT i.id) FROM (VALUES ('active','firing'),('active','acknowledged'),('firing','firing'),('acknowledged','acknowledged')) AS s(mode, status)
JOIN lastalerttoincident lai ON true
JOIN lastalert la ON la.tenant_id = lai.tenant_id AND la.fingerprint = lai.fingerprint
JOIN incident i ON i.tenant_id = lai.tenant_id AND i.id = lai.incident_id
LEFT JOIN incidentenrichment ie ON ie.tenant_id = i.tenant_id AND ie.incident_id = i.id
WHERE la.tenant_id = 'keep' AND lai.deleted_at = '1000-01-01 00:00:00' AND COALESCE(ie.enrichments->>'status', i.status) = s.status
GROUP BY s.mode;"
```
Expected: the `active`, `firing` and `acknowledged` rows equal the three `group_by=incident` responses from Step 2. If they differ, first check whether more than 50000 alerts exist (the endpoint only looks at the latest `ALERTS_HARD_LIMIT` alerts); otherwise this is a bug in Task 2: stop and report the two numbers and the incident ids that differ.

- [ ] **Step 4: Start the worktree UI on :3002 pointing at the worktree gateway**

```bash
lsof -iTCP:3002 -sTCP:LISTEN
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui
cp -n /Users/yarin/keep-namespace/keep-ui/.env.local .env.local
npm run build-monaco-workers
AUTH_TYPE=noauth NEXTAUTH_URL=http://localhost:3002 API_URL=http://localhost:8090 API_URL_CLIENT=http://localhost:8090 nohup npx next dev --turbopack -p 3002 > $SCRATCH/ui-3002.out.log 2>&1 &
echo $! > $SCRATCH/ui-3002.pid
```
The first Turbopack compile takes 30-60 s; wait until `curl -s -o /dev/null -w '%{http_code}' localhost:3002` answers 200 or 307. The `.env.local` copy is gitignored.

- [ ] **Step 5: Walk the feature in the browser (Playwright MCP) and screenshot each state**

On `http://localhost:3002/dashboard` create the scratch dashboard `count-by-verify`, then:
1. Add widget → Preset widget → pick a preset that matches alerts → Panel Type "Alert Count Panel" → Count "Incidents" (status "Active"). Expect the tile to show the Step 3 `active` number with the caption "Active incidents" and a dot in the threshold colour. Edit the widget to "Firing only" and then "Acknowledged only" and confirm the numbers match Step 3 and the captions read "Firing incidents" / "Acknowledged incidents".
2. Edit → Count "Other field" → Service: the tile matches the Step 2 `group_by=service` number with the caption "Services" (or "Service" at 1).
3. Add an Alert Table widget on the same preset with Count "Incidents": the header shows a line "Active incidents: N" above "Alerts count: showing …", the alerts line is grey, the table rows are still alerts.
4. Reload the page: both widgets keep their settings. Edit the counter back to "Alerts": the tile loses its caption and shows the plain alert count, and `curl -s localhost:8090/dashboard -H x-api-key:local` shows that widget without a `countBy` key.
5. Error state: with Playwright route interception abort `**/alerts/query/count` requests whose body contains `group_by`; reload. Expected: the grouped tile is grey with `—` and the caption "Couldn't load count", the table widget line shows `—`; neither shows `0`. Remove the interception.
6. Compact size: resize a counter tile to its minimum height and set it to "Acknowledged only" (longest caption). Expected: the caption truncates with an ellipsis and nothing overflows the tile.
7. Compare each screenshot with `2026-10-08-alert-count-group-by-mock.html` (form states, tile states, table header) and note any visual differences.

- [ ] **Step 6: Clean up**

Delete only the `count-by-verify` dashboard from the UI. Stop only the two processes started here:

```bash
kill $(cat $SCRATCH/gw-8090.pid) $(cat $SCRATCH/ui-3002.pid)
lsof -iTCP:8090 -sTCP:LISTEN; lsof -iTCP:3002 -sTCP:LISTEN
```
The sandbox can swallow `kill` silently, so both `lsof` commands must print nothing; if a listener remains, find its PID with `lsof -tiTCP:<port> -sTCP:LISTEN` and kill that PID (only if its working directory is one of the two worktrees). Report: the Step 2 and Step 3 numbers side by side, the screenshots, and every difference from the mock.

### Task 13: Close-out

**Files:**
- Modify: `docs/superpowers/specs/2026-10-08-alert-count-group-by-design.md` (gateway worktree)

- [ ] **Step 1: Bring the spec in line with what was built and verified**

In section "8. Failure behaviour", replace

```
**Option B: Grouped requests surface the error in the UI.** The hook exposes an error flag and returns no number when a grouped request fails; the tile shows `—` / "Couldn't load count". Ungrouped callers (including the sidebar counters) keep today's behaviour.
```

with

```
**Option B: Grouped requests surface the error in the UI.** The hook exposes an `isError` flag when a grouped request fails (`totalCount` stays `0`); the panels render `—` / "Couldn't load count" instead of the number. Ungrouped callers (including the sidebar counters) keep today's behaviour.
```

In the Summary, replace the whole "**Verify before implementation:**" bullet and its five numbered items with:

```
- **Resolved during planning:** the Tremor control question (a small custom segmented control was built instead of `TabList`); the shared-join defects (reproduced on the SQLite test DB against `origin/dev`: it attaches resolved incidents on a shared fingerprint, soft-deleted links and the wrong status, and misses acknowledged and overridden incidents); the allowlisted field expressions and the override-aware status expression (printed for SQLite, PostgreSQL and MySQL; the queries themselves ran on SQLite only).
- **Still to verify:** (1) the grouped query on PostgreSQL and MySQL, not only SQLite; (2) `EXPLAIN` the grouped query on a large tenant, where any needed index would be a `keep-migrations` PR; (3) `COUNT(DISTINCT incident.id)` on MySQL's UUID column type.
```

- [ ] **Step 2: Confirm both branches are clean, local and unpushed**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway && git status --short && git log --oneline origin/dev..HEAD && git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>&1 | head -1
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-ui && git status --short && git log --oneline origin/dev..HEAD && git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>&1 | head -1
```
Expected: clean trees; the gateway branch shows the spec/plan commits plus Tasks 1-4; the UI branch shows Tasks 5-10 (and any gate fixes); the upstream query reports that no upstream is configured. Nothing is pushed.

- [ ] **Step 3: Commit the spec update**

```bash
cd /Users/yarin/keep-namespace/.worktrees/count-group-by/keep-api-gateway
git add docs/superpowers/specs/2026-10-08-alert-count-group-by-design.md
git commit -m "docs: record what the count-by build verified" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Hand off**

Report to the user: both branch names and commit lists, the Task 12 numbers and screenshots, the remaining "still to verify" items, and the merge order (gateway PR first, then keep-ui). Do not push or open PRs; ask whether they want that.

- [ ] **Step 5: Stop the design companion server if it is still running**

```bash
bash /Users/yarin/.claude/plugins/cache/claude-plugins-official/superpowers/6.4.1/skills/brainstorming/scripts/stop-server.sh /Users/yarin/keep-namespace/.superpowers/brainstorm/20006-1791465603
```
