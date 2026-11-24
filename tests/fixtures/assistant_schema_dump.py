"""Normalized dump of the Keep Assistant schema.

VENDORED BYTE-IDENTICALLY into keep-api-gateway, together with its golden fixture
``assistant_schema.json``, at the same path (``tests/fixtures/``). Neither repo's
CI checks out the other, so each asserts against its own copy: keep-migrations
checks the migration, keep-api-gateway checks its ORM models. Any schema change
updates the migration, the models, and BOTH copies in the same change set.

Two sources, one normal form: ``dump_from_tables`` reads the schema objects a
migration declares, ``dump_from_inspector`` reads a live Postgres. This follows
``automation_schema_dump.py`` and records four things that one does not:

* **Identity** per column: ``null``, or ``{always, start, increment}``. The
  application never writes ``assistant_message.seq``, so losing the identity
  breaks every insert while the column itself looks unchanged.
* **Partial-index predicates**, with casts, parentheses, identifier quotes and
  extra whitespace removed: Postgres reports ``state = 'pending'`` as
  ``(state = 'pending'::text)``. Dropping parentheses is only safe because each
  predicate here is a single comparison; do not add a compound one without
  revisiting ``_canonical_predicate``.
* **CHECK constraints**, as name + sorted string literals: Postgres rewrites
  ``x IN ('a', 'b')`` to ``x = ANY (ARRAY['a'::text, 'b'::text])``, so the text
  is not comparable but the value set is.
* **ON DELETE** per foreign key; the default ``NO ACTION`` is ``null``.

As there: types are compiled for PostgreSQL with TEXT folded into VARCHAR,
server defaults lose casts and quoting, a unique constraint is recorded once
rather than again as its backing index, and every list is sorted because
Postgres guarantees no order.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

#: Anything else in the metadata under test (the `tenant` FK target) stays out.
ASSISTANT_TABLES = ("assistant_conversation", "assistant_draft", "assistant_message")

_PG = postgresql.dialect()
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_CAST = re.compile(r"::\s*(?:character varying|\w+)(?:\(\d+\))?(?:\[\])?")


def _outside_literals(sql: str, transform) -> str:
    """Apply ``transform`` to everything in ``sql`` except its string literals."""
    out, position = [], 0
    for match in _LITERAL.finditer(sql):
        out += [transform(sql[position : match.start()]), match.group(0)]
        position = match.end()
    return "".join([*out, transform(sql[position:])])


def _canonical_type(type_) -> str:
    """Compiled Postgres type name, with TEXT folded into VARCHAR."""
    rendered = type_.compile(_PG)
    return "VARCHAR" if rendered == "TEXT" else rendered


def _sql_text(value) -> str:
    """The SQL of a string, a ``text()``, or any other SQL expression."""
    if isinstance(value, str):
        return value
    if isinstance(value, sa.sql.elements.ClauseElement):
        return str(value.compile(dialect=_PG, compile_kwargs={"literal_binds": True}))
    return str(value)


def _canonical_default(raw) -> str | None:
    """A server default as bare text: no cast suffix, no quotes, no parens."""
    if raw is None or isinstance(raw, sa.Identity):
        return None
    value = _sql_text(getattr(raw, "arg", raw)).strip()
    while True:
        stripped = re.sub(_CAST.pattern + "$", "", value).strip()
        if len(stripped) >= 2 and stripped[0] == "(" and stripped[-1] == ")":
            stripped = stripped[1:-1].strip()
        if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
            stripped = stripped[1:-1]
        if stripped == value:
            return value
        value = stripped


def _canonical_identity(always, start, increment) -> dict:
    """Unset start/increment mean Postgres' defaults, which reflection states."""
    return {
        "always": bool(always),
        "start": 1 if start is None else start,
        "increment": 1 if increment is None else increment,
    }


def _canonical_predicate(raw) -> str | None:
    """A partial-index predicate, as either source spells it."""
    if raw is None:
        return None
    sql = _outside_literals(
        _sql_text(raw), lambda part: re.sub(r'[()"]', " ", _CAST.sub("", part))
    )
    # `alert.state` and `state` are the same column inside one table's index.
    sql = _outside_literals(sql, lambda part: re.sub(r"\b\w+\.(?=\w)", "", part))
    return _outside_literals(sql, lambda part: re.sub(r"\s+", " ", part)).strip()


def _check_entry(name, sqltext) -> dict:
    return {
        "name": name,
        "literals": sorted(
            literal.replace("''", "'") for literal in _LITERAL.findall(_sql_text(sqltext))
        ),
    }


def _column_entry(name, type_, nullable, server_default, identity) -> dict:
    return {
        "name": name,
        "type": _canonical_type(type_),
        "nullable": bool(nullable),
        "server_default": _canonical_default(server_default),
        "identity": identity,
    }


def _index_entry(name, columns, unique, where) -> dict:
    return {
        "name": name,
        "columns": list(columns),
        "unique": bool(unique),
        "where": _canonical_predicate(where),
    }


def _foreign_key_entry(columns, referred_table, referred_columns, ondelete) -> dict:
    ondelete = ondelete.upper() if ondelete else None
    return {
        "constrained_columns": list(columns),
        "referred_table": referred_table,
        "referred_columns": list(referred_columns),
        "ondelete": None if ondelete == "NO ACTION" else ondelete,
    }


def _sorted_table(table: dict) -> dict:
    return {
        "columns": sorted(table["columns"], key=lambda c: c["name"]),
        "primary_key": sorted(table["primary_key"]),
        "indexes": sorted(table["indexes"], key=lambda i: i["name"]),
        "unique_constraints": sorted(
            table["unique_constraints"], key=lambda u: u["name"]
        ),
        "foreign_keys": sorted(
            table["foreign_keys"],
            key=lambda f: (f["constrained_columns"], f["referred_table"]),
        ),
        "check_constraints": sorted(
            table["check_constraints"], key=lambda c: c["name"]
        ),
    }


def dump_from_inspector(inspector, table_names=ASSISTANT_TABLES) -> dict:
    """Dump a live Postgres, as reflected by SQLAlchemy."""
    tables = {}
    for name in sorted(table_names):
        unique_constraints = [
            {"name": uq["name"], "columns": list(uq["column_names"])}
            for uq in inspector.get_unique_constraints(name)
        ]
        backed_by_constraint = {uq["name"] for uq in unique_constraints}
        tables[name] = _sorted_table(
            {
                "columns": [
                    _column_entry(
                        c["name"],
                        c["type"],
                        c["nullable"],
                        c.get("default"),
                        _canonical_identity(
                            c["identity"]["always"],
                            c["identity"]["start"],
                            c["identity"]["increment"],
                        )
                        if c.get("identity")
                        else None,
                    )
                    for c in inspector.get_columns(name)
                ],
                "primary_key": inspector.get_pk_constraint(name)["constrained_columns"],
                "indexes": [
                    _index_entry(
                        ix["name"],
                        ix["column_names"],
                        ix["unique"],
                        ix.get("dialect_options", {}).get("postgresql_where"),
                    )
                    for ix in inspector.get_indexes(name)
                    if ix["name"] not in backed_by_constraint
                ],
                "unique_constraints": unique_constraints,
                "foreign_keys": [
                    _foreign_key_entry(
                        fk["constrained_columns"],
                        fk["referred_table"],
                        fk["referred_columns"],
                        fk.get("options", {}).get("ondelete"),
                    )
                    for fk in inspector.get_foreign_keys(name)
                ],
                "check_constraints": [
                    _check_entry(ck["name"], ck["sqltext"])
                    for ck in inspector.get_check_constraints(name)
                ],
            }
        )
    return {"tables": tables}


def dump_from_tables(tables, indexes, table_names=ASSISTANT_TABLES) -> dict:
    """Dump schema objects a migration declared, without touching a database.

    ``tables`` maps table name -> ``sa.Table``. ``indexes`` is a list of
    ``(index_name, table_name, columns, unique, where)``: ``op.create_index``
    builds its ``Index`` against a throwaway table, so the call's own arguments
    are what gets recorded.
    """
    dumped = {}
    for name in sorted(table_names):
        table = tables[name]
        dumped[name] = _sorted_table(
            {
                "columns": [
                    _column_entry(
                        c.name,
                        c.type,
                        c.nullable,
                        c.server_default,
                        _canonical_identity(
                            c.identity.always, c.identity.start, c.identity.increment
                        )
                        if c.identity is not None
                        else None,
                    )
                    for c in table.columns
                ],
                "primary_key": [c.name for c in table.primary_key.columns],
                "indexes": [
                    _index_entry(index_name, columns, unique, where)
                    for index_name, table_name, columns, unique, where in indexes
                    if table_name == name
                ],
                "unique_constraints": [
                    {"name": uq.name, "columns": [c.name for c in uq.columns]}
                    for uq in table.constraints
                    if isinstance(uq, sa.UniqueConstraint)
                ],
                "foreign_keys": [
                    _foreign_key_entry(
                        fk.column_keys,
                        fk.elements[0].target_fullname.rsplit(".", 1)[0],
                        [e.target_fullname.rsplit(".", 1)[1] for e in fk.elements],
                        fk.ondelete,
                    )
                    for fk in table.foreign_key_constraints
                ],
                "check_constraints": [
                    _check_entry(ck.name, ck.sqltext)
                    for ck in table.constraints
                    if isinstance(ck, sa.CheckConstraint)
                ],
            }
        )
    return {"tables": dumped}
