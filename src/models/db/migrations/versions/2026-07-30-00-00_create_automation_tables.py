"""create the automation control-plane tables in the keep database

Spec §4.4 (rev 4 / CAPP): `automations`, `automation_runs`,
`automation_revisions` live in the **`keep` database's `public` schema** — the
same database and schema as `alert`, `incident`, `preset` and `tenant`. One
database means one Alembic lineage, so the automation schema is owned by
keep-api-gateway's lineage even though `keep-automation-api` remains the sole
*writer* of the rows and keeps the ORM models.

Co-location is the whole point: Postgres has no cross-database foreign keys, so
`tenant_id` can only be an **enforced FK** while these tables share a database
with `tenant`. `automations.tenant_id` and `automation_runs.tenant_id` are
therefore NOT NULL FKs to `tenant.id`. `automation_revisions` deliberately has
**no** `tenant_id`: a revision is only ever reached through its parent
automation, so `automation_id -> automations.tenant_id` already scopes it.

CREATE-FRESH, NOT A DATA MIGRATION. These tables previously existed only in a
local-only `automations` database (keep-automation-api's private Alembic
lineage, revision `a1c0ffee0001`) that was never applied to any shared
environment. There is nothing to copy: no cross-database data migration is
performed here, and none is needed. Do not assume rows were carried over.

No cascade-delete FKs anywhere: rows persist forever, delete is a state flip
(§4.4).

Enum string values are pinned in automation-contracts.md §"DB enums".

Revision ID: create_automation_tables
Revises: drop_commentmention_audit_fk
Create Date: 2026-07-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "create_automation_tables"
down_revision = "drop_commentmention_audit_fk"
branch_labels = None
depends_on = None


def _jsonb():
    """JSONB on Postgres, JSON elsewhere (SQLite test engine)."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


# Native PG enum types. Declaring the members as literal lowercase strings is
# what makes the DB store the enum *values* (`inactive`, `build_failed`, ...)
# rather than the Python member names — the same result keep-automation-api's
# ORM gets via `values_callable=lambda e: [m.value for m in e]`. The values are
# the contract; the Python names are not.
matching_state = sa.Enum(
    "inactive", "active", "deleting", "deleted", name="automation_matching_state"
)
build_state = sa.Enum("idle", "building", "build_failed", name="automation_build_state")
run_state = sa.Enum(
    "pending",
    "submitted",
    "succeeded",
    "failed",
    "stalled",
    "suppressed",
    name="automation_run_state",
)
suppression_reason = sa.Enum(
    "duplicate", "cooldown", name="automation_suppression_reason"
)
failure_class = sa.Enum(
    "transient",
    "permanent",
    "deadline",
    "unclassified",
    "infra",
    "terminated_by_deletion",
    name="automation_failure_class",
)
revision_action = sa.Enum(
    "create", "edit", "enable", "disable", "delete", name="automation_revision_action"
)

ENUM_TYPES = (
    matching_state,
    build_state,
    run_state,
    suppression_reason,
    failure_class,
    revision_action,
)


def upgrade() -> None:
    # Enum types are created implicitly by the first create_table referencing
    # them; safe because the whole migration runs in one transaction (Postgres
    # transactional DDL), so a partial failure rolls the types back too.
    op.create_table(
        "automations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Owning tenant. NOT NULL + a real FK (not an advisory column) — this is
        # what makes match/author/read tenant scoping enforceable downstream.
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Holds a CAPP wallet_name; kept named `namespace` for continuity (§4.4).
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("capp_deployment_id", sa.Text(), nullable=True),
        sa.Column("deployment_url", sa.Text(), nullable=True),
        sa.Column("triggers", _jsonb(), nullable=False),
        sa.Column("script_path", sa.Text(), nullable=False),
        sa.Column("secret_name", sa.Text(), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("cooldown_fields", _jsonb(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "grace_seconds",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("300"),
        ),
        sa.Column("logstash_url", sa.Text(), nullable=True),
        sa.Column(
            "matching_state",
            matching_state,
            nullable=False,
            server_default="inactive",
        ),
        sa.Column("build_state", build_state, nullable=False, server_default="idle"),
        sa.Column("active_digest", sa.Text(), nullable=True),
        sa.Column("building_sha", sa.Text(), nullable=True),
        sa.Column("contract_version", sa.Text(), nullable=True),
        sa.Column("build_lock_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_cascade_step", sa.SmallInteger(), nullable=True),
        sa.Column(
            "index_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The hydration index. `matching_state` LEADS on purpose: the matcher loads
    # every tenant in a single `WHERE matching_state = 'active'` pass (one query
    # per reload interval, not one per tenant), so a tenant-leading index could
    # not serve it. `tenant_id` rides second for grouped output and an optional
    # single-tenant reload — NOT to make the read covering, which it is not: the
    # matcher also reads `triggers`, `cooldown_fields`, `cooldown_seconds` and
    # `grace_seconds`, so every row is a heap fetch either way.
    op.create_index(
        "ix_automations_matching_state_tenant_id",
        "automations",
        ["matching_state", "tenant_id"],
    )
    # Stuck-build reconciler branch — deliberately cross-tenant (a repair scan,
    # not a user read).
    op.create_index(
        "ix_automations_build_state_lock",
        "automations",
        ["build_state", "build_lock_deadline"],
    )
    # Deboard fan-out.
    op.create_index(
        "ix_automations_tenant_id_namespace",
        "automations",
        ["tenant_id", "namespace"],
    )
    # Authoring list path.
    op.create_index(
        "ix_automations_tenant_id_created_at",
        "automations",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "automation_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        # No ON DELETE cascade: rows persist forever, delete is a state flip (§4.4).
        sa.Column(
            "automation_id",
            sa.Uuid(),
            sa.ForeignKey("automations.id"),
            nullable=False,
        ),
        # Denormalized from the parent automation (always equal to it) so a
        # history read is tenant-filtered at the row it returns, rather than
        # trusting a join back to `automations` to have been written.
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant.id"),
            nullable=False,
        ),
        sa.Column("history_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("state", run_state, nullable=False, server_default="pending"),
        sa.Column("suppression_reason", suppression_reason, nullable=True),
        sa.Column("gate_flags", _jsonb(), nullable=True),
        sa.Column("automation_digest", sa.Text(), nullable=True),
        sa.Column("matched_m", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=True),
        sa.Column("outcome_status", sa.Text(), nullable=True),
        sa.Column("failure_class", failure_class, nullable=True),
        sa.Column("result", _jsonb(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # The idempotency authority: one row per (alert event, automation).
        # Tenant-less on purpose — `automation_id` already implies the tenant.
        sa.UniqueConstraint(
            "history_id", "automation_id", name="uq_automation_runs_history_automation"
        ),
    )
    # The reconciler's one indexed scan per branch — tenant-less on purpose, an
    # infra repair loop must see every tenant.
    op.create_index(
        "ix_automation_runs_state_created_at",
        "automation_runs",
        ["state", "created_at"],
    )
    # Run-history reads.
    op.create_index(
        "ix_automation_runs_automation_id_created_at",
        "automation_runs",
        ["automation_id", "created_at"],
    )

    op.create_table(
        "automation_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # No `tenant_id` (§4.4, explicit): a revision is only ever reached
        # through its parent automation, so `automations.tenant_id` already
        # scopes every read and a copy here would be a second value to keep true
        # for no query it enables.
        sa.Column(
            "automation_id",
            sa.Uuid(),
            sa.ForeignKey("automations.id"),
            nullable=False,
        ),
        sa.Column("action", revision_action, nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column("resulting_digest", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Revision-history read (`WHERE automation_id = ? ORDER BY created_at`).
    # Postgres does not auto-index FK columns, so without this the history read
    # seq-scans. Free to add while the table is empty; retrofitting it later
    # costs a CREATE INDEX CONCURRENTLY dance. The name is shared with
    # keep-automation-api's ORM model — keep the two identical.
    op.create_index(
        "ix_automation_revisions_automation_id_created_at",
        "automation_revisions",
        ["automation_id", "created_at"],
    )


def downgrade() -> None:
    # Child tables first (both FK `automations.id`), then the parent, then the
    # enum types — which Postgres will not drop while a column still uses them.
    op.drop_table("automation_revisions")
    op.drop_table("automation_runs")
    op.drop_table("automations")
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.drop(bind, checkfirst=True)
