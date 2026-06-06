"""
Covenant Store — managed source registry DB layer.

Stores per-domain managed source covenants (filesystem/vcs/database
sources) in a SQLite file and records add/remove/enable/disable history
in a separate history table.

Tables:
    - k2g_covenant:         Active covenant records (UNIQUE(domain, source_id))
    - k2g_covenant_history: Change history (action = add|remove|enable|disable|modify)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_COVENANT_SQL = """
CREATE TABLE IF NOT EXISTS k2g_covenant (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT    NOT NULL,
    source_id    TEXT    NOT NULL,
    type         TEXT    NOT NULL,
    config_json  TEXT    NOT NULL,
    group_policy TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    description  TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (domain, source_id)
);
"""

_CREATE_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS k2g_covenant_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    covenant_id  INTEGER,
    action       TEXT NOT NULL,
    domain       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    before_json  TEXT,
    after_json   TEXT,
    acted_at     TEXT NOT NULL
);
"""

_CREATE_SOURCE_FILE_SQL = """
CREATE TABLE IF NOT EXISTS k2g_source_file (
    id            TEXT PRIMARY KEY,
    covenant_id   TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER,
    mime_type     TEXT,
    last_ingested TEXT NOT NULL,
    last_vcs_sha  TEXT,
    extractor_key TEXT NOT NULL DEFAULT 'default',
    unit_strategy TEXT NOT NULL DEFAULT 'whole_file'
);
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_covenant_domain ON k2g_covenant (domain);",
    "CREATE INDEX IF NOT EXISTS idx_covenant_enabled ON k2g_covenant (enabled);",
    "CREATE INDEX IF NOT EXISTS idx_covenant_history_domain ON k2g_covenant_history (domain);",
    "CREATE INDEX IF NOT EXISTS idx_sf_covenant ON k2g_source_file (covenant_id);",
]

VALID_TYPES = frozenset({"filesystem", "vcs", "database", "email", "rss"})
VALID_GROUP_POLICIES = frozenset({"path", "vcs", "db"})


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CovenantRecord:
    """A single managed source covenant record."""

    domain: str
    source_id: str
    type: str
    config: dict[str, Any] = field(default_factory=dict)
    group_policy: str = "path"
    enabled: bool = True
    description: str | None = None
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> CovenantRecord:
    return CovenantRecord(
        id=row["id"],
        domain=row["domain"],
        source_id=row["source_id"],
        type=row["type"],
        config=json.loads(row["config_json"]),
        group_policy=row["group_policy"],
        enabled=bool(row["enabled"]),
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CovenantStore:
    """SQLite-backed managed source covenant store."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            from k2g.core.config import get_settings
            db_path = get_settings().covenant_db_path
        self._db_path = str(db_path)
        logger.info("Covenant store initialized: %s", self._db_path)

        parent = Path(self._db_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.setup_schema()

    def setup_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_COVENANT_SQL)
        cur.execute(_CREATE_HISTORY_SQL)
        cur.execute(_CREATE_SOURCE_FILE_SQL)
        for idx_sql in _CREATE_INDEXES_SQL:
            cur.execute(idx_sql)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list(
        self,
        domain: str | None = None,
        enabled_only: bool = False,
    ) -> list[CovenantRecord]:
        sql = "SELECT * FROM k2g_covenant"
        clauses: list[str] = []
        params: list[Any] = []
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        if enabled_only:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY domain, source_id"

        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return [_row_to_record(r) for r in cur.fetchall()]

    def get(self, domain: str, source_id: str) -> CovenantRecord | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM k2g_covenant WHERE domain = ? AND source_id = ?",
            (domain, source_id),
        )
        row = cur.fetchone()
        return _row_to_record(row) if row else None

    def list_history(
        self, domain: str | None = None, source_id: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM k2g_covenant_history"
        clauses: list[str] = []
        params: list[Any] = []
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"

        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return cur.fetchall()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, record: CovenantRecord) -> int:
        """Register a new covenant and record an 'add' history event."""
        if record.type not in VALID_TYPES:
            raise ValueError(f"invalid type: {record.type} (valid: {sorted(VALID_TYPES)})")
        if record.group_policy not in VALID_GROUP_POLICIES:
            raise ValueError(
                f"invalid group_policy: {record.group_policy} "
                f"(valid: {sorted(VALID_GROUP_POLICIES)})"
            )

        now = _now()
        config_json = json.dumps(record.config, ensure_ascii=False)

        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO k2g_covenant
                    (domain, source_id, type, config_json, group_policy,
                     enabled, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.domain,
                    record.source_id,
                    record.type,
                    config_json,
                    record.group_policy,
                    1 if record.enabled else 0,
                    record.description,
                    now,
                    now,
                ),
            )
            new_id = cur.lastrowid
            self._append_history(
                cur, new_id, "add", record.domain, record.source_id,
                before_json=None, after_json=config_json,
            )
            self._conn.commit()
        except sqlite3.IntegrityError as e:
            self._conn.rollback()
            raise ValueError(
                f"covenant already exists: domain={record.domain} source_id={record.source_id}"
            ) from e

        assert new_id is not None
        return new_id

    def remove(self, domain: str, source_id: str) -> bool:
        """Remove a covenant and record a 'remove' history event."""
        existing = self.get(domain, source_id)
        if existing is None:
            return False

        before_json = json.dumps(existing.config, ensure_ascii=False)
        cur = self._conn.cursor()
        try:
            cur.execute(
                "DELETE FROM k2g_covenant WHERE domain = ? AND source_id = ?",
                (domain, source_id),
            )
            self._append_history(
                cur, existing.id, "remove", domain, source_id,
                before_json=before_json, after_json=None,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return True

    def set_enabled(self, domain: str, source_id: str, enabled: bool) -> bool:
        """Toggle the enabled flag and record an 'enable'/'disable' history event."""
        existing = self.get(domain, source_id)
        if existing is None:
            return False
        if existing.enabled == enabled:
            return True  # no-op

        now = _now()
        cur = self._conn.cursor()
        try:
            cur.execute(
                "UPDATE k2g_covenant SET enabled = ?, updated_at = ? "
                "WHERE domain = ? AND source_id = ?",
                (1 if enabled else 0, now, domain, source_id),
            )
            action = "enable" if enabled else "disable"
            config_json = json.dumps(existing.config, ensure_ascii=False)
            self._append_history(
                cur, existing.id, action, domain, source_id,
                before_json=config_json, after_json=config_json,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return True

    # ------------------------------------------------------------------
    # SourceFile CRUD
    # ------------------------------------------------------------------

    def create_source_file(self, record: dict[str, Any]) -> None:
        """Insert or replace a SourceFile record."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO k2g_source_file
                (id, covenant_id, relative_path, content_hash, size_bytes,
                 mime_type, last_ingested, last_vcs_sha, extractor_key, unit_strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["covenant_id"],
                record["relative_path"],
                record["content_hash"],
                record.get("size_bytes", 0),
                record.get("mime_type", ""),
                record["last_ingested"],
                record.get("last_vcs_sha"),
                record.get("extractor_key", "default"),
                record.get("unit_strategy", "whole_file"),
            ),
        )
        self._conn.commit()

    def get_source_files_by_covenant(self, covenant_id: str) -> list[dict[str, Any]]:
        """Return all SourceFile records for the given covenant."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM k2g_source_file WHERE covenant_id = ?",
            (covenant_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def update_source_file_hash(
        self, sf_id: str, content_hash: str, last_ingested: str,
    ) -> None:
        """Update the content_hash and last_ingested timestamp of a SourceFile."""
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE k2g_source_file SET content_hash = ?, last_ingested = ? WHERE id = ?",
            (content_hash, last_ingested, sf_id),
        )
        self._conn.commit()

    def delete_source_file(self, sf_id: str) -> None:
        """Delete a SourceFile record."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM k2g_source_file WHERE id = ?", (sf_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append_history(
        self,
        cur: sqlite3.Cursor,
        covenant_id: int | None,
        action: str,
        domain: str,
        source_id: str,
        before_json: str | None,
        after_json: str | None,
    ) -> None:
        cur.execute(
            """
            INSERT INTO k2g_covenant_history
                (covenant_id, action, domain, source_id, before_json, after_json, acted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (covenant_id, action, domain, source_id, before_json, after_json, _now()),
        )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
