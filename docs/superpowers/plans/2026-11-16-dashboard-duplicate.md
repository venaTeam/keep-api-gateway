# Duplicate a Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user duplicate a dashboard from a sidebar hover icon, through a name dialog, into an independent copy that includes its own copies of uploaded images.

**Architecture:** A new gateway endpoint, `POST /dashboard/{id}/duplicate`, reads the saved source and writes the new dashboard plus copies of its `dashboardimage` rows in one transaction. Image rows are copied with `INSERT … SELECT` under new ids, and `imageId` is rewritten in the copied config. keep-ui adds a copy icon to sidebar dashboard entries, which opens a feature-module dialog. The dialog calls the endpoint, updates the SWR list and navigates to the copy.

**Tech Stack:** FastAPI + SQLModel/SQLAlchemy 2.0 + Pydantic v1 (gateway); Next.js 15 + React 19 + Tremor 3 + SWR + Jest/RTL (keep-ui).

**Spec:** `docs/superpowers/specs/2026-11-16-dashboard-duplicate-design.md` (this repo). The workspace copy is `/Users/yarin/keep-namespace/spec-dashboard-duplicate.md`.

**Worktrees (local branch `feat/dashboard-duplicate`, off origin/dev, no upstream):**
- Gateway: `/Users/yarin/keep-namespace/.worktrees/dashboard-duplicate/keep-api-gateway` (`.venv` is a symlink to the main checkout's venv)
- UI: `/Users/yarin/keep-namespace/.worktrees/dashboard-duplicate/keep-ui` (`node_modules` is an APFS clone)

## Global Constraints

- **Git:** never push, never set an upstream, never touch `dev`/`main`. Commit locally on `feat/dashboard-duplicate` only.
- **Commit messages:** no URLs. End with exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Comments:** no inline explanatory comments. Docstrings and JSDoc only.
- **Gateway tests:** run from the gateway worktree with `PYTHONPATH=. .venv/bin/python -m pytest …`.
- **Gateway lint:** check only the files you touched (`ruff check`, `black --check`, `isort --check-only`). Never run black or isort over `src/`.
- **UI checks:** run from the UI worktree.
  - `npx jest <paths>`
  - `npx tsc --noEmit`: dev already has 2 type errors; you may not add any.
  - `npx eslint <touched files>`
- **Copy:** these strings are verbatim from the spec.
  - Dialog title: `Duplicate dashboard`
  - Subtitle: `Make a copy of <name>.`
  - Info box: `Widgets, layout, appearance and uploaded images are copied. The original dashboard isn't changed.`
  - Unsaved note: `Unsaved changes aren't included in the copy.`
  - Name clash: `A dashboard with this name already exists`
  - Source gone: `This dashboard no longer exists`
  - Success toast: `Dashboard duplicated`
  - Tooltip: `Duplicate`
- **Default name:** `<source> (copy)`, then `<source> (copy 2)`, `(copy 3)` and so on. Name comparison trims and ignores case.
- **Endpoint:**
  - Route: `POST /dashboard/{dashboard_id}/duplicate`, scope `write:dashboards`, body `{"dashboard_name": str}`.
  - 200 returns the `DashboardResponseDTO`.
  - 404 returns `{"detail": "Dashboard not found"}`.
  - 409 returns `{"detail": "A dashboard with this name already exists"}`.
  - 422 when the name is empty after trimming.
  - 400 returns the existing invalid-images body.
- **Metric label:** `duplicate_dashboard`.

## Review Focus

1. The source `dashboard_config` is stored as a JSON **string**, which Postgres returns on some paths. The copy must still come back as a dict with `imageId`s rewritten. Test in Task 2.
2. The source config is `{}`, or has no `widget_data`/`layout`, like a provisioned or empty dashboard. Duplicating returns 200 with an equal config. Test in Task 2.
3. The name has leading or trailing spaces. Both the server and the client trim it, and a name of only spaces is rejected (server 422, client button disabled). Tests in Tasks 2 and 3.
4. The name differs from an existing one only by case, e.g. `ops` when `Ops` exists. The client blocks it, even though Postgres's unique constraint is case-sensitive. Tests in Tasks 3 and 4.
5. The name contains URL-special characters (`/`, `%`, `#`). Navigation uses `encodeURIComponent`, so the user lands on the copy. Test in Task 4.

---

### Task 1: Gateway image-copy helpers

**Files:**
- Modify: `src/repositories/dashboard_images.py`
- Test: `tests/test_dashboard_duplicate.py` (new)

**Interfaces:**
- Consumes: `referenced_image_ids(dashboard_config) -> set[str]`, `DashboardImageReferenceError(invalid_image_ids)`, `IMAGE_WIDGET_TYPE`, `UPLOAD_SOURCE` (existing, same module).
- Produces:
  - `rewrite_image_ids(dashboard_config: dict | None, id_map: dict[str, str]) -> dict`
  - `copy_dashboard_images(session: Session, tenant_id: str, source_dashboard_id: str, new_dashboard_id: str, id_map: dict[str, str], created_by: str | None) -> None`

- [ ] **Step 1: Write the failing tests** in `tests/test_dashboard_duplicate.py`. Reuse the helper style of `tests/test_dashboard_images.py` (`PNG`, `AUTH`, `_upload`, `_image_widget`, `_config`, `_create_dashboard`). Copy the small helpers in rather than importing private ones.

```python
def test_rewrite_image_ids_swaps_only_upload_image_widgets():
    config = {
        "layout": [{"i": "a"}],
        "widget_data": [
            {"widgetType": "IMAGE", "image": {"source": "upload", "imageId": "old", "fit": "contain"}},
            {"widgetType": "IMAGE", "image": {"source": "url", "url": "https://x/y.png", "fit": "cover"}},
            {"widgetType": "HTML", "html": {"html": "<b>hi</b>"}},
        ],
        "appearance": {"density": "compact"},
    }
    result = rewrite_image_ids(config, {"old": "new"})
    assert result["widget_data"][0]["image"]["imageId"] == "new"
    assert result["widget_data"][1] == config["widget_data"][1]
    assert result["widget_data"][2] == config["widget_data"][2]
    assert result["appearance"] == {"density": "compact"}
    assert config["widget_data"][0]["image"]["imageId"] == "old"


def test_rewrite_image_ids_handles_empty_config():
    assert rewrite_image_ids({}, {}) == {}
    assert rewrite_image_ids(None, {}) == {}


def test_copy_dashboard_images_copies_rows_under_new_ids(db_session):
    # seed two dashboards d_src, d_new (Dashboard rows) and an image owned by d_src
    # copy_dashboard_images(db_session, SINGLE_TENANT_UUID, d_src, d_new, {img: "new-id"}, "admin@keephq"); commit
    # assert new row: dashboard_id == d_new, image_blob == PNG, name/content_type/size_bytes equal, created_by == "admin@keephq"
    # assert original row still owned by d_src


def test_copy_dashboard_images_rejects_image_not_owned_by_source(db_session):
    # image owned by a third dashboard -> pytest.raises(DashboardImageReferenceError) with invalid_image_ids == [img]
```

- [ ] **Step 2: Run them to verify they fail.**
  - Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_dashboard_duplicate.py -v`
  - Expected: ImportError on `rewrite_image_ids` / `copy_dashboard_images`.

- [ ] **Step 3: Implement both helpers in `src/repositories/dashboard_images.py`.**
  - `rewrite_image_ids` returns a `copy.deepcopy` of the config (or `{}` for None). In it, it replaces `image.imageId` for widgets where `widgetType == IMAGE_WIDGET_TYPE` and `image.source == UPLOAD_SOURCE` and the id is in `id_map`. The input is never mutated.
  - `copy_dashboard_images` runs one `session.execute(insert(DashboardImage).from_select([...], select(...)))` per `id_map` item.
    - The select takes `literal(new_id)`, `tenant_id`, `literal(new_dashboard_id)`, `name`, `content_type`, `size_bytes`, `image_blob`, `literal(created_by)` and `literal(datetime.utcnow())`.
    - It filters `tenant_id ==`, `id == old_id` and `dashboard_id == source_dashboard_id`.
    - Use `literal(datetime.utcnow())`, not `func.now()`, to match the model's naive-UTC default.
    - A `rowcount != 1` raises `DashboardImageReferenceError([old_id])`.
    - Bytes never enter Python.

- [ ] **Step 4: Run the tests to verify they pass.** Same command. Expected: 4 passed.

- [ ] **Step 5: Lint the touched files and commit.**
  - Lint: `.venv/bin/ruff check src/repositories/dashboard_images.py tests/test_dashboard_duplicate.py && .venv/bin/black --check src/repositories/dashboard_images.py tests/test_dashboard_duplicate.py`
  - Commit: `git add src/repositories/dashboard_images.py tests/test_dashboard_duplicate.py && git commit -m "feat(dashboard): add image-copy helpers for dashboard duplication"`

### Task 2: Gateway duplicate repository function and endpoint

**Files:**
- Modify: `src/repositories/db.py` (next to `create_dashboard`, about line 2524; extend the `dashboard_images` import at about line 62)
- Modify: `src/routes/dashboard.py`
- Test: `tests/test_dashboard_duplicate.py`

**Interfaces:**
- Consumes: Task 1's `rewrite_image_ids` and `copy_dashboard_images`; existing `referenced_image_ids` and `_invalid_images_response`.
- Produces:
  - `class DashboardNameTakenError(ValueError)` in `src/repositories/db.py`
  - `duplicate_dashboard(tenant_id: str, dashboard_id: str, dashboard_name: str, created_by: str | None) -> Dashboard | None`
  - Route `POST /dashboard/{dashboard_id}/duplicate`

- [ ] **Step 1: Write the failing endpoint tests.** All tests use `@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)`. The NO_AUTH caller email is `admin@keephq`.

```python
def test_duplicate_copies_config_and_sets_metadata(db_session, client, test_app):
    # source config: layout with one item, widget_data = [preset-like widget, url IMAGE widget, HTML widget], appearance {"backgroundColor": "#f8fafc", "density": "compact"}
    # POST /dashboard/{src_id}/duplicate {"dashboard_name": "d1 (copy)"} -> 200
    # body["dashboard_name"] == "d1 (copy)", body["id"] != src_id, body["dashboard_config"] == source config
    # DB row: created_by == updated_by == "admin@keephq", is_private is False; source row unchanged

def test_duplicate_copies_uploaded_images(db_session, client, test_app):
    # upload PNG, create d1 with _image_widget(img) -> duplicate -> new imageId != img
    # GET /dashboard-images/{new_imageId} -> 200, content == PNG; _owner(new) == copy id; _owner(img) == d1 id

def test_deleting_source_keeps_copy_images(db_session, client, test_app):
    # duplicate, DELETE /dashboard/{d1}, GET copy image -> 200

def test_duplicate_name_taken_is_409_and_writes_nothing(db_session, client, test_app):
    # d1 with an uploaded image; duplicate as "d1" -> 409 {"detail": "A dashboard with this name already exists"}
    # Dashboard count == 1, DashboardImage count == 1

def test_duplicate_unknown_dashboard_is_404(db_session, client, test_app): ...
def test_duplicate_other_tenants_dashboard_is_404(db_session, client, test_app): ...
def test_duplicate_private_dashboard_of_another_user_is_404(db_session, client, test_app):
    # Dashboard(is_private=True, created_by="someone@else") added directly -> 404
def test_duplicate_keeps_is_private_for_callers_own_private_dashboard(db_session, client, test_app):
    # Dashboard(is_private=True, created_by="admin@keephq") -> 200, copy row is_private True

@pytest.mark.parametrize("name", ["", "   "])
def test_duplicate_blank_name_is_422(db_session, client, test_app, name): ...
def test_duplicate_trims_name(db_session, client, test_app):
    # "  d1 (copy)  " -> body["dashboard_name"] == "d1 (copy)"

def test_duplicate_rolls_back_when_source_references_foreign_image(db_session, client, test_app):
    # Dashboard row added directly whose config references an image owned by another dashboard
    # -> 400 {"message": "Dashboard references missing or unavailable images", "invalid_image_ids": [img]}
    # no new Dashboard row, DashboardImage count unchanged

def test_duplicate_source_config_stored_as_json_string(db_session, client, test_app):
    # Dashboard(dashboard_config=json.dumps(_config(_image_widget(img))) ...) owning img -> 200, body config is a dict, imageId rewritten

def test_duplicate_empty_config(db_session, client, test_app):
    # Dashboard(dashboard_config={}) -> 200, body["dashboard_config"] == {}
```

- [ ] **Step 2: Run them to verify they fail.**
  - Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_dashboard_duplicate.py -v`
  - Expected: the new tests fail with 404/405 (no route). Task 1's tests still pass.

- [ ] **Step 3: Implement `duplicate_dashboard` in `src/repositories/db.py`.**
  It does all of this in one `with Session(engine) as session:`:
  1. Select the source by `tenant_id`, `id` and `or_(Dashboard.is_private == False, Dashboard.created_by == created_by)`. If none is found, return `None`.
  2. If the config is a `str`, `json.loads` it.
  3. Build `id_map = {old: str(uuid4()) for old in referenced_image_ids(config)}`.
  4. Create `Dashboard(tenant_id, dashboard_name, dashboard_config=rewrite_image_ids(config, id_map), created_by, updated_by=created_by, is_private=source.is_private)`.
  5. Call `session.add` and then `session.flush()`. On `IntegrityError`: `session.rollback()` and raise `DashboardNameTakenError(dashboard_name)`.
  6. Call `copy_dashboard_images(...)`.
  7. Call `commit()`, then `refresh()`, and return the dashboard.

  `DashboardImageReferenceError` propagates, and the session closes without a commit.

- [ ] **Step 4: Add the DTO and route in `src/routes/dashboard.py`.**
  - `class DashboardDuplicateDTO(BaseModel): dashboard_name: str`, with a Pydantic v1 `@validator("dashboard_name")` that returns `value.strip()` and raises `ValueError` when the result is empty.
  - `@router.post("/{dashboard_id}/duplicate", response_model=DashboardResponseDTO) def duplicate_dashboard(...)`, scope `write:dashboards`. It calls `duplicate_dashboard_db(tenant_id, dashboard_id, dto.dashboard_name, authenticated_entity.email)` and maps outcomes as follows:
    - `None` → `HTTPException(404, "Dashboard not found")`
    - `DashboardNameTakenError` → `HTTPException(409, "A dashboard with this name already exists")`
    - `DashboardImageReferenceError` → `_invalid_images_response(e)`
  - On success it calls `logger.info("Duplicated dashboard", extra={"tenant_id", "source_dashboard_id", "new_dashboard_id", "copied_images": len(referenced_image_ids(dashboard.dashboard_config))})`.

- [ ] **Step 5: Run the duplicate tests, then the neighbouring suites.**
  - Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_dashboard_duplicate.py tests/test_dashboard_images.py tests/test_dashboard.py -v`
  - Expected: all pass.

- [ ] **Step 6: Lint the touched files and commit.**
  - Lint `src/repositories/db.py`, `src/routes/dashboard.py` and the test file. For `db.py` and `dashboard.py`, compare against `git show origin/dev:<path>`: only findings on your added lines count.
  - Commit with the message `feat(dashboard): add POST /dashboard/{id}/duplicate`.

### Task 3: UI naming helpers, metric label and duplicate hook

**Files:**
- Create: `src/features/dashboard-duplicate/lib/copy-name.ts`
- Create: `src/features/dashboard-duplicate/lib/__tests__/copy-name.test.ts`
- Create: `src/features/dashboard-duplicate/model/useDuplicateDashboard.ts`
- Create: `src/features/dashboard-duplicate/index.ts`
- Modify: `src/metrics/labels.ts` (add `"duplicate_dashboard"` after `"create_dashboard"` in `ACTION_LABELS`)

**Interfaces:**
- Consumes: `useApi()` (`api.post(url, body)` throws `KeepApiError` with `statusCode`); `useDashboards()` (`mutate`); `Dashboard` from `utils/hooks/useDashboards`; `recordAction(label, seconds)` and `recordError(label, statusCode?)` from `@/utils/metrics`.
- Produces:
  - `isDashboardNameTaken(name: string, existingNames: string[]): boolean`
  - `defaultCopyName(sourceName: string, existingNames: string[]): string`
  - `useDuplicateDashboard(): { duplicateDashboard(sourceId: string, dashboardName: string): Promise<Dashboard>; refreshDashboards(): Promise<unknown> }`

- [ ] **Step 1: Write the failing helper tests.**

```ts
expect(defaultCopyName("Ops", [])).toBe("Ops (copy)");
expect(defaultCopyName("Ops", ["Ops", "Ops (copy)"])).toBe("Ops (copy 2)");
expect(defaultCopyName("Ops", ["ops (COPY)", "Ops (copy 2)"])).toBe("Ops (copy 3)");
expect(isDashboardNameTaken("  ops ", ["Ops"])).toBe(true);
expect(isDashboardNameTaken("Ops (copy)", ["Ops"])).toBe(false);
expect(isDashboardNameTaken("   ", ["Ops"])).toBe(false);
```

- [ ] **Step 2: Run them to verify they fail.**
  - Run: `npx jest src/features/dashboard-duplicate`
  - Expected: cannot find module.

- [ ] **Step 3: Implement the helpers, the label and the hook.**
  - `duplicateDashboard` posts to `` `/dashboard/${encodeURIComponent(sourceId)}/duplicate` `` with `{ dashboard_name: dashboardName.trim() }`. It then:
    - awaits `mutate((current) => [...(current ?? []), created], { revalidate: true })`
    - calls `recordAction("duplicate_dashboard", seconds)`
    - returns the created dashboard
  - On error, the hook calls `recordError("duplicate_dashboard", error.statusCode)` and rethrows.
  - `refreshDashboards` is `mutate()`.
  - `index.ts` exports `DuplicateDashboardModal` once Task 4 exists. For now it exports the helpers and the hook.

- [ ] **Step 4: Run the tests to verify they pass, and run the existing metrics tests.**
  - Run: `npx jest src/features/dashboard-duplicate src/metrics src/app/api/metrics`
  - Expected: all pass.

- [ ] **Step 5: Commit** with the message `feat(dashboard): add duplicate naming helpers, hook and metric label`.

### Task 4: Duplicate dialog

**Files:**
- Create: `src/features/dashboard-duplicate/ui/DuplicateDashboardModal.tsx`
- Create: `src/features/dashboard-duplicate/ui/__tests__/DuplicateDashboardModal.test.tsx`
- Modify: `src/features/dashboard-duplicate/index.ts` (export the modal)

**Interfaces:**
- Consumes:
  - From Task 3: `defaultCopyName`, `isDashboardNameTaken`, `useDuplicateDashboard`.
  - `Modal` from `@/components/ui/Modal` (`isOpen`, `onClose`, `title`, `className`).
  - Tremor `TextInput` (`error`, `errorMessage`) and `Button` (`loading`).
  - `useNavigationGuardStore`, `confirmNavigation` from `@/shared/lib/navigation-guard`.
  - `usePathname`, `useRouter`; `toast` from `react-toastify`; `showErrorToast` from `@/shared/ui`; `KeepApiError` from `@/shared/api`.
- Produces: `DuplicateDashboardModal({ source, existingNames, onClose }: { source: Dashboard | null; existingNames: string[]; onClose: () => void })`. It is open while `source` is non-null.

**Behaviour:**
- When `source` changes, reset the name to `defaultCopyName(source.dashboard_name, existingNames)` and clear the server error.
- The form `onSubmit` submits, so Enter works.
- Submit is disabled when any of these is true:
  - the trimmed name is empty
  - `isDashboardNameTaken` returns true
  - a request is pending
- On success: `onClose()`, `toast.success("Dashboard duplicated")`, then `` confirmNavigation(() => router.push(`/dashboard/${encodeURIComponent(created.dashboard_name)}`)) ``.
- Errors:
  - `statusCode === 409`: set the inline name error.
  - `statusCode === 404`: set the inline error `This dashboard no longer exists` and call `refreshDashboards()`.
  - Anything else: `showErrorToast(error, "Failed to duplicate dashboard")`.
- The unsaved note shows when both are true: `decodeURIComponent(pathname) === `/dashboard/${source.dashboard_name}`` and `useNavigationGuardStore((s) => s.blockers.length > 0)`.
- data-cy values: `dashboard-duplicate-modal`, `dashboard-duplicate-name-input`, `dashboard-duplicate-name-error`, `dashboard-duplicate-unsaved-note`, `dashboard-duplicate-cancel-btn`, `dashboard-duplicate-submit-btn`.

- [ ] **Step 1: Write the failing tests.** Mock `@/shared/lib/hooks/useApi` (`post`), `utils/hooks/useDashboards` (`mutate`), `next/navigation`, `react-toastify`, `@/utils/metrics`, and `@/shared/ui` `showErrorToast`. Each test drives a real `useNavigationGuardStore` state.
  - `prefills the copy name and explains what is copied`: shows `NOC Overview (copy)` with source `NOC Overview` and existing `["NOC Overview", "Ops"]`, plus the info text.
  - `disables submit and shows the clash message for an existing name in another case`: typing `ops` disables submit and shows `A dashboard with this name already exists`.
  - `disables submit for a blank name`: typing `   `.
  - `submits with Enter, posts the trimmed name and navigates to the copy`:
    - `post` is called with `("/dashboard/d1/duplicate", { dashboard_name: "NOC Overview (copy)" })`.
    - `push("/dashboard/NOC%20Overview%20(copy)")` happens.
    - `toast.success("Dashboard duplicated")` is called.
  - `encodes URL-special characters when navigating`: the name `a/b%#` leads to `push("/dashboard/a%2Fb%25%23")`.
  - `does not post twice while the request is pending`: two clicks on a never-resolving `post` give one call, and the button is disabled.
  - `keeps the dialog open with the clash message on 409`: `post` rejects with `new KeepApiError("x", "u", "", {}, 409)`, so no push and the message shows.
  - `shows the source-gone message on 404 and refreshes the list`.
  - `toasts other errors`: a 500 calls `showErrorToast`.
  - `shows the unsaved note only for the open dashboard with unsaved edits`: path `/dashboard/NOC Overview` plus one registered blocker shows the note. Removing the blocker or changing the path hides it.
  - `Cancel closes without posting`.

- [ ] **Step 2: Run them to verify they fail.**
  - Run: `npx jest src/features/dashboard-duplicate/ui`
  - Expected: cannot find module.

- [ ] **Step 3: Implement the modal.** The layout follows the approved mock:
  - title
  - subtitle with the bold source name
  - `Name` label and input
  - gray info box, plus the unsaved note when it applies
  - right-aligned Cancel (secondary) and Duplicate (primary orange) buttons

- [ ] **Step 4: Run the tests to verify they pass, then typecheck and lint.**
  - Run: `npx jest src/features/dashboard-duplicate && npx tsc --noEmit && npx eslint src/features/dashboard-duplicate`
  - Expected: tests pass, no new type errors, and no lint errors in these files.

- [ ] **Step 5: Commit** with the message `feat(dashboard): add duplicate dashboard dialog`.

### Task 5: Sidebar hover action

**Files:**
- Modify: `src/components/LinkWithIcon.tsx`
- Modify: `src/components/navbar/DashboardLink.tsx`
- Modify: `src/components/navbar/DashboardLinks.tsx`
- Test: `src/components/__tests__/LinkWithIcon.test.tsx` (new)
- Test: `src/components/navbar/__tests__/DashboardLinks.test.tsx` (extend)

**Interfaces:**
- Consumes: `DuplicateDashboardModal` from `@/features/dashboard-duplicate` (Task 4); `Tooltip` from `@/shared/ui`; `FiCopy` from `react-icons/fi`.
- Produces:
  - `LinkWithIcon` gains optional `onDuplicate?: () => void`, `duplicateAriaLabel?: string`, `duplicateDataCy?: string`.
  - `DashboardLink` gains a required `duplicateDashboard: (id: string) => void`.

- [ ] **Step 1: Write the failing tests.**
  - `LinkWithIcon` (with `next/navigation` mocked):
    - With `onDuplicate`, a button with `aria-label="Duplicate Ops"` is rendered.
    - Clicking it calls `onDuplicate` once and calls `preventDefault`/`stopPropagation`, so the link is not followed. Assert via a parent `onClick` spy that is not called.
    - Without `onDuplicate`, no such button exists.
    - The existing delete button still calls `onDelete`.
  - `DashboardLinks`: extend the `DashboardLink` mock to render a `duplicate {name}` button that calls `duplicateDashboard(dashboard.id)`, and mock `@/features/dashboard-duplicate` to render `dialog for {source?.dashboard_name}` when `source` is set. Add:
    - `opens the duplicate dialog for the clicked dashboard`: clicking `duplicate Ops` shows `dialog for Ops`.
    - `passes every dashboard name as existing names`.

- [ ] **Step 2: Run them to verify they fail.**
  - Run: `npx jest src/components/__tests__/LinkWithIcon.test.tsx src/components/navbar`
  - Expected: FAIL.

- [ ] **Step 3: Implement the sidebar action.**
  - In `LinkWithIcon`, render the copy button before the trash button, wrapped in `<Tooltip content="Duplicate">`.
  - The icon className is `text-slate-400 hover:text-orange-500 group-hover:block group-focus-within:block hidden h-4 w-4`. It matches the trash icon, and keyboard focus also reveals it.
  - Also add `group-focus-within:block` to the trash icon so both are reachable by keyboard.
  - `DashboardLink` passes:
    - `onDuplicate={() => duplicateDashboard(dashboard.id)}`
    - `` duplicateAriaLabel={`Duplicate ${dashboard.dashboard_name}`} ``
    - `` duplicateDataCy={`nav-link-dashboard-duplicate-${dashboard.id}`} ``
  - `DashboardLinks` holds `const [duplicateSource, setDuplicateSource] = useState<Dashboard | null>(null)` and renders `<DuplicateDashboardModal source={duplicateSource} existingNames={dashboards.map((d) => d.dashboard_name)} onClose={() => setDuplicateSource(null)} />`.

- [ ] **Step 4: Run the tests to verify they pass, then the whole dashboard and navbar area, typecheck and lint.**
  - Run: `npx jest src/components src/features/dashboard-duplicate "src/app/(keep)/dashboard" && npx tsc --noEmit && npx eslint src/components/LinkWithIcon.tsx src/components/navbar`
  - Expected: all pass, with no new type or lint errors.

- [ ] **Step 5: Commit** with the message `feat(dashboard): duplicate a dashboard from the sidebar`.

### Task 6: Live demo (run by the controller, not an implementer)

**Files:** `/Users/yarin/keep-namespace/.worktrees/dashboard-duplicate/demo/{start-demo.sh,stop-demo.sh,reset-demo.sh,README.md}`. Model them on `.worktrees/dashboard-drag-gravity/demo/`.

- Ports: UI `:3006`, gateway `127.0.0.1:8093` (noauth). DB: the shared `cbm-e2e-pg` on `:5440`, which already has `dashboardimage`. The gateway runs from this branch's worktree with `NO_PROXY='*'` and `PGGSSENCMODE=disable`.
- Seed a `Duplicate Demo` dashboard, idempotently. It has:
  - a URL image widget
  - an uploaded PNG image widget (upload via `POST /dashboard-images`, then save)
  - an HTML widget
  - `appearance.backgroundColor`

  `reset-demo.sh` deletes any `Duplicate Demo (copy…)` dashboards.
- Browser check (sonnet lane), with screenshots:
  1. Hovering a sidebar entry shows the gray copy icon and the `Duplicate` tooltip, and the dashboard header has no new button.
  2. The dialog prefills `Duplicate Demo (copy)`.
  3. Typing `duplicate demo` shows the clash message and disables the button.
  4. Confirming lands on the copy with the toast, the copy's uploaded image renders, and the sidebar lists the copy.
  5. Deleting the source keeps the copy's image.
  6. Duplicating again prefills `(copy 2)`.
  7. Editing the source without saving, then duplicating it from the sidebar, shows the unsaved note.
