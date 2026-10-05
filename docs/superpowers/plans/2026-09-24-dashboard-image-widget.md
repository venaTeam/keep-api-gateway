# Dashboard Image Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an IMAGE dashboard widget that shows a picture from a URL or from an image uploaded into Keep's own Postgres. Uploads stay *pending* until a dashboard save claims them, and a save that references a bad upload is rejected.

**Architecture:**
- **keep-migrations:** a hand-written revision adds a `dashboardimage` table.
- **keep-api-gateway:**
  - adds `POST /dashboard-images`, which takes the image as the raw request body so the size cap applies to the stream itself, and `GET /dashboard-images/{id}`
  - hooks a claim step into the single dashboard write path in `src/repositories/db.py`, in the same transaction as the dashboard row
- **keep-ui:** adds `WidgetType.IMAGE` with a form (upload/URL, fit, link) and a grid renderer, following the Service Now widget pattern.

**Tech Stack:**
- Alembic + SQLAlchemy 2.0 + sqlmodel 0.0.22 (keep-migrations)
- FastAPI 0.115 / Starlette 0.46 + SQLModel, pytest (gateway)
- Next.js 15 + React 19 + Tremor + SWR 2, Jest + Testing Library (keep-ui)

**Spec:** `keep-api-gateway/docs/superpowers/specs/2026-09-24-dashboard-image-widget-design.md` (commit `17f934d`)

**Worktrees** (branch `feat/dashboard-image-widget` off `origin/dev` in each; **local only — never push, never open a PR, never touch dev/main**):
- `W=/Users/yarin/keep-namespace/.worktrees/dashboard-image`
- `$W/keep-migrations`, `$W/keep-api-gateway`, `$W/keep-ui`

## Amendments to the spec (decided while planning; confirm at plan review)

1. **The upload body is raw bytes, not multipart.** The endpoint becomes `POST /dashboard-images?name=<filename>` with `Content-Type: <image type>`.
   - **Why:** Starlette 0.46's multipart parser spools a file part of any size to disk *before* the handler runs. The spec's "413 while reading chunks" could not bound the transfer.
   - **What it covers:** reading `request.stream()` ourselves makes the cap real for missing, chunked and false `Content-Length`.
   - **Truncated uploads:** a truncated upload now ends in a client disconnect, and no row is written.
2. **The caps are read per request** through `src.config.core.config(...)` inside the route module, not as import-time constants in `src/config/config.py`.
   - **Why:** `config.py` doubles as the gunicorn config and is reloaded out of order by the test fixtures.
   - **Env var names and defaults are unchanged:** `KEEP_DASHBOARD_IMAGE_MAX_BYTES=5242880`, `KEEP_DASHBOARD_IMAGE_MAX_PENDING=20`.
3. **An `IMAGE` widget with `source: "upload"` but no `imageId` also blocks the save** (`400`, empty `invalid_image_ids`). This applies the spec's "block if not completed properly" rule to a widget whose upload never finished.
4. **An SVG must be valid UTF-8.** This closes the UTF-16 route around the `<!DOCTYPE`/`<!ENTITY` byte check.

## Global Constraints

- Nothing reaches origin: no `git push`, no PR, and no commits on `dev`/`main`. All commits go to `feat/dashboard-image-widget` in the worktrees above.
- Commit messages end with exactly one trailer: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. No Claude-Session line and no URLs.
- Code comments: docstrings/JSDoc only. **No inline explanatory `#` or `//` comments.**
- Pydantic v1 in the gateway.
- Python lint only on changed paths: `poetry run ruff check <files>` / `poetry run black <files>` / `poetry run isort <files>`. Never run bare `black src/` or `isort src/`, which reformats 174 unrelated files.
- Gateway and migrations tests always run as `PYTHONPATH=. poetry run pytest …` from the worktree root.
- Migrations are hand-written: no `--autogenerate`, and no model imports in a revision.
- Allowed content types, verbatim: `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `image/svg+xml`.
- Size cap: `KEEP_DASHBOARD_IMAGE_MAX_BYTES`, default `5242880`. Pending cap: `KEEP_DASHBOARD_IMAGE_MAX_PENDING`, default `20`. Pending grace period: 24 hours.
- Scopes: `write:dashboards` for upload, `read:dashboards` for fetch.
- Fetch response headers, verbatim:
  - `X-Content-Type-Options: nosniff`
  - `Content-Security-Policy: sandbox`
  - `Cache-Control: private, max-age=31536000, immutable`
- The save-rejection body, verbatim: `{"message": "Dashboard references missing or unavailable images", "invalid_image_ids": [...]}` with status `400`.
- The UI accepts only `http:`/`https:` for the image URL and the link. Links open with `target="_blank" rel="noopener noreferrer"`.

## Review Focus

1. **Two dashboards racing to claim the same pending image.** Exactly one save succeeds; the other gets `400` naming that image. Pinned in Task 5 (`test_claim_race_loser_is_rejected`) by making the claim `UPDATE … WHERE dashboard_id IS NULL` check its row count.
2. **An upload sent with chunked transfer encoding and no `Content-Length`,** larger than the cap. Expect `413` with nothing stored. Pinned in Task 4 (`test_upload_chunked_over_cap_is_413`).
3. **A saved dashboard whose widget type is switched from IMAGE to another type** (the `image` field stays on the widget object). The image is treated as unreferenced and deleted, and the save succeeds. Pinned in Task 5 (`test_non_image_widget_with_stale_image_field_releases_it`).
4. **An SVG encoded as UTF-16 that hides a `<!DOCTYPE>`.** It is rejected with `415`. Pinned in Task 3 (`test_svg_must_be_utf8`).
5. **Renaming a dashboard without sending `dashboard_config`.** Its images are left alone. Pinned in Task 5 (`test_rename_without_config_keeps_images`).

---

### Task 0: Worktree environments

**Files:** none tracked (environment only).

- [ ] **Step 1: Link the gateway venv and clone keep-ui node_modules**

```bash
W=/Users/yarin/keep-namespace/.worktrees/dashboard-image
ln -s /Users/yarin/keep-namespace/keep-api-gateway/.venv "$W/keep-api-gateway/.venv"
cp -c -R /Users/yarin/keep-namespace/keep-ui/node_modules "$W/keep-ui/node_modules"
```

- [ ] **Step 2: Install keep-migrations dependencies (no venv exists yet)**

```bash
cd "$W/keep-migrations" && poetry install --no-interaction
```

Expected: exits 0.

- [ ] **Step 3: Baseline runs (must pass before any change)**

```bash
cd "$W/keep-migrations" && PYTHONPATH=. poetry run pytest -q
cd "$W/keep-api-gateway" && PYTHONPATH=. poetry run pytest tests/test_dashboard.py tests/test_schema_drift.py -q
cd "$W/keep-ui" && npx jest src/entities/alerts/ui/__tests__/DisposeOnNewAlertToggle.test.tsx
cd "$W/keep-ui" && npm run typecheck 2>&1 | tail -5
cd "$W/keep-ui" && npm run lint 2>&1 | tail -5
```

Expected: the tests pass. Record the test counts and the typecheck/lint error counts; later tasks compare against these baselines.

---

### Task 1: keep-migrations — `add_dashboard_image` revision

**Files:**
- Create: `keep-migrations/migrations/versions/2026-09-24-00-00_add_dashboard_image.py`
- Test: `keep-migrations/tests/test_add_dashboard_image_migration.py`

**Interfaces:**
- Produces:
  - **Table `dashboardimage`:** columns `id`, `tenant_id`, `dashboard_id`, `name`, `content_type`, `size_bytes`, `image_blob`, `created_by`, `created_at`.
  - **Indexes:** `ix_dashboardimage_tenant_id`, `ix_dashboardimage_dashboard_id`.
  - **Revision id:** `add_dashboard_image`, with `down_revision = "derive_alert_suppression"`.

- [ ] **Step 1: Write the failing test**

```python
"""Execution coverage for the `add_dashboard_image` revision.

Loads the revision by path and drives its real upgrade()/downgrade() through
alembic's MigrationContext against a SQLite database we own, as the other
tests/test_*_migration.py files do.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "migrations",
    "versions",
    "2026-09-24-00-00_add_dashboard_image.py",
)

EXPECTED_COLUMNS = {
    "id": False,
    "tenant_id": False,
    "dashboard_id": True,
    "name": False,
    "content_type": False,
    "size_bytes": False,
    "image_blob": False,
    "created_by": True,
    "created_at": False,
}


def _load():
    spec = importlib.util.spec_from_file_location("add_dashboard_image", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            step()


@pytest.fixture
def migration():
    return _load()


@pytest.fixture
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'images.db'}")
    md = sa.MetaData()
    sa.Table("tenant", md, sa.Column("id", sa.String(), primary_key=True))
    sa.Table("dashboard", md, sa.Column("id", sa.String(), primary_key=True))
    md.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_revision_chain(migration):
    assert migration.revision == "add_dashboard_image"
    assert migration.down_revision == "derive_alert_suppression"


def test_upgrade_creates_table_columns_and_nullability(engine, migration):
    _run(engine, migration.upgrade)
    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("dashboardimage")}
    assert set(columns) == set(EXPECTED_COLUMNS)
    for name, nullable in EXPECTED_COLUMNS.items():
        assert columns[name]["nullable"] is nullable, name


def test_upgrade_creates_indexes(engine, migration):
    _run(engine, migration.upgrade)
    indexes = {
        i["name"]: i["column_names"]
        for i in sa.inspect(engine).get_indexes("dashboardimage")
    }
    assert indexes["ix_dashboardimage_tenant_id"] == ["tenant_id"]
    assert indexes["ix_dashboardimage_dashboard_id"] == ["dashboard_id"]


def test_upgrade_creates_foreign_keys(engine, migration):
    _run(engine, migration.upgrade)
    fks = {
        fk["constrained_columns"][0]: fk
        for fk in sa.inspect(engine).get_foreign_keys("dashboardimage")
    }
    assert fks["tenant_id"]["referred_table"] == "tenant"
    assert fks["dashboard_id"]["referred_table"] == "dashboard"
    assert fks["dashboard_id"]["options"].get("ondelete") == "CASCADE"


def test_downgrade_drops_table(engine, migration):
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert "dashboardimage" not in sa.inspect(engine).get_table_names()


def test_downgrade_emits_drop_table_so_check_refuses_it(migration):
    """keep-migrate --check refuses any path whose SQL contains DROP TABLE."""
    import io

    buffer = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(ctx):
        migration.downgrade()
    assert "DROP TABLE dashboardimage" in buffer.getvalue()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd $W/keep-migrations && PYTHONPATH=. poetry run pytest tests/test_add_dashboard_image_migration.py -v`
Expected: FAIL (`FileNotFoundError` for the migration path).

- [ ] **Step 3: Write the revision**

```python
"""add dashboardimage: images uploaded for dashboard IMAGE widgets

A row is created pending (dashboard_id NULL) by keep-api-gateway's
POST /dashboard-images and is claimed by the dashboard save that references it,
in the same transaction. Deleting a dashboard deletes its images; the FK's
ON DELETE CASCADE is only a backstop for that.

No backfill: the table is new and no existing dashboard references an image.
Downgrade drops the table, so `keep-migrate --check` refuses it without
--allow-destructive.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mysql

revision = "add_dashboard_image"
down_revision = "derive_alert_suppression"
branch_labels = None
depends_on = None

TABLE = "dashboardimage"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("dashboard_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("content_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "image_blob",
            sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"),
            nullable=False,
        ),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboard.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dashboardimage_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_dashboardimage_dashboard_id", TABLE, ["dashboard_id"])


def downgrade() -> None:
    op.drop_index("ix_dashboardimage_dashboard_id", table_name=TABLE)
    op.drop_index("ix_dashboardimage_tenant_id", table_name=TABLE)
    op.drop_table(TABLE)
```

- [ ] **Step 4: Run the test to verify it passes, then the whole suite**

Run: `PYTHONPATH=. poetry run pytest tests/test_add_dashboard_image_migration.py -v && PYTHONPATH=. poetry run pytest -q`
Expected: 6 new tests PASS; the full suite count equals the baseline + 6.

- [ ] **Step 5: Confirm a single head**

Run: `PYTHONPATH=. poetry run alembic heads`
Expected: exactly `add_dashboard_image (head)`.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/2026-09-24-00-00_add_dashboard_image.py tests/test_add_dashboard_image_migration.py
git commit -m "feat: add dashboardimage table for dashboard image widgets

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Gateway — `DashboardImage` model and schema registration

**Files:**
- Create: `keep-api-gateway/src/models/db/dashboard_image.py`
- Modify: `keep-api-gateway/src/models/db/all_models.py` (the wildcard import block and the `MODELS` tuple)
- Test: `keep-api-gateway/tests/test_dashboard_images.py` (new file; later tasks append to it)

**Interfaces:**
- Produces: `src.models.db.dashboard_image.DashboardImage`, a SQLModel with table `dashboardimage` and these fields:
  - `id: str`
  - `tenant_id: str`
  - `dashboard_id: Optional[str]`
  - `name: str`
  - `content_type: str`
  - `size_bytes: int`
  - `image_blob: bytes`
  - `created_by: Optional[str]`
  - `created_at: datetime` (naive UTC)

- [ ] **Step 1: Write the failing test**

```python
"""Dashboard image widget: model, upload/fetch routes and the save-time claim."""

from src.models.db.all_models import declared_tables


def test_dashboardimage_is_declared_with_migration_columns():
    assert declared_tables()["dashboardimage"] == {
        "id",
        "tenant_id",
        "dashboard_id",
        "name",
        "content_type",
        "size_bytes",
        "image_blob",
        "created_by",
        "created_at",
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd $W/keep-api-gateway && PYTHONPATH=. poetry run pytest tests/test_dashboard_images.py -v`
Expected: FAIL with `KeyError: 'dashboardimage'`.

- [ ] **Step 3: Write the model and register it**

`src/models/db/dashboard_image.py`:

```python
"""Images uploaded for dashboard IMAGE widgets.

A row is pending while `dashboard_id` is NULL. The dashboard save that
references it claims it (src/repositories/dashboard_images.py), and deleting
that dashboard deletes it.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, LargeBinary
from sqlalchemy.dialects import mysql
from sqlmodel import Field, SQLModel
from sqlmodel.sql.sqltypes import AutoString


class DashboardImage(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id", index=True)
    dashboard_id: Optional[str] = Field(
        default=None,
        sa_column=Column(
            AutoString,
            ForeignKey("dashboard.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    name: str
    content_type: str
    size_bytes: int
    image_blob: bytes = Field(
        sa_column=Column(
            LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"), nullable=False
        )
    )
    created_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

In `src/models/db/all_models.py`, add this line after the `dashboard` wildcard import:

```python
from src.models.db.dashboard_image import *  # noqa: F401,F403
```

Also add this entry to `MODELS` right after `Dashboard,  # noqa: F405`:

```python
    DashboardImage,  # noqa: F405
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_images.py tests/test_schema_drift.py -v`
Expected: PASS.

- [ ] **Step 5: Lint changed files and commit**

```bash
poetry run ruff check src/models/db/dashboard_image.py src/models/db/all_models.py tests/test_dashboard_images.py
poetry run black src/models/db/dashboard_image.py tests/test_dashboard_images.py
poetry run isort src/models/db/dashboard_image.py tests/test_dashboard_images.py
git add src/models/db/dashboard_image.py src/models/db/all_models.py tests/test_dashboard_images.py
git commit -m "feat: add DashboardImage model

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Gateway — image content validation

**Files:**
- Create: `keep-api-gateway/src/services/dashboard_image_validation.py`
- Test: `keep-api-gateway/tests/test_dashboard_image_validation.py`

**Interfaces:**
- Produces:
  - `ALLOWED_CONTENT_TYPES: frozenset[str]`
  - `class ImageContentError(ValueError)`
  - `validate_image_content(content_type: str, data: bytes) -> None`, which raises `ImageContentError`

- [ ] **Step 1: Write the failing tests**

```python
"""validate_image_content: declared type must match the bytes."""

import pytest

from src.services.dashboard_image_validation import (
    ALLOWED_CONTENT_TYPES,
    ImageContentError,
    validate_image_content,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'


@pytest.mark.parametrize(
    "content_type,data",
    [
        ("image/png", PNG),
        ("image/jpeg", JPEG),
        ("image/gif", GIF),
        ("image/webp", WEBP),
        ("image/svg+xml", SVG),
        ("image/svg+xml", b'<?xml version="1.0"?>' + SVG),
    ],
)
def test_valid_images_pass(content_type, data):
    validate_image_content(content_type, data)


def test_allowed_types_are_exactly_the_five():
    assert ALLOWED_CONTENT_TYPES == {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
    }


@pytest.mark.parametrize(
    "content_type,data",
    [
        ("image/png", JPEG),
        ("image/jpeg", PNG),
        ("image/gif", b"GIF90a" + b"\x00" * 8),
        ("image/webp", b"RIFF\x00\x00\x00\x00WAVE"),
        ("text/html", b"<html></html>"),
        ("image/bmp", b"BM" + b"\x00" * 16),
    ],
)
def test_mismatched_or_unsupported_rejected(content_type, data):
    with pytest.raises(ImageContentError):
        validate_image_content(content_type, data)


@pytest.mark.parametrize(
    "data",
    [
        b'<!DOCTYPE svg [<!ENTITY a "x">]><svg xmlns="http://www.w3.org/2000/svg"/>',
        b'<?xml version="1.0"?><!ENTITY a "x"><svg/>',
        b"<html><body/></html>",
        b"<svg",
        b"not xml at all",
    ],
)
def test_bad_svg_rejected(data):
    with pytest.raises(ImageContentError):
        validate_image_content("image/svg+xml", data)


def test_svg_must_be_utf8():
    hidden = '<!DOCTYPE svg [<!ENTITY a "x">]><svg/>'.encode("utf-16")
    with pytest.raises(ImageContentError):
        validate_image_content("image/svg+xml", hidden)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_image_validation.py -v`
Expected: FAIL with `ModuleNotFoundError: src.services.dashboard_image_validation`.

- [ ] **Step 3: Implement**

```python
"""Checks that uploaded bytes really are the image type they claim to be.

SVG gets stricter treatment because it is XML: it must be UTF-8, must not
declare a DOCTYPE or entities, and must have an <svg> root element.
"""

import xml.etree.ElementTree as ET

ALLOWED_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
)

_SIGNATURES = {
    "image/png": lambda d: d.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda d: d.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda d: d.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP",
}


class ImageContentError(ValueError):
    """The bytes are not a well-formed image of the declared type."""


def validate_image_content(content_type: str, data: bytes) -> None:
    """Raise ImageContentError unless `data` is a `content_type` image."""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ImageContentError(f"unsupported content type {content_type!r}")
    if content_type == "image/svg+xml":
        _validate_svg(data)
    elif not _SIGNATURES[content_type](data):
        raise ImageContentError(f"content does not match {content_type}")


def _validate_svg(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ImageContentError("SVG must be UTF-8") from e
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ImageContentError("SVG must not declare a DOCTYPE or entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ImageContentError("SVG is not well-formed XML") from e
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ImageContentError("SVG root element must be <svg>")
```

`text` is used only for the DOCTYPE/ENTITY scan. Parsing `data` (bytes) keeps files with an `<?xml … encoding="UTF-8"?>` declaration working, because `ET.fromstring` rejects a `str` that carries an encoding declaration.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_image_validation.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
poetry run ruff check src/services/dashboard_image_validation.py tests/test_dashboard_image_validation.py
poetry run black src/services/dashboard_image_validation.py tests/test_dashboard_image_validation.py
poetry run isort src/services/dashboard_image_validation.py tests/test_dashboard_image_validation.py
git add src/services/dashboard_image_validation.py tests/test_dashboard_image_validation.py
git commit -m "feat: validate dashboard image bytes against declared type

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Gateway — upload and fetch routes

**Files:**
- Create: `keep-api-gateway/src/routes/dashboard_images.py`
- Modify: `keep-api-gateway/src/routes/router_setup.py` (import list and one `include_router`)
- Test: `keep-api-gateway/tests/test_dashboard_images.py` (append)

**Interfaces:**
- Consumes: `DashboardImage` (Task 2), and `ALLOWED_CONTENT_TYPES`, `ImageContentError` and `validate_image_content` (Task 3).
- Produces:
  - `POST /dashboard-images?name=<str>`: raw body, `Content-Type` is the image type. Returns `201 {"id","name","content_type","size_bytes"}`; errors are `413`/`415`/`400`/`429`.
  - `GET /dashboard-images/{image_id}`: returns the bytes, or `404`.

- [ ] **Step 1: Append the failing tests**

Merge the import lines below into the file's existing top-of-file import block (ruff's default rules include E402), then append the rest.

```python
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.models.db.dashboard_image import DashboardImage
from src.models.db.tenant import Tenant
from src.repositories.dependencies import SINGLE_TENANT_UUID
from tests.fixtures.client import client, test_app  # noqa

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
AUTH = {"x-api-key": "some-key"}


def _upload(client, data=PNG, content_type="image/png", name="a.png"):
    return client.post(
        f"/dashboard-images?name={name}",
        content=data,
        headers={**AUTH, "Content-Type": content_type},
    )


def _add_image(db_session, tenant_id=SINGLE_TENANT_UUID, dashboard_id=None, age=timedelta(0)):
    image = DashboardImage(
        tenant_id=tenant_id,
        dashboard_id=dashboard_id,
        name="seed.png",
        content_type="image/png",
        size_bytes=len(PNG),
        image_blob=PNG,
        created_at=datetime.utcnow() - age,
    )
    db_session.add(image)
    db_session.commit()
    return image.id


def _add_tenant(db_session, tenant_id="other-tenant"):
    db_session.add(Tenant(id=tenant_id, name=tenant_id, created_by="tests"))
    db_session.commit()
    return tenant_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
@pytest.mark.parametrize(
    "data,content_type",
    [(PNG, "image/png"), (SVG, "image/svg+xml")],
)
def test_upload_then_fetch_round_trip(db_session, client, test_app, data, content_type):
    created = _upload(client, data, content_type)
    assert created.status_code == 201
    body = created.json()
    assert body["content_type"] == content_type
    assert body["size_bytes"] == len(data)

    fetched = client.get(f"/dashboard-images/{body['id']}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.content == data
    assert fetched.headers["content-type"].startswith(content_type)
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert fetched.headers["content-security-policy"] == "sandbox"
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"

    row = db_session.get(DashboardImage, body["id"])
    assert row.dashboard_id is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_unsupported_type_is_415(db_session, client, test_app):
    assert _upload(client, b"<html/>", "text/html").status_code == 415


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_bytes_not_matching_type_is_415(db_session, client, test_app):
    assert _upload(client, SVG, "image/png").status_code == 415


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_empty_is_400(db_session, client, test_app):
    assert _upload(client, b"").status_code == 400


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_over_cap_by_content_length_is_413(db_session, client, test_app, monkeypatch):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_BYTES", "20")
    assert _upload(client, PNG + b"\x00" * 10).status_code == 413
    assert db_session.query(DashboardImage).count() == 0


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_chunked_over_cap_is_413(db_session, client, test_app, monkeypatch):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_BYTES", "20")
    chunks = iter([PNG, b"\x00" * 30])
    response = client.post(
        "/dashboard-images?name=big.png",
        content=chunks,
        headers={**AUTH, "Content-Type": "image/png"},
    )
    assert response.status_code == 413
    assert db_session.query(DashboardImage).count() == 0


def test_read_capped_stops_streams_past_the_limit():
    from src.routes.dashboard_images import _read_capped

    class _Request:
        async def stream(self):
            yield b"x" * 15
            yield b"x" * 15

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read_capped(_Request(), 20))
    assert exc.value.status_code == 413


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_requires_name(db_session, client, test_app):
    response = client.post(
        "/dashboard-images",
        content=PNG,
        headers={**AUTH, "Content-Type": "image/png"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_pending_cap_is_429(db_session, client, test_app, monkeypatch):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_PENDING", "2")
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 201
    assert _upload(client).status_code == 429


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_claimed_images_do_not_count_toward_pending_cap(db_session, client, test_app, monkeypatch):
    monkeypatch.setenv("KEEP_DASHBOARD_IMAGE_MAX_PENDING", "1")
    from src.models.db.dashboard import Dashboard

    dashboard = Dashboard(tenant_id=SINGLE_TENANT_UUID, dashboard_name="d", dashboard_config={})
    db_session.add(dashboard)
    db_session.commit()
    _add_image(db_session, dashboard_id=dashboard.id)
    assert _upload(client).status_code == 201


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_sweeps_only_this_tenants_stale_pending(db_session, client, test_app):
    other = _add_tenant(db_session)
    stale = _add_image(db_session, age=timedelta(hours=25))
    fresh = _add_image(db_session, age=timedelta(hours=1))
    other_stale = _add_image(db_session, tenant_id=other, age=timedelta(hours=25))

    assert _upload(client).status_code == 201

    db_session.expire_all()
    assert db_session.get(DashboardImage, stale) is None
    assert db_session.get(DashboardImage, fresh) is not None
    assert db_session.get(DashboardImage, other_stale) is not None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_fetch_unknown_is_404(db_session, client, test_app):
    assert client.get("/dashboard-images/nope", headers=AUTH).status_code == 404


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_fetch_other_tenants_image_is_404(db_session, client, test_app):
    other = _add_tenant(db_session)
    image_id = _add_image(db_session, tenant_id=other)
    assert client.get(f"/dashboard-images/{image_id}", headers=AUTH).status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_images.py -v`
Expected: the new tests FAIL with `404` on `POST /dashboard-images` (the route is not mounted).

- [ ] **Step 3: Implement the route module**

`src/routes/dashboard_images.py`:

```python
"""Upload and serve images for dashboard IMAGE widgets.

Uploads are the raw request body with the image's Content-Type, not multipart:
Starlette spools a multipart file part of any size before a handler runs, so
the only way to enforce the size cap on the transfer itself is to read the
stream here. An upload is pending until a dashboard save claims it
(src/repositories/dashboard_images.py); pending uploads older than
PENDING_GRACE are swept on the tenant's next upload.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, func
from sqlmodel import Session, col, select

from src.config.core import config
from src.models.db.dashboard_image import DashboardImage
from src.repositories.db import get_session
from src.services.dashboard_image_validation import (
    ALLOWED_CONTENT_TYPES,
    ImageContentError,
    validate_image_content,
)
from src.services.identity_manager.authenticatedentity import AuthenticatedEntity
from src.services.identity_manager.identitymanagerfactory import IdentityManagerFactory

router = APIRouter()
logger = logging.getLogger(__name__)

PENDING_GRACE = timedelta(hours=24)
DEFAULT_MAX_BYTES = 5_242_880
DEFAULT_MAX_PENDING = 20
IMAGE_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Cache-Control": "private, max-age=31536000, immutable",
}


def max_bytes() -> int:
    """Upload size cap, read per request so it follows the environment."""
    return config(
        "KEEP_DASHBOARD_IMAGE_MAX_BYTES", cast=int, default=DEFAULT_MAX_BYTES
    )


def max_pending() -> int:
    """Per-tenant cap on unclaimed uploads, read per request."""
    return config(
        "KEEP_DASHBOARD_IMAGE_MAX_PENDING", cast=int, default=DEFAULT_MAX_PENDING
    )


def _too_large(limit: int) -> HTTPException:
    return HTTPException(413, f"Image exceeds the {limit} byte limit")


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the body, failing with 413 as soon as it passes `limit` bytes."""
    chunks = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _pending_filter(tenant_id: str):
    return (
        DashboardImage.tenant_id == tenant_id,
        col(DashboardImage.dashboard_id).is_(None),
    )


@router.post("", status_code=201)
async def upload_dashboard_image(
    request: Request,
    name: str = Query(..., min_length=1, max_length=255),
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["write:dashboards"])
    ),
    session: Session = Depends(get_session),
):
    """Store an uploaded image as pending until a dashboard save claims it."""
    tenant_id = authenticated_entity.tenant_id
    limit = max_bytes()

    declared_length = request.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > limit:
        raise _too_large(limit)

    content_type = (
        request.headers.get("content-type", "").split(";")[0].strip().lower()
    )
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            415, "Unsupported image type; allowed: PNG, JPEG, GIF, WEBP, SVG"
        )

    data = await _read_capped(request, limit)
    if not data:
        raise HTTPException(400, "Image is empty")
    try:
        validate_image_content(content_type, data)
    except ImageContentError as e:
        raise HTTPException(415, f"Unsupported or corrupt image: {e}")

    session.execute(
        delete(DashboardImage).where(
            *_pending_filter(tenant_id),
            DashboardImage.created_at < datetime.utcnow() - PENDING_GRACE,
        )
    )
    pending = session.exec(
        select(func.count(DashboardImage.id)).where(*_pending_filter(tenant_id))
    ).one()
    if pending >= max_pending():
        session.commit()
        raise HTTPException(
            429, "Too many unsaved images; save or discard the dashboard first"
        )

    image = DashboardImage(
        tenant_id=tenant_id,
        name=name,
        content_type=content_type,
        size_bytes=len(data),
        image_blob=data,
        created_by=authenticated_entity.email,
    )
    session.add(image)
    session.commit()
    logger.info(
        "Dashboard image uploaded",
        extra={"tenant_id": tenant_id, "image_id": image.id, "size": len(data)},
    )
    return {
        "id": image.id,
        "name": image.name,
        "content_type": image.content_type,
        "size_bytes": image.size_bytes,
    }


@router.get("/{image_id}")
def get_dashboard_image(
    image_id: str,
    authenticated_entity: AuthenticatedEntity = Depends(
        IdentityManagerFactory.get_auth_verifier(["read:dashboards"])
    ),
    session: Session = Depends(get_session),
):
    """Serve an image of the caller's tenant; 404 when it does not exist there."""
    image = session.exec(
        select(DashboardImage).where(
            DashboardImage.tenant_id == authenticated_entity.tenant_id,
            DashboardImage.id == image_id,
        )
    ).first()
    if not image:
        raise HTTPException(404, "Image not found")
    return Response(
        content=image.image_blob,
        media_type=image.content_type,
        headers=IMAGE_RESPONSE_HEADERS,
    )
```

In `src/routes/router_setup.py`:
- Add `dashboard_images,` to the `from src.routes import (...)` list, right after `dashboard,`.
- Add this after the `dashboard` `include_router` line:

```python
    app.include_router(
        dashboard_images.router, prefix="/dashboard-images", tags=["dashboard"]
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_images.py -v`
Expected: all PASS. httpx sends an iterator body with `Transfer-Encoding: chunked` and no `Content-Length`, so `test_upload_chunked_over_cap_is_413` exercises the stream path. `test_read_capped_stops_streams_past_the_limit` pins the same rule without the HTTP stack.

- [ ] **Step 5: Lint and commit**

```bash
poetry run ruff check src/routes/dashboard_images.py src/routes/router_setup.py tests/test_dashboard_images.py
poetry run black src/routes/dashboard_images.py tests/test_dashboard_images.py
poetry run isort src/routes/dashboard_images.py tests/test_dashboard_images.py
git add src/routes/dashboard_images.py src/routes/router_setup.py tests/test_dashboard_images.py
git commit -m "feat: add dashboard image upload and fetch endpoints

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Gateway — claim images in the dashboard write path

**Files:**
- Create: `keep-api-gateway/src/repositories/dashboard_images.py`
- Modify: `keep-api-gateway/src/repositories/db.py` (`create_dashboard` ~2434, `update_dashboard` ~2451, `delete_dashboard` ~2477, plus one import)
- Modify: `keep-api-gateway/src/routes/dashboard.py` (`create_dashboard`/`update_dashboard` handlers + imports)
- Test: `keep-api-gateway/tests/test_dashboard_images.py` (append)

**Interfaces:**
- Consumes: `DashboardImage` (Task 2).
- Produces:
  - `class DashboardImageReferenceError(ValueError)` with `.invalid_image_ids: list[str]`
  - `referenced_image_ids(dashboard_config: dict | None) -> set[str]`, which raises `DashboardImageReferenceError([])` for an upload widget with no id
  - `sync_dashboard_images(session, tenant_id: str, dashboard_id: str, dashboard_config: dict | None) -> None`
  - `delete_dashboard_images(session, tenant_id: str, dashboard_id: str) -> None`
  - HTTP: `POST /dashboard` and `PUT /dashboard/{id}` return `400 {"message": "Dashboard references missing or unavailable images", "invalid_image_ids": [...]}` on a bad reference.

- [ ] **Step 1: Append the failing tests**

Merge the import below into the file's top import block, then append the rest.

```python
from src.models.db.dashboard import Dashboard


def _image_widget(image_id, i="w-1", widget_type="IMAGE"):
    return {
        "i": i,
        "x": 0,
        "y": 0,
        "w": 4,
        "h": 4,
        "static": False,
        "name": f"widget {i}",
        "widgetType": widget_type,
        "image": {"source": "upload", "imageId": image_id, "fit": "contain"},
    }


def _config(*widgets):
    return {"layout": [], "widget_data": list(widgets)}


def _create_dashboard(client, name, config):
    return client.post(
        "/dashboard",
        headers=AUTH,
        json={"dashboard_name": name, "dashboard_config": config},
    )


def _put_dashboard(client, dashboard_id, config=None, name=None):
    body = {}
    if config is not None:
        body["dashboard_config"] = config
    if name is not None:
        body["dashboard_name"] = name
    return client.put(f"/dashboard/{dashboard_id}", headers=AUTH, json=body)


def _owner(db_session, image_id):
    db_session.expire_all()
    row = db_session.get(DashboardImage, image_id)
    return "gone" if row is None else row.dashboard_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_create_claims_pending_image(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    response = _create_dashboard(client, "d1", _config(_image_widget(image_id)))
    assert response.status_code == 200
    assert _owner(db_session, image_id) == response.json()["id"]


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_create_with_unknown_image_is_400_and_writes_nothing(db_session, client, test_app):
    response = _create_dashboard(client, "d1", _config(_image_widget("missing")))
    assert response.status_code == 400
    assert response.json() == {
        "message": "Dashboard references missing or unavailable images",
        "invalid_image_ids": ["missing"],
    }
    assert db_session.query(Dashboard).filter(Dashboard.dashboard_name == "d1").count() == 0


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_upload_widget_without_image_id_is_400(db_session, client, test_app):
    widget = _image_widget(None)
    response = _create_dashboard(client, "d1", _config(widget))
    assert response.status_code == 400
    assert response.json()["invalid_image_ids"] == []


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_other_tenants_image_is_400(db_session, client, test_app):
    other = _add_tenant(db_session)
    image_id = _add_image(db_session, tenant_id=other)
    response = _create_dashboard(client, "d1", _config(_image_widget(image_id)))
    assert response.status_code == 400
    assert _owner(db_session, image_id) is None


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_image_owned_by_another_dashboard_is_400(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    first = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()["id"]
    response = _create_dashboard(client, "d2", _config(_image_widget(image_id)))
    assert response.status_code == 400
    assert _owner(db_session, image_id) == first


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_removing_widget_deletes_its_image(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()["id"]
    assert _put_dashboard(client, dashboard_id, _config()).status_code == 200
    assert _owner(db_session, image_id) == "gone"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_replacing_image_swaps_ownership(db_session, client, test_app):
    old = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(old))).json()["id"]
    new = _upload(client).json()["id"]
    assert _put_dashboard(client, dashboard_id, _config(_image_widget(new))).status_code == 200
    assert _owner(db_session, old) == "gone"
    assert _owner(db_session, new) == dashboard_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_update_with_bad_reference_keeps_previous_state(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()["id"]
    response = _put_dashboard(client, dashboard_id, _config(_image_widget("missing")))
    assert response.status_code == 400
    assert _owner(db_session, image_id) == dashboard_id
    db_session.expire_all()
    stored = db_session.get(Dashboard, dashboard_id).dashboard_config
    assert stored["widget_data"][0]["image"]["imageId"] == image_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_rename_without_config_keeps_images(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()["id"]
    assert _put_dashboard(client, dashboard_id, name="renamed").status_code == 200
    assert _owner(db_session, image_id) == dashboard_id


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_non_image_widget_with_stale_image_field_releases_it(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()["id"]
    switched = _image_widget(image_id, widget_type="METRIC")
    assert _put_dashboard(client, dashboard_id, _config(switched)).status_code == 200
    assert _owner(db_session, image_id) == "gone"


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_url_image_widgets_need_no_upload(db_session, client, test_app):
    widget = _image_widget(None)
    widget["image"] = {"source": "url", "url": "https://example.com/a.png", "fit": "cover"}
    assert _create_dashboard(client, "d1", _config(widget)).status_code == 200


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_dashboards_without_image_widgets_unchanged(db_session, client, test_app):
    config = {"layout": [], "widget_data": [{"i": "w-1", "name": "p", "widgetType": "PRESET"}]}
    response = _create_dashboard(client, "plain", config)
    assert response.status_code == 200
    assert response.json()["dashboard_config"] == config


@pytest.mark.parametrize("test_app", ["NO_AUTH"], indirect=True)
def test_delete_dashboard_deletes_its_images(db_session, client, test_app):
    image_id = _upload(client).json()["id"]
    dashboard_id = _create_dashboard(client, "d1", _config(_image_widget(image_id))).json()["id"]
    assert client.delete(f"/dashboard/{dashboard_id}", headers=AUTH).status_code == 200
    assert _owner(db_session, image_id) == "gone"


def test_claim_race_loser_is_rejected(db_session, monkeypatch):
    """Second of two concurrent saves: it read the image as pending, but the guarded UPDATE matches no row."""
    from src.repositories import dashboard_images as module

    first = Dashboard(tenant_id=SINGLE_TENANT_UUID, dashboard_name="a", dashboard_config={})
    second = Dashboard(tenant_id=SINGLE_TENANT_UUID, dashboard_name="b", dashboard_config={})
    db_session.add_all([first, second])
    db_session.commit()
    image_id = _add_image(db_session)
    config = _config(_image_widget(image_id))

    module.sync_dashboard_images(db_session, SINGLE_TENANT_UUID, first.id, config)
    db_session.commit()

    monkeypatch.setattr(
        module, "_current_owners", lambda session, tenant_id, ids: {i: None for i in ids}
    )
    with pytest.raises(module.DashboardImageReferenceError) as exc:
        module.sync_dashboard_images(db_session, SINGLE_TENANT_UUID, second.id, config)
    db_session.rollback()
    monkeypatch.undo()

    assert exc.value.invalid_image_ids == [image_id]
    assert _owner(db_session, image_id) == first.id
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_images.py -v -k "claim or create or update or rename or stale or url_image or without_image or delete_dashboard or other_tenants_image_is_400 or owned_by"`
Expected: FAIL (bad references return `200`, images are not claimed, and `src.repositories.dashboard_images` does not exist).

- [ ] **Step 3: Implement the claim helpers**

`src/repositories/dashboard_images.py`:

```python
"""Keeps dashboardimage rows in step with the dashboards that reference them.

Called from the dashboard write path in src/repositories/db.py with that
function's session, so claims and deletions commit atomically with the
dashboard row. Only columns are read here, never image blobs.
"""

from sqlalchemy import delete, update
from sqlmodel import Session, col, select

from src.models.db.dashboard_image import DashboardImage

IMAGE_WIDGET_TYPE = "IMAGE"
UPLOAD_SOURCE = "upload"


class DashboardImageReferenceError(ValueError):
    """A dashboard references images that are missing or not available to it."""

    def __init__(self, invalid_image_ids: list[str]):
        super().__init__(f"invalid dashboard image references: {invalid_image_ids}")
        self.invalid_image_ids = invalid_image_ids


def referenced_image_ids(dashboard_config: dict | None) -> set[str]:
    """Uploaded-image ids used by IMAGE widgets in `dashboard_config`.

    Raises DashboardImageReferenceError([]) for an upload widget without an id,
    which is a widget whose upload never completed.
    """
    ids = set()
    for widget in (dashboard_config or {}).get("widget_data") or []:
        if not isinstance(widget, dict) or widget.get("widgetType") != IMAGE_WIDGET_TYPE:
            continue
        image = widget.get("image") or {}
        if image.get("source") != UPLOAD_SOURCE:
            continue
        image_id = image.get("imageId")
        if not isinstance(image_id, str) or not image_id:
            raise DashboardImageReferenceError([])
        ids.add(image_id)
    return ids


def _current_owners(session: Session, tenant_id: str, ids: set[str]) -> dict:
    """{image id -> dashboard_id or None} for the tenant's images among `ids`."""
    rows = session.exec(
        select(DashboardImage.id, DashboardImage.dashboard_id).where(
            DashboardImage.tenant_id == tenant_id, col(DashboardImage.id).in_(ids)
        )
    ).all()
    return {image_id: owner for image_id, owner in rows}


def sync_dashboard_images(
    session: Session, tenant_id: str, dashboard_id: str, dashboard_config: dict | None
) -> None:
    """Claim the images `dashboard_config` references; delete the ones it dropped.

    Raises DashboardImageReferenceError when a referenced id does not exist for
    this tenant, or belongs to another dashboard, including one that claimed it
    concurrently. The caller's transaction must then be rolled back.
    """
    wanted = referenced_image_ids(dashboard_config)
    owners = _current_owners(session, tenant_id, wanted) if wanted else {}
    invalid = sorted(
        i for i in wanted if i not in owners or owners[i] not in (None, dashboard_id)
    )
    if invalid:
        raise DashboardImageReferenceError(invalid)

    pending = sorted(i for i, owner in owners.items() if owner is None)
    if pending:
        claimed = session.execute(
            update(DashboardImage)
            .where(
                DashboardImage.tenant_id == tenant_id,
                col(DashboardImage.id).in_(pending),
                col(DashboardImage.dashboard_id).is_(None),
            )
            .values(dashboard_id=dashboard_id)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != len(pending):
            lost = sorted(
                i
                for i, owner in _current_owners(session, tenant_id, set(pending)).items()
                if owner != dashboard_id
            ) or pending
            raise DashboardImageReferenceError(lost)

    released = delete(DashboardImage).where(
        DashboardImage.tenant_id == tenant_id,
        DashboardImage.dashboard_id == dashboard_id,
    )
    if wanted:
        released = released.where(col(DashboardImage.id).not_in(wanted))
    session.execute(released.execution_options(synchronize_session=False))


def delete_dashboard_images(session: Session, tenant_id: str, dashboard_id: str) -> None:
    """Delete every image the dashboard owns (the FK cascade is only a backstop)."""
    session.execute(
        delete(DashboardImage)
        .where(
            DashboardImage.tenant_id == tenant_id,
            DashboardImage.dashboard_id == dashboard_id,
        )
        .execution_options(synchronize_session=False)
    )
```

How the race test works:
- **Stale read:** it patches `_current_owners` so the second save sees the already-claimed image as pending, which is what a save racing the first one would have read.
- **Guarded update:** `UPDATE … WHERE dashboard_id IS NULL` matches 0 rows, so `rowcount != len(pending)`.
- **Result:** the re-read (still patched, so it reports `None` ≠ `second.id`) names the image as lost.
- **`or pending` fallback:** it covers a re-read that finds nothing, e.g. a row deleted meanwhile.

- [ ] **Step 4: Wire the helpers into `src/repositories/db.py`**

Add near the other `src.repositories` imports at the top of `db.py`:

```python
from src.repositories.dashboard_images import (
    delete_dashboard_images,
    sync_dashboard_images,
)
```

Replace `create_dashboard` with:

```python
def create_dashboard(
    tenant_id, dashboard_name, created_by, dashboard_config, is_private=False
):
    with Session(engine) as session:
        dashboard = Dashboard(
            tenant_id=tenant_id,
            dashboard_name=dashboard_name,
            dashboard_config=dashboard_config,
            created_by=created_by,
            is_private=is_private,
        )
        session.add(dashboard)
        session.flush()
        sync_dashboard_images(session, tenant_id, dashboard.id, dashboard_config)
        session.commit()
        session.refresh(dashboard)
        return dashboard
```

In `update_dashboard`, replace the block:

```python
        if dashboard_config:
            dashboard.dashboard_config = dashboard_config
```

with:

```python
        if dashboard_config:
            sync_dashboard_images(session, tenant_id, dashboard.id, dashboard_config)
            dashboard.dashboard_config = dashboard_config
```

In `delete_dashboard`, replace:

```python
        if dashboard:
            session.delete(dashboard)
```

with:

```python
        if dashboard:
            delete_dashboard_images(session, tenant_id, dashboard_id)
            session.delete(dashboard)
```

- [ ] **Step 5: Map the error to 400 in `src/routes/dashboard.py`**

Add these imports:

```python
from fastapi.responses import JSONResponse

from src.repositories.dashboard_images import DashboardImageReferenceError
```

Add this helper above `read_dashboards`:

```python
def _invalid_images_response(error: DashboardImageReferenceError) -> JSONResponse:
    """400 body the UI uses to name the widgets whose images are unavailable."""
    return JSONResponse(
        status_code=400,
        content={
            "message": "Dashboard references missing or unavailable images",
            "invalid_image_ids": error.invalid_image_ids,
        },
    )
```

Wrap the DB call in `create_dashboard`:

```python
    try:
        dashboard = create_dashboard_db(
            tenant_id=authenticated_entity.tenant_id,
            dashboard_name=dashboard_dto.dashboard_name,
            dashboard_config=dashboard_dto.dashboard_config,
            created_by=email,
        )
    except DashboardImageReferenceError as e:
        return _invalid_images_response(e)
    return dashboard
```

Wrap the DB call in `update_dashboard`:

```python
    try:
        dashboard = update_dashboard_db(
            tenant_id=authenticated_entity.tenant_id,
            dashboard_id=dashboard_id,
            dashboard_name=dashboard_dto.dashboard_name,
            dashboard_config=dashboard_dto.dashboard_config,
            updated_by=authenticated_entity.email,
        )
    except DashboardImageReferenceError as e:
        return _invalid_images_response(e)
    return dashboard
```

The replaced block in `update_dashboard` includes its old `# update the dashboard in the database` comment line; drop that line. Leave comments on lines you don't touch as they are.

`provision_dashboards` already catches `Exception` per dashboard, so a provisioned dashboard with a bad image reference is logged and skipped, and startup continues.

- [ ] **Step 6: Run the tests to verify they pass, plus the existing dashboard tests**

Run: `PYTHONPATH=. poetry run pytest tests/test_dashboard_images.py tests/test_dashboard.py tests/test_schema_drift.py -v`
Expected: all PASS.

- [ ] **Step 7: Lint and commit**

```bash
poetry run ruff check src/repositories/dashboard_images.py src/repositories/db.py src/routes/dashboard.py tests/test_dashboard_images.py
poetry run black src/repositories/dashboard_images.py tests/test_dashboard_images.py
poetry run isort src/repositories/dashboard_images.py tests/test_dashboard_images.py
git diff --stat
git add src/repositories/dashboard_images.py src/repositories/db.py src/routes/dashboard.py tests/test_dashboard_images.py
git commit -m "feat: claim dashboard images on save and reject bad references

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

Check `git diff --stat` before committing: `db.py` and `dashboard.py` must show only the hunks above. Do not run black/isort on them; they are pre-existing unformatted files.

---

### Task 6: UI — widget types, validation helpers, image data access

**Files:**
- Modify: `keep-ui/src/app/(keep)/dashboard/types.tsx`
- Create: `keep-ui/src/app/(keep)/dashboard/widget-types/image/image-widget-validation.ts`
- Create: `keep-ui/src/entities/dashboard-images/model/useDashboardImages.ts`
- Test: `keep-ui/src/app/(keep)/dashboard/widget-types/image/__tests__/image-widget-validation.test.ts`

**Interfaces:**
- Produces:
  - `WidgetType.IMAGE = "IMAGE"`
  - `type ImageFit = "contain" | "cover"`
  - `interface ImageWidgetConfig { source: "upload" | "url"; imageId?: string; url?: string; fit: ImageFit; link?: string }`
  - `WidgetData.image?: ImageWidgetConfig`
  - `ALLOWED_IMAGE_TYPES`, `IMAGE_ACCEPT`, `DEFAULT_MAX_IMAGE_BYTES`
  - `isHttpUrl(value: string): boolean`
  - `validateImageFile(file: File, maxBytes?: number): string | null`
  - `describeInvalidImageError(error: unknown, widgets: WidgetData[]): string | null`
  - `interface DashboardImageUpload { id: string; name: string; content_type: string; size_bytes: number }`
  - `uploadDashboardImage(api: ApiClient, file: File): Promise<DashboardImageUpload>`
  - `fetchDashboardImageBlob(api: ApiClient, imageId: string): Promise<Blob>`
  - `useDashboardImage(imageId?: string): { url?: string; error: unknown; isLoading: boolean }`

- [ ] **Step 1: Write the failing test**

```ts
import { KeepApiError } from "@/shared/api";
import { WidgetData, WidgetType } from "../../../types";
import {
  DEFAULT_MAX_IMAGE_BYTES,
  describeInvalidImageError,
  isHttpUrl,
  validateImageFile,
} from "../image-widget-validation";

function file(type: string, size: number, name = "a.png") {
  return new File([new Uint8Array(size)], name, { type });
}

describe("isHttpUrl", () => {
  it.each(["https://x.io/a.png", "http://intranet/diagram.svg"])(
    "accepts %s",
    (value) => expect(isHttpUrl(value)).toBe(true)
  );
  it.each(["javascript:alert(1)", "data:image/png;base64,AAA", "ftp://x/a", "not a url", ""])(
    "rejects %s",
    (value) => expect(isHttpUrl(value)).toBe(false)
  );
});

describe("validateImageFile", () => {
  it("accepts each allowed type", () => {
    for (const type of ["image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"]) {
      expect(validateImageFile(file(type, 10))).toBeNull();
    }
  });
  it("rejects unsupported types", () => {
    expect(validateImageFile(file("image/bmp", 10))).toMatch(/Unsupported/);
  });
  it("rejects empty files", () => {
    expect(validateImageFile(file("image/png", 0))).toMatch(/empty/);
  });
  it("rejects files over the cap", () => {
    expect(validateImageFile(file("image/png", DEFAULT_MAX_IMAGE_BYTES + 1))).toMatch(/5 MB/);
  });
});

describe("describeInvalidImageError", () => {
  const widgets = [
    { i: "1", name: "Topology", widgetType: WidgetType.IMAGE, image: { source: "upload", imageId: "img-1", fit: "contain" } },
    { i: "2", name: "Logo", widgetType: WidgetType.IMAGE, image: { source: "upload", imageId: "img-2", fit: "contain" } },
  ] as unknown as WidgetData[];

  it("names the widgets whose images were rejected", () => {
    const error = new KeepApiError("x", "/dashboard", "", { invalid_image_ids: ["img-2"] }, 400);
    expect(describeInvalidImageError(error, widgets)).toBe(
      "Image missing for widget(s): Logo. Re-upload it and save again."
    );
  });
  it("falls back to a generic message when no widget matches", () => {
    const error = new KeepApiError("x", "/dashboard", "", { invalid_image_ids: [] }, 400);
    expect(describeInvalidImageError(error, widgets)).toBe(
      "Some image widgets have no available image. Re-upload and save again."
    );
  });
  it("ignores other errors", () => {
    expect(describeInvalidImageError(new Error("boom"), widgets)).toBeNull();
    const other = new KeepApiError("x", "/dashboard", "", { detail: "no" }, 400);
    expect(describeInvalidImageError(other, widgets)).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd $W/keep-ui && npx jest "src/app/\(keep\)/dashboard/widget-types/image"`
Expected: FAIL (`Cannot find module '../image-widget-validation'`).

- [ ] **Step 3: Implement**

In `types.tsx`:
- Add `IMAGE = "IMAGE",` as the last member of `enum WidgetType`.
- Add these declarations below the enum:

```ts
export type ImageFit = "contain" | "cover";

export interface ImageWidgetConfig {
  source: "upload" | "url";
  imageId?: string;
  url?: string;
  fit: ImageFit;
  link?: string;
}
```

- Add `image?: ImageWidgetConfig;` to `WidgetData`, after `customLink?: string;`.

`widget-types/image/image-widget-validation.ts`:

```ts
import { KeepApiError } from "@/shared/api";
import { WidgetData, WidgetType } from "../../types";

export const ALLOWED_IMAGE_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/svg+xml",
];

export const IMAGE_ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.svg";

export const DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024;

/** True only for absolute http(s) URLs, so javascript: and data: never reach an href or src. */
export function isHttpUrl(value: string): boolean {
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

/** Client-side pre-check mirroring the gateway; returns an error message or null. */
export function validateImageFile(
  file: File,
  maxBytes: number = DEFAULT_MAX_IMAGE_BYTES
): string | null {
  if (!ALLOWED_IMAGE_TYPES.includes(file.type)) {
    return "Unsupported image type (PNG, JPEG, GIF, WEBP, SVG)";
  }
  if (file.size === 0) {
    return "Image is empty";
  }
  if (file.size > maxBytes) {
    return `Image is larger than ${Math.round(maxBytes / (1024 * 1024))} MB`;
  }
  return null;
}

/** Turns the gateway's 400 for unavailable dashboard images into a message naming the widgets. */
export function describeInvalidImageError(
  error: unknown,
  widgets: WidgetData[]
): string | null {
  if (!(error instanceof KeepApiError) || error.statusCode !== 400) {
    return null;
  }
  const ids = error.responseJson?.invalid_image_ids;
  if (!Array.isArray(ids)) {
    return null;
  }
  const names = widgets
    .filter(
      (w) =>
        w.widgetType === WidgetType.IMAGE &&
        w.image?.imageId !== undefined &&
        ids.includes(w.image.imageId)
    )
    .map((w) => w.name);
  return names.length
    ? `Image missing for widget(s): ${names.join(", ")}. Re-upload it and save again.`
    : "Some image widgets have no available image. Re-upload and save again.";
}
```

`src/entities/dashboard-images/model/useDashboardImages.ts`:

```ts
import { useEffect, useState } from "react";
import useSWRImmutable from "swr/immutable";
import { ApiClient } from "@/shared/api";
import { useApi } from "@/shared/lib/hooks/useApi";

export interface DashboardImageUpload {
  id: string;
  name: string;
  content_type: string;
  size_bytes: number;
}

/** Uploads the file as the raw request body; the gateway keeps it pending until a dashboard save claims it. */
export function uploadDashboardImage(
  api: ApiClient,
  file: File
): Promise<DashboardImageUpload> {
  return api.request<DashboardImageUpload>(
    `/dashboard-images?name=${encodeURIComponent(file.name)}`,
    {
      method: "POST",
      body: file,
      headers: { "Content-Type": file.type },
    }
  );
}

/** Fetches image bytes with the session's auth headers; rejects on any non-2xx. */
export async function fetchDashboardImageBlob(
  api: ApiClient,
  imageId: string
): Promise<Blob> {
  const response = await fetch(
    `${api.getApiBaseUrl()}/dashboard-images/${encodeURIComponent(imageId)}`,
    { headers: api.getHeaders() as HeadersInit }
  );
  if (!response.ok) {
    throw new Error(`Image request failed with ${response.status}`);
  }
  return response.blob();
}

/** Object URL for an uploaded image; the URL is revoked when the caller unmounts or the image changes. */
export function useDashboardImage(imageId?: string) {
  const api = useApi();
  const { data: blob, error, isLoading } = useSWRImmutable(
    api.isReady() && imageId ? ["dashboard-image", imageId] : null,
    () => fetchDashboardImageBlob(api, imageId as string)
  );
  const [url, setUrl] = useState<string>();

  useEffect(() => {
    if (!blob) {
      setUrl(undefined);
      return;
    }
    const objectUrl = URL.createObjectURL(blob);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [blob]);

  return { url, error, isLoading };
}
```

- [ ] **Step 4: Run the tests and typecheck**

Run: `npx jest "src/app/\(keep\)/dashboard/widget-types/image" && npm run typecheck`
Expected: tests PASS; the typecheck error count equals the Task 0 baseline.

- [ ] **Step 5: Commit**

```bash
git add "src/app/(keep)/dashboard/types.tsx" "src/app/(keep)/dashboard/widget-types/image" src/entities/dashboard-images
git commit -m "feat: add image widget types, validation and image data access

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: UI — image renderer and grid wiring

**Files:**
- Create: `keep-ui/src/app/(keep)/dashboard/widget-types/image/image-grid-item.tsx`
- Modify: `keep-ui/src/app/(keep)/dashboard/GridItem.tsx`
- Test: `keep-ui/src/app/(keep)/dashboard/widget-types/image/__tests__/image-grid-item.test.tsx`

**Interfaces:**
- Consumes: `ImageWidgetConfig`, `WidgetData`, `WidgetType` and `isHttpUrl` (Task 6), and `useDashboardImage` (Task 6).
- Produces:
  - `DashboardImageView({ image, alt }: { image: ImageWidgetConfig; alt: string })`, a named export
  - `ImageGridItem({ item }: { item: WidgetData })`, the default export

- [ ] **Step 1: Write the failing test**

```tsx
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { ImageWidgetConfig } from "../../../types";
import { DashboardImageView } from "../image-grid-item";

const mockUseDashboardImage = jest.fn();
jest.mock("@/entities/dashboard-images/model/useDashboardImages", () => ({
  useDashboardImage: (id?: string) => mockUseDashboardImage(id),
}));

beforeEach(() => {
  mockUseDashboardImage.mockReset();
  mockUseDashboardImage.mockReturnValue({ url: undefined, error: undefined, isLoading: false });
});

const upload: ImageWidgetConfig = { source: "upload", imageId: "img-1", fit: "cover" };
const byUrl: ImageWidgetConfig = { source: "url", url: "https://x.io/a.png", fit: "contain" };

describe("DashboardImageView", () => {
  it("renders an uploaded image from its blob URL with the chosen fit", () => {
    mockUseDashboardImage.mockReturnValue({ url: "blob:abc", error: undefined, isLoading: false });
    render(<DashboardImageView image={upload} alt="Topology" />);
    const img = screen.getByRole("img", { name: "Topology" });
    expect(img).toHaveAttribute("src", "blob:abc");
    expect(img).toHaveStyle({ objectFit: "cover" });
    expect(mockUseDashboardImage).toHaveBeenCalledWith("img-1");
  });

  it("does not fetch for URL images", () => {
    render(<DashboardImageView image={byUrl} alt="Logo" />);
    expect(screen.getByRole("img", { name: "Logo" })).toHaveAttribute("src", "https://x.io/a.png");
    expect(mockUseDashboardImage).toHaveBeenCalledWith(undefined);
  });

  it("shows the placeholder when the upload fetch fails", () => {
    mockUseDashboardImage.mockReturnValue({ url: undefined, error: new Error("404"), isLoading: false });
    render(<DashboardImageView image={upload} alt="Topology" />);
    expect(screen.getByText("Image unavailable")).toBeInTheDocument();
  });

  it("shows the placeholder when a URL image fails to load", () => {
    render(<DashboardImageView image={byUrl} alt="Logo" />);
    fireEvent.error(screen.getByRole("img", { name: "Logo" }));
    expect(screen.getByText("Image unavailable")).toBeInTheDocument();
  });

  it("never renders a non-http URL", () => {
    render(<DashboardImageView image={{ ...byUrl, url: "javascript:alert(1)" }} alt="Bad" />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("Image unavailable")).toBeInTheDocument();
  });

  it("wraps the image in a safe new-tab link when a link is set", () => {
    render(<DashboardImageView image={{ ...byUrl, link: "https://wiki/runbook" }} alt="Logo" />);
    const anchor = screen.getByRole("link");
    expect(anchor).toHaveAttribute("href", "https://wiki/runbook");
    expect(anchor).toHaveAttribute("target", "_blank");
    expect(anchor).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("drops a non-http link", () => {
    render(<DashboardImageView image={{ ...byUrl, link: "javascript:alert(1)" }} alt="Logo" />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx jest "src/app/\(keep\)/dashboard/widget-types/image/__tests__/image-grid-item"`
Expected: FAIL (`Cannot find module '../image-grid-item'`).

- [ ] **Step 3: Implement**

`widget-types/image/image-grid-item.tsx`:

```tsx
import React, { useEffect, useState } from "react";
import Skeleton from "react-loading-skeleton";
import "react-loading-skeleton/dist/skeleton.css";
import { useDashboardImage } from "@/entities/dashboard-images/model/useDashboardImages";
import { ImageWidgetConfig, WidgetData } from "../../types";
import { isHttpUrl } from "./image-widget-validation";

/** Renders an image widget's picture, or an in-card placeholder when it cannot be shown. */
export function DashboardImageView({
  image,
  alt,
}: {
  image: ImageWidgetConfig;
  alt: string;
}) {
  const isUpload = image.source === "upload";
  const { url: blobUrl, error, isLoading } = useDashboardImage(
    isUpload ? image.imageId : undefined
  );
  const [broken, setBroken] = useState(false);
  const src = isUpload
    ? blobUrl
    : image.url && isHttpUrl(image.url)
      ? image.url
      : undefined;

  useEffect(() => setBroken(false), [src]);

  if (isUpload && isLoading) {
    return <Skeleton containerClassName="block h-full w-full" className="h-full" />;
  }
  if (!src || error || broken) {
    return (
      <div
        className="flex h-full w-full items-center justify-center text-sm text-gray-400"
        data-cy="dashboard-widget-image-unavailable"
      >
        Image unavailable
      </div>
    );
  }

  const picture = (
    <img
      src={src}
      alt={alt}
      onError={() => setBroken(true)}
      className="h-full w-full"
      style={{ objectFit: image.fit }}
      data-cy="dashboard-widget-image"
    />
  );
  const href = image.link && isHttpUrl(image.link) ? image.link : undefined;
  return href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" className="block h-full w-full">
      {picture}
    </a>
  ) : (
    picture
  );
}

export default function ImageGridItem({ item }: { item: WidgetData }) {
  if (!item.image) {
    return null;
  }
  return (
    <div className="mt-2 min-h-0 flex-1" data-cy="dashboard-widget-image-panel">
      <DashboardImageView image={item.image} alt={item.name} />
    </div>
  );
}
```

In `GridItem.tsx`:
- Add the import `import ImageGridItem from "./widget-types/image/image-grid-item";`
- After the `SERVICE_NOW` block, add:

```tsx
        {item.widgetType === WidgetType.IMAGE && <ImageGridItem item={item} />}
```

- [ ] **Step 4: Run the tests and typecheck**

Run: `npx jest "src/app/\(keep\)/dashboard/widget-types/image" && npm run typecheck`
Expected: PASS; no new type errors.

- [ ] **Step 5: Commit**

```bash
git add "src/app/(keep)/dashboard/widget-types/image" "src/app/(keep)/dashboard/GridItem.tsx"
git commit -m "feat: render dashboard image widgets

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: UI — image widget form, modal wiring, save-error toast

**Files:**
- Create: `keep-ui/src/app/(keep)/dashboard/widget-types/image/image-widget-form.tsx`
- Modify: `keep-ui/src/app/(keep)/dashboard/WidgetModal.tsx`
- Modify: `keep-ui/src/app/(keep)/dashboard/[id]/dashboard.tsx` (`handleSaveDashboard` catch block + import)
- Test: `keep-ui/src/app/(keep)/dashboard/widget-types/image/__tests__/image-widget-form.test.tsx`

**Interfaces:**
- Consumes:
  - from Task 6: `uploadDashboardImage`, `validateImageFile`, `isHttpUrl`, `IMAGE_ACCEPT`, `describeInvalidImageError`, `ImageWidgetConfig`, `ImageFit`
  - from Task 7: `DashboardImageView`
- Produces: `ImageWidgetForm({ editingItem, onChange }: { editingItem?: WidgetData | null; onChange: (formValue: Partial<WidgetData>, isValid: boolean) => void })`

- [ ] **Step 1: Write the failing test**

```tsx
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WidgetData, WidgetType } from "../../../types";
import { ImageWidgetForm } from "../image-widget-form";

const mockUpload = jest.fn();
jest.mock("@/entities/dashboard-images/model/useDashboardImages", () => ({
  uploadDashboardImage: (...args: unknown[]) => mockUpload(...args),
  useDashboardImage: () => ({ url: undefined, error: undefined, isLoading: false }),
}));
jest.mock("@/shared/lib/hooks/useApi", () => ({ useApi: () => ({}) }));

function lastCall(onChange: jest.Mock) {
  return onChange.mock.calls[onChange.mock.calls.length - 1];
}

function pick(fileToPick: File) {
  fireEvent.change(screen.getByLabelText("Image file"), {
    target: { files: [fileToPick] },
  });
}

const png = new File([new Uint8Array(10)], "a.png", { type: "image/png" });

beforeEach(() => mockUpload.mockReset());

describe("ImageWidgetForm", () => {
  it("is invalid until an upload completes", async () => {
    let resolve: (v: unknown) => void = () => {};
    mockUpload.mockReturnValue(new Promise((r) => (resolve = r)));
    const onChange = jest.fn();
    render(<ImageWidgetForm onChange={onChange} />);
    expect(lastCall(onChange)[1]).toBe(false);

    pick(png);
    await waitFor(() => expect(screen.getByText("Uploading…")).toBeInTheDocument());
    expect(lastCall(onChange)[1]).toBe(false);

    await act(async () => resolve({ id: "img-1", name: "a.png", content_type: "image/png", size_bytes: 10 }));
    await waitFor(() => expect(lastCall(onChange)[1]).toBe(true));
    expect(lastCall(onChange)[0]).toMatchObject({
      w: 4,
      h: 4,
      image: { source: "upload", imageId: "img-1", fit: "contain" },
    });
  });

  it("rejects an invalid file without uploading", () => {
    const onChange = jest.fn();
    render(<ImageWidgetForm onChange={onChange} />);
    pick(new File([new Uint8Array(10)], "a.bmp", { type: "image/bmp" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Unsupported image type");
    expect(mockUpload).not.toHaveBeenCalled();
    expect(lastCall(onChange)[1]).toBe(false);
  });

  it("shows the server error with a retry that re-uploads", async () => {
    mockUpload.mockRejectedValueOnce(new Error("Too many unsaved images"));
    mockUpload.mockResolvedValueOnce({ id: "img-2", name: "a.png", content_type: "image/png", size_bytes: 10 });
    const onChange = jest.fn();
    render(<ImageWidgetForm onChange={onChange} />);
    pick(png);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Too many unsaved images"));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(lastCall(onChange)[1]).toBe(true));
    expect(mockUpload).toHaveBeenCalledTimes(2);
  });

  it("keeps an existing upload valid when editing, without layout fields", () => {
    const editingItem = {
      i: "w-1",
      name: "Topology",
      widgetType: WidgetType.IMAGE,
      image: { source: "upload", imageId: "img-9", fit: "cover" },
    } as unknown as WidgetData;
    const onChange = jest.fn();
    render(<ImageWidgetForm editingItem={editingItem} onChange={onChange} />);
    const [value, isValid] = lastCall(onChange);
    expect(isValid).toBe(true);
    expect(value).toEqual({ image: { source: "upload", imageId: "img-9", fit: "cover" } });
  });

  it("invalidates a non-http link", () => {
    const editingItem = {
      i: "w-1",
      name: "Logo",
      widgetType: WidgetType.IMAGE,
      image: { source: "url", url: "https://x.io/a.png", fit: "contain", link: "javascript:alert(1)" },
    } as unknown as WidgetData;
    const onChange = jest.fn();
    render(<ImageWidgetForm editingItem={editingItem} onChange={onChange} />);
    expect(lastCall(onChange)[1]).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx jest "src/app/\(keep\)/dashboard/widget-types/image/__tests__/image-widget-form"`
Expected: FAIL (`Cannot find module '../image-widget-form'`).

- [ ] **Step 3: Implement the form**

`widget-types/image/image-widget-form.tsx`:

```tsx
import React, { useEffect, useState } from "react";
import { Button, Select, SelectItem, Subtitle, TextInput } from "@tremor/react";
import { useApi } from "@/shared/lib/hooks/useApi";
import { uploadDashboardImage } from "@/entities/dashboard-images/model/useDashboardImages";
import { ImageFit, ImageWidgetConfig, LayoutItem, WidgetData } from "../../types";
import { DashboardImageView } from "./image-grid-item";
import { IMAGE_ACCEPT, isHttpUrl, validateImageFile } from "./image-widget-validation";

type ImageSource = ImageWidgetConfig["source"];

const NEW_WIDGET_LAYOUT: Partial<LayoutItem> = {
  w: 4,
  h: 4,
  minW: 0,
  minH: 2,
  static: false,
};

/** Image widget settings: upload (stored in Keep) or URL, fit mode and an optional link. */
export function ImageWidgetForm({
  editingItem,
  onChange,
}: {
  editingItem?: WidgetData | null;
  onChange: (formValue: Partial<WidgetData>, isValid: boolean) => void;
}) {
  const api = useApi();
  const initial = editingItem?.image;
  const [source, setSource] = useState<ImageSource>(initial?.source ?? "upload");
  const [imageId, setImageId] = useState<string | undefined>(initial?.imageId);
  const [uploadedName, setUploadedName] = useState<string>();
  const [url, setUrl] = useState(initial?.url ?? "");
  const [fit, setFit] = useState<ImageFit>(initial?.fit ?? "contain");
  const [link, setLink] = useState(initial?.link ?? "");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [retryFile, setRetryFile] = useState<File | null>(null);

  const urlError =
    source === "url" && url !== "" && !isHttpUrl(url)
      ? "URL must start with http:// or https://"
      : null;
  const linkError =
    link !== "" && !isHttpUrl(link)
      ? "Link must start with http:// or https://"
      : null;
  const isValid =
    !uploading &&
    !linkError &&
    (source === "upload" ? !!imageId : url !== "" && !urlError);

  const image: ImageWidgetConfig =
    source === "upload"
      ? { source, imageId, fit, ...(link ? { link } : {}) }
      : { source, url, fit, ...(link ? { link } : {}) };

  useEffect(() => {
    onChange({ ...(editingItem ? {} : NEW_WIDGET_LAYOUT), image }, isValid);
  }, [source, imageId, url, fit, link, isValid]);

  async function upload(file: File) {
    const problem = validateImageFile(file);
    if (problem) {
      setUploadError(problem);
      setRetryFile(null);
      return;
    }
    setUploading(true);
    setUploadError(null);
    setRetryFile(file);
    setImageId(undefined);
    try {
      const result = await uploadDashboardImage(api, file);
      setImageId(result.id);
      setUploadedName(result.name);
      setRetryFile(null);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  const showPreview = source === "upload" ? !!imageId : url !== "" && !urlError;

  return (
    <div data-cy="dashboard-widget-form-image">
      <div className="mb-4 mt-2">
        <Subtitle>Image Source</Subtitle>
        <Select
          value={source}
          onValueChange={(value) => setSource(value as ImageSource)}
          data-cy="dashboard-widget-form-image-source-select"
        >
          <SelectItem value="upload">Upload</SelectItem>
          <SelectItem value="url">URL</SelectItem>
        </Select>
      </div>

      {source === "upload" ? (
        <div className="mb-4 mt-2">
          <Subtitle>Image File (PNG, JPEG, GIF, WEBP, SVG — up to 5 MB)</Subtitle>
          <input
            type="file"
            accept={IMAGE_ACCEPT}
            aria-label="Image file"
            className="mt-1 block w-full text-sm"
            data-cy="dashboard-widget-form-image-file-input"
            onChange={(event) => {
              const picked = event.target.files?.[0];
              event.target.value = "";
              if (picked) {
                void upload(picked);
              }
            }}
          />
          {uploading && <p className="mt-1 text-sm text-gray-500">Uploading…</p>}
          {!uploading && uploadedName && (
            <p className="mt-1 text-sm text-gray-500">Uploaded {uploadedName}</p>
          )}
          {uploadError && (
            <div className="mt-1 flex items-center gap-2">
              <p role="alert" className="text-sm text-red-600">
                {uploadError}
              </p>
              {retryFile && !uploading && (
                <Button
                  type="button"
                  size="xs"
                  variant="secondary"
                  color="orange"
                  onClick={() => void upload(retryFile)}
                >
                  Retry
                </Button>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="mb-4 mt-2">
          <Subtitle>Image URL</Subtitle>
          <TextInput
            value={url}
            onValueChange={setUrl}
            placeholder="https://intranet.example.com/diagram.svg"
            error={!!urlError}
            errorMessage={urlError ?? undefined}
            data-cy="dashboard-widget-form-image-url-input"
          />
        </div>
      )}

      <div className="mb-4 mt-2">
        <Subtitle>Fit</Subtitle>
        <Select
          value={fit}
          onValueChange={(value) => setFit(value as ImageFit)}
          data-cy="dashboard-widget-form-image-fit-select"
        >
          <SelectItem value="contain">Contain (show whole image)</SelectItem>
          <SelectItem value="cover">Cover (fill, may crop)</SelectItem>
        </Select>
      </div>

      <div className="mb-4 mt-2">
        <Subtitle>Link (optional)</Subtitle>
        <TextInput
          value={link}
          onValueChange={setLink}
          placeholder="https://example.com"
          error={!!linkError}
          errorMessage={linkError ?? undefined}
          data-cy="dashboard-widget-form-image-link-input"
        />
      </div>

      {showPreview && (
        <div className="mb-4 h-40 rounded border p-1">
          <DashboardImageView image={image} alt="Preview" />
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Wire the form into `WidgetModal.tsx`**

- Add the import `import { ImageWidgetForm } from "./widget-types/image/image-widget-form";`
- In the type list, add `{ key: WidgetType.IMAGE, value: "Image" },` after `{ key: WidgetType.METRIC, value: "Metric" },`.
- After the `SERVICE_NOW` form block, add:

```tsx
        {widgetType === WidgetType.IMAGE && (
          <ImageWidgetForm
            editingItem={editingItem}
            onChange={(formValue, isValid) =>
              setInnerFormState({ formValue, isValid })
            }
          />
        )}
```

- [ ] **Step 5: Name the widgets in the save-error toast in `[id]/dashboard.tsx`**

- Add the import `import { describeInvalidImageError } from "../widget-types/image/image-widget-validation";`
- In `handleSaveDashboard`'s `catch`, replace `showErrorToast(error, "Failed to save dashboard");` with:

```tsx
      showErrorToast(
        error,
        describeInvalidImageError(error, widgetData) ?? "Failed to save dashboard"
      );
```

- [ ] **Step 6: Run the tests, typecheck and lint**

Run: `npx jest "src/app/\(keep\)/dashboard" && npm run typecheck && npm run lint`
Expected:
- All image tests PASS.
- Typecheck shows no new errors.
- Lint shows no new *errors*. Warnings of the same kinds that already exist in sibling widget forms, such as `react-hooks/exhaustive-deps` and `@next/next/no-img-element`, are acceptable; do not silence them with inline disable comments.

- [ ] **Step 7: Commit**

```bash
git add "src/app/(keep)/dashboard/widget-types/image" "src/app/(keep)/dashboard/WidgetModal.tsx" "src/app/(keep)/dashboard/[id]/dashboard.tsx"
git commit -m "feat: add image widget form and name unavailable images on save

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Whole-feature verification

**Files:** none changed unless a check fails. Fixes go back into the owning task's files, with their own commit.

- [ ] **Step 1: Full suites**

```bash
cd $W/keep-migrations && PYTHONPATH=. poetry run pytest -q
cd $W/keep-api-gateway && PYTHONPATH=. poetry run pytest tests/ --timeout 60 -q
cd $W/keep-ui && npx jest && npm run typecheck && npm run lint
```

Expected: every suite matches its Task 0 baseline plus the new tests, with no new failures. If a gateway failure also fails on an untouched `origin/dev` worktree, record it as pre-existing rather than fixing it here.

- [ ] **Step 2: End-to-end sanity**

Invoke the `sanity-check` skill with a worktree root holding `keep-api-gateway` and `keep-ui` from `$W`, plus `keep-event-handler` and `keep-workflows` worktrees at `origin/dev`. Apply `$W/keep-migrations` to the sanity database before the gateway starts (the gateway no longer migrates itself).
Expected: the dashboard checks pass, and no check regresses against a dev-only run.

- [ ] **Step 3: Manual pass (noauth local stack)**

1. Create a dashboard. Add an Image widget, upload a PNG, pick Cover, and set a link. Add a second Image widget with an SVG upload, and a third with a URL. Save.
2. Reload the page. All three render. Clicking the first opens its link in a new tab.
3. In Postgres, run `SELECT id, dashboard_id, content_type, size_bytes FROM dashboardimage;`. It shows two rows, both with the dashboard's id.
4. Delete the SVG widget and save. Its row is gone.
5. Open the Add Widget modal, upload an image, and cancel. A new row with `dashboard_id IS NULL` exists (it's swept after 24 hours).
6. Delete the dashboard. All of its rows are gone.
7. Open `GET /dashboard-images/<id>` for an SVG directly in the browser. The response carries `Content-Security-Policy: sandbox`.

- [ ] **Step 4: Report**

Summarize for the user:
- per-repo commit lists (`git log --oneline origin/dev..HEAD` in each worktree)
- suite counts against the baseline
- the sanity result
- any pre-existing failures

Nothing is pushed.
