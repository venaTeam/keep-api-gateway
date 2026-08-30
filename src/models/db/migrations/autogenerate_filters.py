"""Autogenerate filters for keep-api-gateway's Alembic lineage.

Lives in its own module (rather than inline in ``env.py``) so it is importable
and therefore testable: ``env.py`` runs the migrations at import time and cannot
be imported outside an Alembic run.

WHY THIS EXISTS — a data-loss footgun
-------------------------------------
The automation control-plane tables (`automations`, `automation_runs`,
`automation_revisions`) live in the `keep` database's `public` schema, and
keep-api-gateway owns their **migration lineage** — one database means one
`alembic_version`, so their revisions land here alongside the rest of the
platform schema.

But keep-api-gateway does **not** own their **ORM models**: those stay in
`keep-automation-api` (`src/models/db/automation*.py`), which remains the sole
writer of the rows. That was a deliberate decision — gateway does not get copies
of these models.

`env.py` sets ``target_metadata = SQLModel.metadata`` after importing every
gateway model, so gateway's metadata will never contain the three automation
tables. To Alembic autogenerate, a table that exists in the database but not in
`target_metadata` looks like a table that should be deleted: the next
``alembic revision --autogenerate`` would happily emit
``op.drop_table("automations")`` and destroy the automation control plane.

``include_object`` tells autogenerate to leave them alone. The cost is that
automation schema changes are **always hand-written revisions** in this repo —
autogenerate can neither create nor alter them. That is intended.
"""

# The automation control-plane tables: migrated here, modelled in
# keep-automation-api. Never let autogenerate reason about them.
AUTOMATION_TABLES = frozenset(
    {
        "automations",
        "automation_runs",
        "automation_revisions",
    }
)

# The native PG enum types those tables' columns use. Reflected as objects of
# type "type" on some setups; excluded for the same reason as the tables.
AUTOMATION_ENUM_TYPES = frozenset(
    {
        "automation_matching_state",
        "automation_build_state",
        "automation_run_state",
        "automation_suppression_reason",
        "automation_failure_class",
        "automation_revision_action",
    }
)


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Alembic ``include_object`` hook: False hides the object from autogenerate.

    Excludes the automation control-plane tables (and their enum types) so a
    ``--autogenerate`` run cannot emit a DROP for schema this repo migrates but
    does not model. Everything else is included, i.e. behaviour is unchanged for
    every gateway-owned object.
    """
    if type_ == "table" and name in AUTOMATION_TABLES:
        return False
    if type_ == "type" and name in AUTOMATION_ENUM_TYPES:
        return False
    # A column reflected off one of those tables must be hidden too — Alembic
    # only skips a table's columns automatically when the table itself is in
    # target_metadata.
    if (
        type_ == "column"
        and getattr(getattr(object_, "table", None), "name", None)
        in AUTOMATION_TABLES
    ):
        return False
    return True
