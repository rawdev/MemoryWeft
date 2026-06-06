"""
SQLite Content Store
- Same interface as PostgresContentStore
- For local use: runs from a single SQLite file with no server required
- inline_meta stored as TEXT (JSON string)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from k2g.core.models import ContentRecord, new_content_id

logger = logging.getLogger(__name__)


def _coerce_pg_timestamp(v: Any) -> Any:
    """Normalize a Postgres-style timestamp string to ISO-8601 for Pydantic.

    Converts ``'YYYY-MM-DD HH:MM:SS.ffffff+00'`` to ``+00:00`` form.
    Already-datetime values or other formats are returned unchanged.

    When the K2G build pipeline migrates PG → SQLite it inserts the raw
    ``timestamptz::text`` output (``+00`` suffix) directly. Pydantic v2's
    datetime parser rejects the shortened ``+HH`` form (RFC3339/ISO requires
    ``+HH:MM``).
    """
    if not isinstance(v, str):
        return v
    # Trailing ``+00`` or ``-05`` etc. → append ``:00``. ``+0000`` is OK for Pydantic.
    if len(v) >= 3 and v[-3] in "+-" and v[-2:].isdigit():
        return v + ":00"
    return v


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS content_store (
    content_id    TEXT PRIMARY KEY,
    domain        TEXT NOT NULL,
    vector_id     TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    storage_uri   TEXT NOT NULL,
    inline_meta   TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_content_store_domain ON content_store (domain);",
    "CREATE INDEX IF NOT EXISTS idx_content_store_vector_id ON content_store (vector_id);",
    "CREATE INDEX IF NOT EXISTS idx_content_store_created_at ON content_store (created_at DESC);",
]


class SqliteContentStore:
    """SQLite-backed content store.

    Provides the same interface as PostgresContentStore.
    Can be used for local testing without a PostgreSQL server.
    """

    def __init__(self, db_path: str | Path = "k2g_content.db") -> None:
        self._db_path = str(db_path)
        logger.info("SQLite content store init: %s", self._db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.setup_schema()

    def setup_schema(self) -> None:
        """Create tables and indexes."""
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE_SQL)
        for idx_sql in _CREATE_INDEXES_SQL:
            cur.execute(idx_sql)
        self._conn.commit()
        logger.info("SQLite schema initialized")

    def save(
        self,
        domain: str,
        vector_id: str,
        content_type: str,
        storage_uri: str,
        inline_meta: dict[str, Any] | None = None,
    ) -> str:
        """Persist a new content record and return its content_id."""
        content_id = new_content_id()
        meta = inline_meta or {}

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO content_store
                (content_id, domain, vector_id, content_type, storage_uri, inline_meta, created_at)
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                domain,
                vector_id,
                content_type,
                storage_uri,
                json.dumps(meta, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        logger.debug("Content saved: content_id=%s, domain=%s", content_id, domain)
        return content_id

    def _row_to_record(self, row: sqlite3.Row) -> ContentRecord:
        """Convert a sqlite3.Row to a ContentRecord."""
        raw_meta = row["inline_meta"]
        if isinstance(raw_meta, str):
            meta = json.loads(raw_meta)
        elif isinstance(raw_meta, dict):
            meta = raw_meta
        else:
            meta = {}

        return ContentRecord(
            content_id=row["content_id"],
            domain=row["domain"],
            vector_id=row["vector_id"],
            content_type=row["content_type"],
            storage_uri=row["storage_uri"],
            inline_meta=meta,
            created_at=_coerce_pg_timestamp(row["created_at"]),
        )

    def get(self, content_id: str) -> ContentRecord | None:
        """Look up a record by content_id."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM content_store WHERE content_id = ?",
            (content_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def get_by_vector_id(self, vector_id: str) -> ContentRecord | None:
        """Look up a content record by vector_id."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM content_store WHERE vector_id = ? LIMIT 1",
            (vector_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_by_domain(
        self,
        domain: str,
        limit: int = 100,
        offset: int = 0,
        exclude_orphans: bool = True,
    ) -> list[ContentRecord]:
        """Return content records for a domain, newest first."""
        cur = self._conn.cursor()

        if exclude_orphans:
            # SQLite has no JSONB operators; use json_extract instead
            cur.execute(
                """
                SELECT * FROM content_store
                WHERE domain = ?
                  AND (json_extract(inline_meta, '$.orphan') IS NULL
                       OR json_extract(inline_meta, '$.orphan') != 1)
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (domain, limit, offset),
            )
        else:
            cur.execute(
                """
                SELECT * FROM content_store
                WHERE domain = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (domain, limit, offset),
            )

        return [self._row_to_record(row) for row in cur.fetchall()]

    def mark_orphan(self, content_id: str) -> None:
        """Mark a content record as orphaned (append-only: no hard delete)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT inline_meta FROM content_store WHERE content_id = ?",
            (content_id,),
        )
        row = cur.fetchone()
        if row is None:
            logger.warning("mark_orphan: content_id=%s not found", content_id)
            return

        meta = json.loads(row["inline_meta"]) if isinstance(row["inline_meta"], str) else {}
        meta["orphan"] = True
        meta["orphaned_at"] = datetime.now(timezone.utc).isoformat()

        cur.execute(
            "UPDATE content_store SET inline_meta = ? WHERE content_id = ?",
            (json.dumps(meta, ensure_ascii=False), content_id),
        )
        self._conn.commit()
        logger.warning("Content marked as orphan: content_id=%s", content_id)

    def ping(self) -> bool:
        """Check that the SQLite connection is alive."""
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            return True
        except Exception as e:
            logger.error("SQLite connection failed: %s", e)
            return False

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self, "_conn") and self._conn:
            try:
                self._conn.close()
                logger.info("SQLite connection closed: %s", self._db_path)
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
