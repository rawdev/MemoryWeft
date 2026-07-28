"""``denormalize_for_column`` — JSON/JSONB tolerance for Postgres-sourced archives.

An archive exported from a PostgreSQL-backed instance carries *nested* JSON for
every JSONB column: psycopg2 decodes JSONB into a Python ``dict``/``list``, and
``normalize_value`` passes JSON-compatible values straight through.  The
importer must re-serialize those before binding them to a cursor — sqlite3
cannot bind a dict at all, and psycopg2 raises "can't adapt type 'dict'".

Affected columns span 11 tables: ``acl_json`` (x7), ``inline_meta``,
``params_json``, ``metrics_json``, ``entity_ids`` (x2), ``entity_list``,
``expected_entities``, ``source_cg_ids``, ``source_locator``.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from k2g.portable.schema import denormalize_for_column


# --- dict / list re-serialization ----------------------------------------

def test_dict_becomes_json_string() -> None:
    out = denormalize_for_column("acl_json", {"grants": ["u1"], "mode": "ro"})
    assert isinstance(out, str)
    assert json.loads(out) == {"grants": ["u1"], "mode": "ro"}


def test_empty_dict_becomes_empty_object() -> None:
    """``content_store.inline_meta`` is NOT NULL DEFAULT '{}' — an empty dict is
    the single most common value in a hosted export.  It is *falsy*, so a
    truthiness guard (``if v and isinstance(...)``) would let it through raw and
    crash the bind.  This pins the isinstance check."""
    assert denormalize_for_column("inline_meta", {}) == "{}"


def test_empty_list_becomes_empty_array() -> None:
    assert denormalize_for_column("entity_ids", []) == "[]"


def test_list_becomes_json_string() -> None:
    out = denormalize_for_column("entity_ids", ["ent_a", "ent_b"])
    assert json.loads(out) == ["ent_a", "ent_b"]


def test_nested_structure_round_trips() -> None:
    value = {"a": [{"b": 1}, {"c": [2, 3]}], "d": {"e": None}}
    assert json.loads(denormalize_for_column("metrics_json", value)) == value


def test_non_ascii_is_not_escaped() -> None:
    """``ensure_ascii=False`` — Korean/CJK bodies stay readable in the DB."""
    out = denormalize_for_column("inline_meta", {"content": "한글 본문"})
    assert "한글 본문" in out


def test_compact_separators() -> None:
    """``separators=(",", ":")`` — no incidental whitespace."""
    assert denormalize_for_column("acl_json", {"a": 1, "b": 2}) == '{"a":1,"b":2}'


# --- pre-existing behavior must not regress ------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, False), (1, True), ("f", False), ("t", True),
     ("false", False), ("true", True), (False, False), (True, True)],
)
def test_boolean_column_still_coerced(raw: object, expected: bool) -> None:
    assert denormalize_for_column("deprecated", raw) is expected


@pytest.mark.parametrize("col", ["acl_json", "deprecated", "summary", "domain"])
def test_none_passes_through(col: str) -> None:
    assert denormalize_for_column(col, None) is None


@pytest.mark.parametrize("value", ["plain", 42, 3.5, '{"already":"json"}'])
def test_scalar_passes_through_unchanged(value: object) -> None:
    assert denormalize_for_column("summary", value) == value


def test_base64_prefix_still_unwrapped() -> None:
    assert denormalize_for_column("embedding", "base64:aGk=") == b"hi"


# --- sqlite binding (the actual crash) -----------------------------------

def _table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE t ("
        "  id TEXT PRIMARY KEY,"
        "  acl_json TEXT,"
        "  inline_meta TEXT NOT NULL DEFAULT '{}'"
        ")"
    )


def test_raw_dict_is_not_bindable_by_sqlite() -> None:
    """Regression anchor: documents *why* the dict branch exists.

    Without ``denormalize_for_column`` re-serializing, this is exactly the
    failure a Postgres-sourced archive produces on import.  The exact class
    moved across CPython versions (``InterfaceError`` on <=3.10,
    ``ProgrammingError`` on 3.11+), so both are accepted; the message
    ("type 'dict' is not supported") is the stable part.
    """
    conn = sqlite3.connect(":memory:")
    try:
        _table(conn)
        with pytest.raises(
            (sqlite3.InterfaceError, sqlite3.ProgrammingError),
            match="not supported",
        ):
            conn.execute(
                "INSERT INTO t (id, acl_json) VALUES (?, ?)", ("x", {"a": 1}),
            )
    finally:
        conn.close()


def test_denormalized_row_binds_and_round_trips() -> None:
    """A psycopg2-shaped row (dict values for JSONB columns) survives the trip."""
    conn = sqlite3.connect(":memory:")
    try:
        _table(conn)
        cols = ("id", "acl_json", "inline_meta")
        pg_row = ("evt_1", {"grants": ["u1"]}, {"content": "hello"})
        conn.execute(
            "INSERT INTO t (id, acl_json, inline_meta) VALUES (?, ?, ?)",
            tuple(denormalize_for_column(c, v) for c, v in zip(cols, pg_row)),
        )
        stored = conn.execute(
            "SELECT acl_json, inline_meta FROM t WHERE id = 'evt_1'"
        ).fetchone()
        assert json.loads(stored[0]) == {"grants": ["u1"]}
        assert json.loads(stored[1]) == {"content": "hello"}
    finally:
        conn.close()
