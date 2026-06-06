"""SQLite DDL for source lineage tables (k2g_raw_document / k2g_segment_blob / purge_audit).

Column order and names are identical to the PostgreSQL schema; only dialect
differences apply:
- ``TEXT`` (sqlite) vs ``JSONB`` (postgres) — locator etc. serialized as JSON text
- ``DATETIME`` (sqlite) vs ``TIMESTAMPTZ`` (postgres)
- ``INTEGER PRIMARY KEY AUTOINCREMENT`` (sqlite) vs ``BIGSERIAL`` (postgres)
"""
from __future__ import annotations


_CREATE_RAW_DOCUMENT_SQL = """
CREATE TABLE IF NOT EXISTS k2g_raw_document (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    source_uri      TEXT,
    title           TEXT,
    content         TEXT NOT NULL,
    byte_size       INTEGER,
    sha256          TEXT,
    ingested_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    -- BP-47 RLS (visibility enum: 'public'|'private'|'shared'|'tombstoned')
    owner_id        TEXT,
    org_id          TEXT,
    visibility      TEXT NOT NULL DEFAULT 'public',
    acl_json        TEXT,
    share_group_id  TEXT,
    -- BP-50 §5 — RLS-based logical purge
    tombstoned_at       DATETIME,
    tombstoned_reason   TEXT,
    tombstoned_by       TEXT
)
"""


_CREATE_SEGMENT_BLOB_SQL = """
CREATE TABLE IF NOT EXISTS k2g_segment_blob (
    id              TEXT PRIMARY KEY,
    raw_document_id TEXT,                             -- FK to k2g_raw_document.id, nullable
    order_index     INTEGER NOT NULL,
    byte_start      INTEGER,
    byte_end        INTEGER,
    content         TEXT NOT NULL,
    -- BP-47 RLS
    owner_id        TEXT,
    org_id          TEXT,
    visibility      TEXT NOT NULL DEFAULT 'public',
    acl_json        TEXT,
    share_group_id  TEXT,
    -- BP-50 §5 — RLS-based logical purge
    tombstoned_at       DATETIME,
    tombstoned_reason   TEXT,
    tombstoned_by       TEXT,
    UNIQUE (raw_document_id, order_index)
)
"""


_CREATE_PURGE_AUDIT_SQL = """
CREATE TABLE IF NOT EXISTS k2g_source_purge_audit (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_provider          TEXT NOT NULL,
    source_id                TEXT NOT NULL,
    purged_at                DATETIME DEFAULT CURRENT_TIMESTAMP,
    actor_id                 TEXT,
    reason                   TEXT,
    affected_events_count    INTEGER,
    affected_entities_count  INTEGER,
    affected_segments_count  INTEGER
)
"""


_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS k2g_raw_document_domain_idx ON k2g_raw_document (domain)",
    "CREATE INDEX IF NOT EXISTS k2g_raw_document_owner_idx ON k2g_raw_document (owner_id)",
    "CREATE INDEX IF NOT EXISTS k2g_raw_document_visibility_idx ON k2g_raw_document (visibility)",
    "CREATE INDEX IF NOT EXISTS k2g_segment_blob_doc_idx ON k2g_segment_blob (raw_document_id, order_index)",
    "CREATE INDEX IF NOT EXISTS k2g_segment_blob_owner_idx ON k2g_segment_blob (owner_id)",
    "CREATE INDEX IF NOT EXISTS k2g_segment_blob_visibility_idx ON k2g_segment_blob (visibility)",
    "CREATE INDEX IF NOT EXISTS k2g_source_purge_audit_lookup ON k2g_source_purge_audit (source_provider, source_id)",
]


def setup_source_lineage_schema_sqlite(cur) -> None:
    """Create the 3 source-lineage tables and their indexes on a SQLite cursor.

    Uses the same idempotent pattern as the covenant and share-group schema
    helpers. Called from the main ``setup_schema`` routine.
    """
    cur.execute(_CREATE_RAW_DOCUMENT_SQL)
    cur.execute(_CREATE_SEGMENT_BLOB_SQL)
    cur.execute(_CREATE_PURGE_AUDIT_SQL)
    for sql in _CREATE_INDEXES_SQL:
        cur.execute(sql)


__all__ = ["setup_source_lineage_schema_sqlite"]
