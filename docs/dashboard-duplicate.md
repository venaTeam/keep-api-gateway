# Duplicate a dashboard

A user can copy an existing dashboard into a new one and change the copy freely.
The copy has the same widgets, layout and appearance, and gets its **own** copy
of every uploaded image. It came from a user feature request: start new dashboards
from an existing one instead of a blank page.

It ships in two repos, which must merge together, **gateway first**:

| Repo | What changed |
|---|---|
| `keep-api-gateway` | new endpoint `POST /dashboard/{dashboard_id}/duplicate` |
| `keep-ui` | copy icon on hover in the sidebar's Dashboards list, and the duplicate dialog |

There is no schema change, no `keep-migrations` change, and no new environment
variable or feature flag.

Related documents in this folder:

- Spec: [`superpowers/specs/2026-11-16-dashboard-duplicate-design.md`](superpowers/specs/2026-11-16-dashboard-duplicate-design.md) (the binding requirements)
- Plan: [`superpowers/plans/2026-11-16-dashboard-duplicate.md`](superpowers/plans/2026-11-16-dashboard-duplicate.md) (how it was split into tasks)

## How it works for the user

1. Hover a dashboard in the sidebar. A gray copy icon appears next to the trash icon.
   It turns orange under the pointer, with a "Duplicate" tooltip. Keyboard focus on the
   row shows it too. The tooltip opens on hover or keyboard focus. It does not open
   when focus returns to the button after the dialog was closed with the mouse.
2. Click it. The dialog "Duplicate dashboard" suggests `<name> (copy)`, or
   `(copy 2)`, `(copy 3)`… when that name is taken.
3. Press Duplicate or Enter. The copy is saved, the sidebar lists it, and the user
   lands on it in view mode with a "Dashboard duplicated" toast.

Nothing is added to the dashboard page itself. Duplicating is an occasional
action, so it deliberately adds no visual weight to the screen teams keep open.

The dialog's rules:

- **Name.** It is checked as you type. The Duplicate button is disabled while the
  trimmed name is empty, or matches an existing dashboard name (case ignored).
- **Unsaved edits.** Only the **saved** version is copied. If the dashboard being
  copied is open with unsaved edits, the dialog says "Unsaved changes aren't
  included in the copy."
- **Leaving with unsaved edits.** If the user is editing a dashboard with unsaved
  changes, the existing unsaved-changes prompt runs before navigating. If they stay,
  the copy still exists in the sidebar.
- **Errors.**
  - 409 (the name was taken meanwhile): inline "A dashboard with this name already
    exists", and the dialog stays open. The dashboard list refreshes, so from then on
    the name check catches that name as you type.
  - 404 (the source was deleted meanwhile): inline "This dashboard no longer exists",
    and the list refreshes. Duplicate stays disabled and typing does not clear the
    message, because every retry would get the same 404.
  - Anything else: an error toast.
- **Cancel while saving.** If the dialog is closed before the request finishes, a
  late success only shows the toast. It neither navigates nor closes another
  dialog, and a late error is ignored.

## API

```
POST /dashboard/{dashboard_id}/duplicate
Scope: write:dashboards
Body:  { "dashboard_name": "NOC Overview (copy)" }
```

| Status | When | Body |
|---|---|---|
| 200 | Copy created | `DashboardResponseDTO`, same as `POST /dashboard` |
| 400 | The source references an image it doesn't own, including a still-pending upload (inconsistent data) | `{"message": "Dashboard references missing or unavailable images", "invalid_image_ids": [...]}` |
| 403 | Missing `write:dashboards`, or read-only mode | standard |
| 404 | Source missing, in another tenant, or private and not created by the caller | `{"detail": "Dashboard not found"}` |
| 409 | `dashboard_name` already exists in the tenant, **ignoring case** | `{"detail": "A dashboard with this name already exists"}` |
| 422 | `dashboard_name` missing or empty after trimming | standard validation error |

The client sends only the name. The server copies the **saved** config, so a stale
or tampered client copy cannot get in. The name is trimmed.

Name clashes are checked ignoring case, so API callers get the same rule as the UI.
"noc overview" is rejected when "NOC Overview" exists, whatever its privacy. This
check is on the duplicate endpoint only. `POST /dashboard` and renames are still
case-sensitive (see Follow-ups). The database's unique constraint
(`unique_dashboard_name_per_tenant`, exact case) still catches a race between two
requests with the same exact name.

The copy's metadata:

- `created_by` and `updated_by`: the caller
- `created_at` and `updated_at`: now
- `is_private`: taken from the source, so a copy is never more visible than its original

On success, `duplicate_dashboard` logs `Duplicated dashboard` at INFO after the
commit, with `tenant_id`, `source_dashboard_id`, `new_dashboard_id` and
`copied_images` (the number of image rows it copied). The route does not parse the
config.

## Uploaded images

Every `dashboardimage` row belongs to exactly one dashboard. A save that
references an image owned by another dashboard is rejected, and deleting a
dashboard deletes its images. A copy therefore cannot reuse the source's image
ids. Copying the dashboard in the browser alone would fail with a 400.

The gateway does the whole copy in **one transaction** (`duplicate_dashboard` in
`src/repositories/db.py`):

```mermaid
sequenceDiagram
    participant UI as keep-ui
    participant GW as keep-api-gateway
    participant DB as Database
    UI->>GW: POST /dashboard/{id}/duplicate {dashboard_name}
    GW->>DB: SELECT source ... FOR UPDATE (tenant + visibility)
    GW->>DB: are all images owned by the source? (no writes)
    GW->>DB: name taken, ignoring case? (no writes)
    GW->>DB: INSERT dashboard (config with new imageIds), flush
    GW->>DB: INSERT dashboardimage ... SELECT (one per uploaded image)
    GW->>DB: COMMIT
    GW-->>UI: 200 DashboardResponseDTO
```

Step by step:

- **Parse.** `get_dashboards` already handles a config stored as a JSON string; the
  same parsing is applied here.
- **Rewrite ids.** `rewrite_image_ids` (in `src/repositories/dashboard_images.py`)
  deep-copies the config and swaps each upload IMAGE widget's `imageId` for a new id.
  URL images, HTML, Grafana, preset and every other widget are copied as they are.
- **Copy rows.** `copy_dashboard_images` inserts one new row per image with
  `INSERT … SELECT`, filtered on `tenant_id`, the old `id` **and**
  `dashboard_id = source`. The bytes never enter Python or the browser. The new rows
  belong to the new dashboard from the start; they are never pending. So the
  20-per-tenant pending-upload cap does not apply.
- **Lock.** The source row is read with `SELECT … FOR UPDATE`. On Postgres, a
  concurrent delete or save of the source has to lock that same row before it can
  commit. So:
  - It either finishes first, in which case the source is gone (404) or the newer
    saved version is copied.
  - Or it waits until the copy commits. Image rows it deleted but has not committed
    are still visible to the copy.

  Without the lock, a delete landing between the read and the image copy gave a
  misleading 400 instead of 404. SQLite ignores `FOR UPDATE`. On MySQL (InnoDB) the
  same race could end in a deadlock that the database resolves by failing one
  request. Prod runs Postgres.
- **Checks before any write.** Two checks run before anything is written, in this
  order:
  - `check_dashboard_image_references(..., allow_pending=False)`, which rejects any
    image the source does not own, including a still-pending upload. The save path
    keeps the default `allow_pending=True`.
  - the case-insensitive name check.

  The copy's own row-count guard stays as a backstop.
- **Failures.**
  - A name clash is a 409. That is either the case-insensitive check, or the
    `unique_dashboard_name_per_tenant` constraint on flush, recognised by name; SQLite
    reports it as `dashboard.dashboard_name`.
  - Any other integrity error on flush is logged and re-raised, so it surfaces as a
    500, not as a misleading "name taken".
  - An image the source does not own is a 400.

  In every case nothing is committed, and the session closes and rolls back.
- **Storage.** A copy stores the image bytes again, at most
  `KEEP_DASHBOARD_IMAGE_MAX_BYTES` (5 MB) per image. No per-tenant total cap was
  added, in line with the image widget decision of 2026-09-24.

## UI pieces

| File | Role |
|---|---|
| `src/features/dashboard-duplicate/lib/copy-name.ts` | `defaultCopyName`, `isDashboardNameTaken` (trim, case-insensitive) |
| `src/features/dashboard-duplicate/model/useDuplicateDashboard.ts` | Calls the endpoint, appends the copy to the SWR `/dashboard` list, records metrics |
| `src/features/dashboard-duplicate/ui/DuplicateDashboardModal.tsx` | The dialog |
| `src/components/LinkWithIcon.tsx` | Optional `onDuplicate` / `duplicateAriaLabel` / `duplicateDataCy`. Links that don't pass them render exactly as before. |
| `src/components/navbar/DashboardLink.tsx`, `DashboardLinks.tsx` | Wire the icon; hold the clicked dashboard as a snapshot in state |
| `src/metrics/labels.ts` | New action label `duplicate_dashboard` (latency on success, status code on error) |

New `data-cy` selectors:

- `nav-link-dashboard-duplicate-<id>`
- `dashboard-duplicate-modal`
- `dashboard-duplicate-name-input`
- `dashboard-duplicate-name-error`
- `dashboard-duplicate-unsaved-note`
- `dashboard-duplicate-cancel-btn`
- `dashboard-duplicate-submit-btn`

The generated inventory, `docs/data-cy-selectors.md` in keep-ui, was not regenerated
(see decisions).

## Deploying

- Merge and deploy the **gateway first**. A UI talking to an older gateway gets a 404
  from the missing route, and the dialog shows "This dashboard no longer exists".
- No migration, no env var, no flag.
- The metric label must ship with the UI. The metrics ingestion routes reject
  unknown labels.

## Tests

- **Gateway:** `tests/test_dashboard_duplicate.py` covers:
  - the helper functions
  - every status code
  - the image copy and its ownership
  - deleting the source keeps the copy's images
  - the JSON-string config and the empty config
  - name trimming
  - caller metadata
  - a no-commit-on-failure test

  Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_dashboard_duplicate.py -v`

  After the PR-review fixes (2026-11-17):
  - The three dashboard test files together (`test_dashboard_duplicate.py`,
    `test_dashboard_images.py`, `test_dashboard.py`) give 83 passed.
  - The full keep-ui unit suite gives 1211 passed in 100 suites.
  - `tsc` stays at the 2 errors `dev` already had.
  - `npm run lint` fails with the same 16 errors in 7 untouched files on `dev`
    (83316e34) as on this branch; the branch adds none.
- **UI:** run `npx jest src/features/dashboard-duplicate src/components`. It covers:
  - `src/features/dashboard-duplicate/**/__tests__` (naming rules, hook, dialog)
  - `src/components/__tests__/LinkWithIcon.test.tsx`
  - `src/components/navbar/__tests__/DashboardLinks.test.tsx`
- **Postgres:** the gateway test fixture runs on SQLite, which cannot observe a
  rollback. The endpoint was also run against a real Postgres on 2026-11-16. That run
  confirmed byte-identical image copies, 409 writing nothing, 404, 422, the trimmed
  name, deleting the source keeping the copy's image, and the log line.

  After the PR-review fixes it was run again on Postgres (2026-11-17):
  - **Concurrent delete.** A transaction deleted the source and held its row for 6 s.
    The duplicate blocked about 5 s, then returned 404, leaving no copy and no orphan
    image rows.
  - **Concurrent save.** A save held the row; the duplicate blocked about 4 s, then
    returned 200 with its own image.
  - **Name case.** "duplicate demo" and "  DUPLICATE DEMO (COPY) " returned 409.
  - **Pending image.** A source referencing a pending image returned 400 with no new
    row, and the image stayed pending.
  - **Log.** There is exactly one `Duplicated dashboard` record per copy, from
    `src.repositories.db`.
- **Browser, after the PR-review fixes:**
  - A mouse Cancel or a mouse-click Duplicate leaves no tooltip; Escape shows it.
  - A 404 keeps Duplicate disabled while typing.
  - A 409 refreshes the list, so retyping the name hits the local clash message.
- **Browser:** the full flow was checked in a browser on a local stack (8/8):
  - hover icon and tooltip, and no header button
  - dialog copy and focus
  - case-insensitive name clash and blank name
  - Enter, the copy page, the toast and the copied image
  - `(copy 2)`
  - the unsaved-changes note
  - deleting the original keeps the copy's image

## Decisions made during the build

These were made during implementation. Each line says what it costs if it turns out
wrong.

1. **Pseudo-comments.** Plan test skeletons contained pseudo-comments; implementers
   wrote real assertions and kept code comment-free. *If wrong:* cosmetic.
2. **Type-error baseline.** keep-ui at `fbb5610f` already has 2 `tsc` errors. Every UI
   task kept the count at 2. *If wrong:* a pre-existing error could be misattributed.
3. **No e2e-suite case.** The e2e-suite case for the sanity suite was replaced by the
   live browser check above. The sanity suite needs a full deploy. An e2e case in
   `e2e/specs/10-create-dashboard.spec.ts` is a follow-up. *If wrong:* one e2e case is
   missing.
4. **isort profile.** `isort --check-only --profile black` is the import check for the
   gateway files, because the repo formats with black and has no isort config.
   *If wrong:* import-order nits.
5. **Extra copy tests.** Two extra tests pin `copy_dashboard_images` rejecting another
   tenant's image and a pending image. *If wrong:* two small tests.
6. **Parallel tasks.** The gateway endpoint and the UI helpers were built in parallel,
   because the contract was fixed in the plan. *If wrong:* UI rework if the contract
   drifted (reviewers checked it did not).
7. **Extra pre-check.** `duplicate_dashboard` also runs
   `check_dashboard_image_references` before writing anything, mirroring the create
   path. Since the PR review it passes `allow_pending=False`. *If wrong:* one
   redundant ownership read.
8. **Late test fixes.** Test-only gaps from task reviews were fixed in the final fix
   wave: pinning `created_by` to the caller, and a no-commit-on-failure test.
   *If wrong:* the tests were unpinned until then.
9. **Late UI fixes.** UI edge cases (a late result after Cancel, error announced to
   screen readers) were fixed in the final fix wave. *If wrong:* a rare surprise
   navigation after cancel.
10. **Keyboard reveal.** The copy icon appears on hover or **keyboard** focus
    (`group-has-[:focus-visible]`), not on any focus. The shared trash icon is
    unchanged. *If wrong:* keyboard users don't see the trash icon on focus, which is
    pre-existing.
11. **Deferred polish.** Some minor polish is deferred; see Follow-ups. *If wrong:*
    polish left for later.
12. **Selector inventory.** `docs/data-cy-selectors.md` and
    `scripts/data-cy/manifest.json` in keep-ui were not regenerated. Dev's manifest
    already fails `npm run audit:data-cy -- --check` before this branch, and no CI job
    enforces it, so regenerating would pull unrelated selectors into this change.
    *If wrong:* the QA inventory lags until someone regenerates it.

## Changes from PR review (2026-11-17)

Review of gw#126 and ui#112 by a teammate. Each item was checked against the code
before acting.

| # | Comment | Outcome |
|---|---|---|
| G1 | Every `IntegrityError` on flush became 409, unlogged | Fixed. Only the name constraint maps to 409; anything else is logged and re-raised. |
| G2 | A concurrent delete or save of the source gave a misleading 400 | Fixed. The source row is locked (`FOR UPDATE`) for the whole copy. |
| G3 | The pre-check accepted pending images that the copy then rejected after a write | Fixed. `allow_pending=False` on this path, so ownership failures are rejected before any write. |
| G4 | Name clash was case-sensitive on the server | **Product decision (2026-11-17):** fixed in the duplicate endpoint now. Case-insensitive check before insert; `POST /dashboard` is unchanged (follow-up). |
| G5 | The route re-parsed the config after commit just to log | Fixed. The log moved into `duplicate_dashboard`, using the copied-image count. |
| U1 | `await mutate(..., {revalidate: true})` was said to wait for a full GET | **Not changed.** In the installed swr 2.3.8 the revalidation is started without being awaited (`dist/_internal/config-context-12s-Dh3trQsc.mjs:366`). The `await` returns after the cache write, so the toast and navigation don't wait for the GET. A test with real SWR pins this, and it fails if the hook ever awaits the refetch. |
| U2 | Server errors didn't feed into the submit state | Fixed. A 404 keeps Duplicate disabled for good; a 409 refreshes the list. |
| U3 | The tooltip reopened when focus returned to the button after the dialog closed | Fixed. The tooltip is blocked on focus that isn't `:focus-visible`. Hover and keyboard focus still open it. |

## Follow-ups (not blocking)

- An e2e-suite case for duplicating, in `e2e/specs/10-create-dashboard.spec.ts`.
- `POST /dashboard` and dashboard renames still treat names as case-sensitive. Only
  the duplicate endpoint ignores case. Make them match, ideally with a
  case-insensitive unique index, which also closes the race below.
- Two concurrent duplicates named "X" and "x" can both succeed: the case-insensitive
  check runs before insert, and the unique constraint is exact-case.
- When a source has both invalid images and a taken name, the response is 400, not
  409, because the image check runs first. The UI shows it as an error toast.
- The source-row lock has no `lock_timeout`. A duplicate queued behind an unusually
  slow save or delete of the same dashboard waits for it to finish.
- `duplicateAriaLabel` is optional on `LinkWithIcon`. A future caller passing
  `onDuplicate` alone would render an unlabelled button.
- Closing the dialog with Escape or Enter returns keyboard focus to the copy button,
  so the tooltip shows. This is intended for keyboard focus.
- `matches(":focus-visible")` is not guarded. Browsers without `:focus-visible`
  (Chrome before 86, Safari before 15.4) would only lose the tooltip on focus.
- A 404 that arrives after the dialog was cancelled is ignored, so the sidebar keeps
  showing the deleted source until the list revalidates.
- The test-local transaction helper in `tests/test_dashboard_duplicate.py` works around
  the `conftest.py` SQLite bug below. If that bug is fixed, drop the helper; it will fail
  loudly ("cannot start a transaction within a transaction") rather than silently.
- The `nav-link-dashboard-duplicate-<id>` selector shares the `nav-link-dashboard-`
  prefix, so prefix locators must also filter by text.
- `tests/conftest.py` runs SQLite in autocommit (its `do_begin` hook swallows an
  error), so no gateway test can observe a rollback. This is suite-wide and
  pre-existing.
- `GET /dashboard` doesn't pass the caller's email, so a user's own private
  dashboards aren't listed. This is pre-existing. A copy keeps `is_private`, but no
  route sets it today.
- `POST /dashboard` returns 500, not 409, on a duplicate name. This is pre-existing
  and needs a separate small fix.
- Copying a dashboard into another tenant or org (export/import) was decided out of
  scope.
