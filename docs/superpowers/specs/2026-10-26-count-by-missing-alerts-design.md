# Count-by: show the alerts the count leaves out

Follow-up to `2026-10-08-alert-count-group-by-design.md`. Approved in conversation on 2026-10-26.

## Why

When a preset widget counts distinct incidents, alerts that are in no incident disappear from the number. A preset with 100 alerts, 90 of them in 10 incidents, shows "10 active incidents", and the 10 alerts outside any incident are invisible. The same happens for the other fields: alerts with no service never show up in a "Services" count. Users want to see that remainder next to the number, and choose per widget whether they do.

## Requirements

1. **Toggle, per widget.** The create/edit form of both preset panel types (Alert Count Panel, Alert Table) gets a switch when Count is Incidents or Other field. The label follows the field: "Show alerts not in an incident", "Show alerts with no service", "with no host", and so on. It is off by default, so existing widgets don't change. It is hidden when Count is Alerts. Switching Count back to Alerts drops it along with the rest of the count setting.
2. **What is counted.** The number of alerts the widget's own query matches that have no value for the counted thing:
   - Incidents: alerts with no live link to an incident the Incidents page shows (visible, not a candidate), in **any** status. An alert whose only incident is resolved is in an incident, so it is not counted here, and it is not in the "active incidents" number either. That is intended. A deleted incident is the exception: deleting only sets its status to `deleted` and keeps its links, so an alert whose only incident was deleted counts as having no incident (merging already removes the links, so merged incidents behave the same way).
   - Other fields: alerts whose field is empty (`NULL` or `''`), the same rule the distinct count uses to skip blanks.
   - Same matching alerts as the main number: the preset filter, the dashboard time range, firing-only for field counts when the widget has it on, and the same `ALERTS_HARD_LIMIT` window. Alerts are counted the way the plain alert count counts them (one per fingerprint).
3. **Count Panel tile.** A footer strip across the bottom of the tile: "+ 10 alerts not in an incident", "+ 1 alert with no service". Always neutral grey; thresholds and the tile colour follow the main number only. At zero it reads "Every alert is in an incident" / "Every alert has a service". It is not clickable on its own; clicking anywhere on the tile still opens the preset.
4. **Alert Table.** The same text as its own grey line under the group-count line. The rows don't change: they stay the preset's latest N alerts, with or without an incident.
5. **Failures.** The two numbers come from one request. If it fails, the tile shows the existing error state ("—", "Couldn't load count") and no strip. If the gateway answers the grouped count but without the left-out number (a gateway without this change), the main number shows and the strip shows "—".
6. **Height.** A count tile with the strip needs one more grid row (30 px) than a grouped tile without it: 5 rows instead of 4, applied the same way the grouped minimum is applied today (on create, and on edit when the saved height is smaller).

## Contract

`POST /alerts/query/count` gets an optional `include_missing: bool` (default `false`), valid only with `group_by`; with `group_by` absent it is a 422 like `incident_status` is.

- `include_missing` false or absent: unchanged, `{"count": n, "group_by": "<field>"}`.
- `include_missing: true`: `{"count": n, "group_by": "<field>", "missing": m}`.
- Ungrouped requests: unchanged bare integer.

A gateway without this change drops the unknown key (Pydantic v1 ignores extras) and returns `{count, group_by}`, so the UI can tell: no `missing` number means "not available", never 0.

## Not in this change

- Clicking the strip to list those alerts. The alerts page's incident filter only attaches firing incidents and ignores removed links, so a list opened from the strip would not match the number. It comes later, together with fixing that shared join.
- Any change to the main number, the table rows, or the alerts page.

## Builder's calls

- Names: request `include_missing`, response `missing`, widget config `countBy.showMissing`.
- The toggle lives in the Count control, so both panel types get it from one place; changing the incident status or the field keeps the toggle's value.
- The missing count is a separate `SELECT count(...)` built from the same filtering builder with a `NOT EXISTS` on live visible non-candidate links (incidents) or `NULLIF(field, '') IS NULL` (fields). Cost: one more count over the same window; no migrations.

## Testing

- Gateway: model validation, route shapes (with/without `include_missing`, ungrouped unchanged), and repository counts on the shared graph fixture: alerts with no incident, only-resolved incident, hidden/candidate incident, removed link, empty and blank field values, other tenant, and a CEL filter that references `incident.`.
- UI: Jest for the label helpers, the hook (requests `include_missing` only when on; `missing` absent → unavailable), the Count control toggle, the tile strip in every state, the table line, and the form's height rule.
- Local stack in a browser: a dashboard with every case, the numbers cross-checked against SQL, and the strip against a gateway built from `dev` without this change.

### What ran (2026-10-26/27)

- Gateway suite: 895 passed, 5 skipped, 0 failed (the two Elasticsearch test files need Docker Compose and were run separately: they fail the same way on `dev` without Docker). Ruff: nothing new.
- keep-ui: Jest 85 suites / 924 tests passed; typecheck only the 2 pre-existing errors; ESLint nothing new.
- Live stack on PostgreSQL 16 with the schema from keep-migrations `dev` and a namespaced dataset (27 alerts, 8 incidents, including a deleted, a merged, a hidden and a candidate incident, a removed link, resolved-only, blank and whitespace values): 240 API checks comparing the design, independent SQL and the live endpoint, response shapes included, 0 mismatches. A gateway built from `dev` answers the same requests with `{count, group_by}` and no `missing`.
- Browser, 43 checks, all passing: every tile, strip and table line against the expected numbers; with the grouped requests answered by the `dev` gateway every strip and line shows "—" and the main numbers stay; the form switch labels and states; a new tile created with the switch on is 5 rows; turning it off lowers the minimum to 4 and keeps the height; clicking the strip opens the preset like the rest of the tile.
