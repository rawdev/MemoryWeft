"""PostgreSQL Content Store.

- Relational DB abstraction for content metadata
- References Object Storage URIs + inline_meta JSONB
- Append-only: logical deactivation via orphan flag, no physical deletes
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection

from k2g.core.models import ContentRecord, new_content_id

logger = logging.getLogger(__name__)

# DDL
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS content_store (
    content_id    VARCHAR(64)  PRIMARY KEY,
    domain        VARCHAR(128) NOT NULL,
    vector_id     VARCHAR(64)  NOT NULL,
    content_type  VARCHAR(128) NOT NULL,
    storage_uri   TEXT         NOT NULL,
    inline_meta   JSONB        NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_content_store_domain ON content_store (domain);",
    "CREATE INDEX IF NOT EXISTS idx_content_store_vector_id ON content_store (vector_id);",
    "CREATE INDEX IF NOT EXISTS idx_content_store_created_at ON content_store (created_at DESC);",
    # JSONB GIN index (optimises inline_meta queries)
    "CREATE INDEX IF NOT EXISTS idx_content_store_meta ON content_store USING GIN (inline_meta);",
]


class PostgresContentStore:
    """PostgreSQL-based content store managing the content_store table."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        logger.info("PostgreSQL content store initialising")
        self._conn: PgConnection = psycopg2.connect(
            dsn,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        self._conn.autocommit = False
        # Skip schema setup in read-only MCP environments
        # (same flag as the graph store — 2026-05-12 incident).
        import os
        if os.environ.get("K2G_DB_SKIP_SCHEMA_SETUP", "").strip().lower() in ("1", "true", "yes"):
            logger.info(
                "PostgresContentStore: schema setup skip (K2G_DB_SKIP_SCHEMA_SETUP=true)"
            )
        else:
            self.setup_schema()

    def setup_schema(self) -> None:
        """Create tables and indexes. Skips if they already exist."""
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            for idx_sql in _CREATE_INDEXES_SQL:
                cur.execute(idx_sql)
        self._conn.commit()
        logger.info("PostgreSQL schema initialized")

    def save(
        self,
        domain: str,
        vector_id: str,
        content_type: str,
        storage_uri: str,
        inline_meta: dict[str, Any] | None = None,
    ) -> str:
        """Save a new content record.

        Args:
            domain: Domain name.
            vector_id: Associated vector ID.
            content_type: MIME type.
            storage_uri: Object Storage URI.
            inline_meta: Additional metadata.

        Returns:
            Generated content_id.
        """
        content_id = new_content_id()
        meta = inline_meta or {}

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_store
                    (content_id, domain, vector_id, content_type, storage_uri, inline_meta, created_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    content_id,
                    domain,
                    vector_id,
                    content_type,
                    storage_uri,
                    json.dumps(meta, ensure_ascii=False),
                    datetime.now(timezone.utc),
                ),
            )
        self._conn.commit()
        logger.debug("Content saved: content_id=%s, domain=%s", content_id, domain)
        return content_id

    def get(self, content_id: str) -> ContentRecord | None:
        """Look up a record by content_id. Returns ContentRecord or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM content_store WHERE content_id = %s",
                (content_id,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        return ContentRecord(
            content_id=row["content_id"],
            domain=row["domain"],
            vector_id=row["vector_id"],
            content_type=row["content_type"],
            storage_uri=row["storage_uri"],
            inline_meta=row["inline_meta"] if isinstance(row["inline_meta"], dict) else {},
            created_at=row["created_at"],
        )

    def get_by_vector_id(self, vector_id: str) -> ContentRecord | None:
        """Look up a content record by vector_id."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM content_store WHERE vector_id = %s LIMIT 1",
                (vector_id,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        return ContentRecord(
            content_id=row["content_id"],
            domain=row["domain"],
            vector_id=row["vector_id"],
            content_type=row["content_type"],
            storage_uri=row["storage_uri"],
            inline_meta=row["inline_meta"] if isinstance(row["inline_meta"], dict) else {},
            created_at=row["created_at"],
        )

    def list_by_domain(
        self,
        domain: str,
        limit: int = 100,
        offset: int = 0,
        exclude_orphans: bool = True,
    ) -> list[ContentRecord]:
        """Return a list of content records for the given domain."""
        orphan_filter = ""
        if exclude_orphans:
            orphan_filter = "AND NOT (inline_meta ? 'orphan' AND (inline_meta->>'orphan')::boolean = true)"

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM content_store
                WHERE domain = %s {orphan_filter}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (domain, limit, offset),
            )
            rows = cur.fetchall()

        return [
            ContentRecord(
                content_id=row["content_id"],
                domain=row["domain"],
                vector_id=row["vector_id"],
                content_type=row["content_type"],
                storage_uri=row["storage_uri"],
                inline_meta=row["inline_meta"] if isinstance(row["inline_meta"], dict) else {},
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def mark_orphan(self, content_id: str) -> None:
        """Mark content as orphan (used by compensating transactions).

        Does not physically delete data (append-only principle).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_store
                SET inline_meta = inline_meta || %s::jsonb
                WHERE content_id = %s
                """,
                (json.dumps({"orphan": True, "orphaned_at": datetime.now(timezone.utc).isoformat()}), content_id),
            )
        self._conn.commit()
        logger.warning("Content marked as orphan: content_id=%s", content_id)

    def ping(self) -> bool:
        """Check connection health."""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception as e:
            logger.error("PostgreSQL connection failed: %s", e)
            return False

    def close(self) -> None:
        """Close the connection."""
        if hasattr(self, "_conn") and self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("PostgreSQL connection closed")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
