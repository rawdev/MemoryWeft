"""Auto-ALTER the events table to add source_* columns.

Follows the same pattern as ``apply_data_owner_alters_*``
(``src/k2g/security/data_owner.py``). Called idempotently from
``setup_schema``.

Columns added to events:
- source_provider     TEXT          — provider name ('text_blob_postgres', etc.)
- source_id           TEXT          — provider-specific source identifier
- source_locator      TEXT/JSONB    — location within the source (provider-specific schema)
- source_version      TEXT          — external backend row version (optional)
- source_tombstoned_at TIMESTAMPTZ  — lazy detection of external source removal
- tombstoned_at       TIMESTAMPTZ  — internal logical purge (GDPR etc.)
- tombstoned_reason   TEXT
- tombstoned_by       TEXT          — actor_id

Design: ``BP-50`` §3.1.
"""
from __future__ import annotations


# (column_name, sqlite_type, postgres_type) — 8 columns
SOURCE_LINEAGE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("source_provider",      "TEXT",     "TEXT"),
    ("source_id",            "TEXT",     "TEXT"),
    ("source_locator",       "TEXT",     "JSONB"),
    ("source_version",       "TEXT",     "TEXT"),
    ("source_tombstoned_at", "DATETIME", "TIMESTAMPTZ"),
    ("tombstoned_at",        "DATETIME", "TIMESTAMPTZ"),
    ("tombstoned_reason",    "TEXT",     "TEXT"),
    ("tombstoned_by",        "TEXT",     "TEXT"),
)


def apply_source_lineage_alters_sqlite(cur) -> int:
    """Add the 8 source-lineage columns to the SQLite events table (idempotent).

    SQLite does not support ``ADD COLUMN IF NOT EXISTS``, so existing
    columns are checked first.
    Returns:
        Number of columns actually added.
    """
    # Fetch existing column list for the events table
    cur.execute("PRAGMA table_info(events)")
    existing = {row[1] for row in cur.fetchall()}      # row[1] = column name
    added = 0
    for col_name, sqlite_type, _pg_type in SOURCE_LINEAGE_COLUMNS:
        if col_name in existing:
            continue
        try:
            cur.execute(f"ALTER TABLE events ADD COLUMN {col_name} {sqlite_type}")
            added += 1
        except Exception:                                # noqa: BLE001
            # Race / partial migration — column already exists
            pass

    # Index on source_* for fast lookup
    try:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS events_source_lookup "
            "ON events (source_provider, source_id)"
        )
    except Exception:                                    # noqa: BLE001
        pass

    return added


def apply_source_lineage_alters_postgres(cur) -> int:
    """Add the 8 source-lineage columns to the Postgres events table (idempotent).

    Uses ``ADD COLUMN IF NOT EXISTS``.
    Returns:
        Number of statements executed (0 or 8; Postgres executes each as a
        single statement).
    """
    statements = postgres_alter_statements()
    added = 0
    for sql in statements:
        try:
            cur.execute(sql)
            added += 1
        except Exception:                                # noqa: BLE001
            pass

    try:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS events_source_lookup "
            "ON events (source_provider, source_id)"
        )
    except Exception:                                    # noqa: BLE001
        pass

    return added


def postgres_alter_statements() -> list[str]:
    """Return the list of ALTER ADD COLUMN IF NOT EXISTS statements (8 total)."""
    return [
        f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {name} {pg_type}"
        for name, _sqlite_type, pg_type in SOURCE_LINEAGE_COLUMNS
    ]


def sqlite_alter_statements() -> list[str]:
    """Return the list of SQLite ALTER TABLE statements (reference only —
    the apply function handles IF NOT EXISTS logic)."""
    return [
        f"ALTER TABLE events ADD COLUMN {name} {sqlite_type}"
        for name, sqlite_type, _pg_type in SOURCE_LINEAGE_COLUMNS
    ]


__all__ = [
    "SOURCE_LINEAGE_COLUMNS",
    "apply_source_lineage_alters_sqlite",
    "apply_source_lineage_alters_postgres",
    "postgres_alter_statements",
    "sqlite_alter_statements",
]
