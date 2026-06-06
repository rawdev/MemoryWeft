"""
Build Manifest Store -- two-stage hash-based incremental build versioning.

Uses file-level hashes (Step 1) + segment-level hashes (Step 2) to
selectively ingest only changed segments.

Tables:
    - build_file_manifest:    file-level version tracking
    - build_segment_manifest: segment-level version tracking + vector_id mapping
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_FILE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_file_manifest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    segment_count   INTEGER NOT NULL,
    build_id        TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    source_root     TEXT,
    status          TEXT DEFAULT 'active',
    stage_state     TEXT NOT NULL DEFAULT 'produced',
    UNIQUE(domain, file_path, build_id)
);
"""

_CREATE_SEGMENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_segment_manifest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    segment_key     TEXT NOT NULL,
    segment_index   INTEGER NOT NULL,
    segment_hash    TEXT NOT NULL,
    vector_id       TEXT,
    old_vector_id   TEXT,
    event_id        TEXT,
    build_id        TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    status          TEXT DEFAULT 'active',
    stage_state     TEXT NOT NULL DEFAULT 'produced',
    loaded_at       TEXT,
    UNIQUE(domain, file_path, segment_key, build_id)
);
"""

_CREATE_VCS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_vcs_manifest (
    domain          TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    branch          TEXT NOT NULL,
    last_revision   TEXT,
    last_built_at   TEXT,
    build_id        TEXT,
    status          TEXT,
    stage_state     TEXT NOT NULL DEFAULT 'committed',
    PRIMARY KEY (domain, repo_url, branch)
);
"""

# Extractor-stage input -> output mapping tracking.
# input_path: input file path for file extractors, source_root for batch.
# input_hash: sha256(file content) for file extractors, merkle hash for batch.
# output_path: output location (file or directory).
_CREATE_EXTRACT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_extract_manifest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    extractor_kind  TEXT NOT NULL,
    input_path      TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    output_path     TEXT NOT NULL,
    build_id        TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    status          TEXT DEFAULT 'active',
    stage_state     TEXT NOT NULL DEFAULT 'extracted',
    UNIQUE(domain, extractor_kind, input_path, build_id)
);
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_file_manifest_hash ON build_file_manifest(domain, file_path, file_hash);",
    "CREATE INDEX IF NOT EXISTS idx_file_manifest_build ON build_file_manifest(build_id);",
    "CREATE INDEX IF NOT EXISTS idx_file_manifest_active ON build_file_manifest(domain, file_path, status);",
    "CREATE INDEX IF NOT EXISTS idx_segment_manifest_hash ON build_segment_manifest(domain, file_path, segment_key, segment_hash);",
    "CREATE INDEX IF NOT EXISTS idx_segment_manifest_build ON build_segment_manifest(build_id);",
    "CREATE INDEX IF NOT EXISTS idx_segment_manifest_active ON build_segment_manifest(domain, file_path, status);",
    "CREATE INDEX IF NOT EXISTS idx_segment_manifest_vector ON build_segment_manifest(old_vector_id);",
    "CREATE INDEX IF NOT EXISTS idx_extract_manifest_active ON build_extract_manifest(domain, extractor_kind, input_path, status);",
    "CREATE INDEX IF NOT EXISTS idx_extract_manifest_hash ON build_extract_manifest(domain, extractor_kind, input_path, input_hash);",
    "CREATE INDEX IF NOT EXISTS idx_extract_manifest_build ON build_extract_manifest(build_id);",
]

# stage_state indexes.  Created after the ALTER TABLE step for legacy
# DB compatibility, hence separated from _CREATE_INDEXES_SQL.
_CREATE_STAGE_STATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_segment_manifest_stage ON build_segment_manifest(domain, stage_state, status);",
    "CREATE INDEX IF NOT EXISTS idx_file_manifest_stage ON build_file_manifest(domain, stage_state, status);",
]


class BuildManifestStore:
    """SQLite-based build manifest -- file/segment two-stage version tracking."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            from k2g.core.config import get_settings
            db_path = f"{get_settings().data_dir}/build_manifest.db"
        self._db_path = str(db_path)
        logger.info("Build Manifest store initializing: %s", self._db_path)

        parent = Path(self._db_path).parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.setup_schema()

    def setup_schema(self) -> None:
        """Create tables and indexes.

        If the existing DB lacks the ``source_root`` column, ALTER
        TABLE adds it (NULL allowed).  New DBs already include it in
        the CREATE TABLE definition.  The ``build_extract_manifest``
        table is created if missing (CREATE TABLE IF NOT EXISTS for
        legacy DB compatibility).  Each manifest table receives a
        ``stage_state`` column (segments also get ``loaded_at``).
        Idempotent: new DBs include them in CREATE, legacy DBs get
        them via ALTER.
        """
        cur = self._conn.cursor()
        cur.execute(_CREATE_FILE_TABLE_SQL)
        cur.execute(_CREATE_SEGMENT_TABLE_SQL)
        cur.execute(_CREATE_VCS_TABLE_SQL)
        cur.execute(_CREATE_EXTRACT_TABLE_SQL)
        for idx_sql in _CREATE_INDEXES_SQL:
            cur.execute(idx_sql)
        # Add source_root column to legacy DB if missing
        cur.execute("PRAGMA table_info(build_file_manifest)")
        columns = {row["name"] for row in cur.fetchall()}
        if "source_root" not in columns:
            cur.execute("ALTER TABLE build_file_manifest ADD COLUMN source_root TEXT")
            logger.info("Added source_root column to build_file_manifest")

        # Idempotent addition of stage_state / loaded_at columns.
        # SQLite ALTER TABLE NOT NULL DEFAULT populates on add (3.31+).
        for tbl, default in (
            ("build_file_manifest", "produced"),
            ("build_segment_manifest", "produced"),
            ("build_extract_manifest", "extracted"),
            ("build_vcs_manifest", "committed"),
        ):
            cur.execute(f"PRAGMA table_info({tbl})")
            cols = {row["name"] for row in cur.fetchall()}
            if "stage_state" not in cols:
                cur.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN stage_state TEXT "
                    f"NOT NULL DEFAULT '{default}'"
                )
                logger.info("Added stage_state column to %s (default=%s)", tbl, default)

        # Add loaded_at to segment table only
        cur.execute("PRAGMA table_info(build_segment_manifest)")
        cols = {row["name"] for row in cur.fetchall()}
        if "loaded_at" not in cols:
            cur.execute("ALTER TABLE build_segment_manifest ADD COLUMN loaded_at TEXT")
            logger.info("Added loaded_at column to build_segment_manifest")

        # stage_state indexes (created after ALTER for legacy DB compat)
        for idx_sql in _CREATE_STAGE_STATE_INDEXES_SQL:
            try:
                cur.execute(idx_sql)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stage_state index creation skipped: %s", exc)

        self._conn.commit()

    # ------------------------------------------------------------------
    # VCS-level
    # ------------------------------------------------------------------

    def get_last_revision(
        self, domain: str, repo_url: str, branch: str,
    ) -> str | None:
        """Return the last successful build revision for (domain, repo, branch)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT last_revision FROM build_vcs_manifest "
            "WHERE domain = ? AND repo_url = ? AND branch = ?",
            (domain, repo_url, branch),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return row["last_revision"]

    def update_vcs_manifest(
        self,
        domain: str,
        repo_url: str,
        branch: str,
        *,
        last_revision: str,
        status: str,
        build_id: str | None = None,
    ) -> None:
        """Upsert VCS build result."""
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO build_vcs_manifest "
            "(domain, repo_url, branch, last_revision, last_built_at, build_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(domain, repo_url, branch) DO UPDATE SET "
            "  last_revision = excluded.last_revision, "
            "  last_built_at = excluded.last_built_at, "
            "  build_id      = excluded.build_id, "
            "  status        = excluded.status",
            (
                domain,
                repo_url,
                branch,
                last_revision,
                datetime.now(timezone.utc).isoformat(),
                build_id,
                status,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # File-level (Step 1)
    # ------------------------------------------------------------------

    def check_file_hash(self, domain: str, file_path: str, file_hash: str) -> bool:
        """True when the active record's file_hash matches (= no change)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT file_hash FROM build_file_manifest "
            "WHERE domain = ? AND file_path = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (domain, file_path),
        )
        row = cur.fetchone()
        if row is None:
            return False
        return row["file_hash"] == file_hash

    def get_active_file(self, domain: str, file_path: str) -> sqlite3.Row | None:
        """Return the active record for the given file."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM build_file_manifest "
            "WHERE domain = ? AND file_path = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (domain, file_path),
        )
        return cur.fetchone()

    def record_file(
        self,
        domain: str,
        file_path: str,
        file_hash: str,
        segment_count: int,
        build_id: str,
        source_root: str | None = None,
    ) -> None:
        """INSERT a new file record (status='active').

        ``source_root`` is the domain_root hint at ingestion time so
        Reader / MCP can resolve file_path in the consumer
        environment.  NULL when not specified.
        """
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO build_file_manifest "
            "(domain, file_path, file_hash, segment_count, build_id, "
            " built_at, source_root, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                domain,
                file_path,
                file_hash,
                segment_count,
                build_id,
                datetime.now(timezone.utc).isoformat(),
                source_root,
            ),
        )
        self._conn.commit()

    def supersede_file(self, domain: str, file_path: str) -> None:
        """Mark the active record for the given file as superseded."""
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE build_file_manifest SET status = 'superseded' "
            "WHERE domain = ? AND file_path = ? AND status = 'active'",
            (domain, file_path),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Segment-level (Step 2)
    # ------------------------------------------------------------------

    def get_active_segments(self, domain: str, file_path: str) -> list[sqlite3.Row]:
        """Return all active segment records for the given file."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM build_segment_manifest "
            "WHERE domain = ? AND file_path = ? AND status = 'active' "
            "ORDER BY segment_index",
            (domain, file_path),
        )
        return cur.fetchall()

    def get_active_segment(
        self, domain: str, file_path: str, segment_key: str,
    ) -> sqlite3.Row | None:
        """Return the active record for a specific segment."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM build_segment_manifest "
            "WHERE domain = ? AND file_path = ? AND segment_key = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (domain, file_path, segment_key),
        )
        return cur.fetchone()

    def record_segment(
        self,
        domain: str,
        file_path: str,
        segment_key: str,
        segment_index: int,
        segment_hash: str,
        vector_id: str | None,
        old_vector_id: str | None,
        event_id: str | None,
        build_id: str,
    ) -> None:
        """INSERT a new segment record (status='active')."""
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO build_segment_manifest "
            "(domain, file_path, segment_key, segment_index, segment_hash, "
            " vector_id, old_vector_id, event_id, build_id, built_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                domain,
                file_path,
                segment_key,
                segment_index,
                segment_hash,
                vector_id,
                old_vector_id,
                event_id,
                build_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def record_segments_bulk(self, rows: list[dict]) -> int:
        """Bulk INSERT multiple segment records (executemany).

        Each dict in ``rows`` has the same keys as the arguments to
        ``record_segment``.  Commits internally (if the caller wraps
        this in db.session() the outer commit wins).  On failure the
        exception propagates and the caller rolls back.
        """
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        params = [
            (
                r["domain"],
                r["file_path"],
                r["segment_key"],
                r.get("segment_index", 0),
                r["segment_hash"],
                r.get("vector_id"),
                r.get("old_vector_id"),
                r.get("event_id"),
                r.get("build_id", ""),
                now,
            )
            for r in rows
        ]
        cur = self._conn.cursor()
        cur.executemany(
            "INSERT INTO build_segment_manifest "
            "(domain, file_path, segment_key, segment_index, segment_hash, "
            " vector_id, old_vector_id, event_id, build_id, built_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            params,
        )
        self._conn.commit()
        return len(params)

    def supersede_segment(
        self, domain: str, file_path: str, segment_key: str,
    ) -> str | None:
        """Mark the active segment as superseded and return old_vector_id."""
        cur = self._conn.cursor()
        # First, look up the existing vector_id
        cur.execute(
            "SELECT vector_id FROM build_segment_manifest "
            "WHERE domain = ? AND file_path = ? AND segment_key = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (domain, file_path, segment_key),
        )
        row = cur.fetchone()
        old_vector_id = row["vector_id"] if row else None

        # supersede
        cur.execute(
            "UPDATE build_segment_manifest SET status = 'superseded' "
            "WHERE domain = ? AND file_path = ? AND segment_key = ? AND status = 'active'",
            (domain, file_path, segment_key),
        )
        self._conn.commit()
        return old_vector_id

    def supersede_all_segments(self, domain: str, file_path: str) -> None:
        """Mark all active segments for the given file as superseded."""
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE build_segment_manifest SET status = 'superseded' "
            "WHERE domain = ? AND file_path = ? AND status = 'active'",
            (domain, file_path),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # stage_state promotion (on LOAD success / file promotion)
    # ------------------------------------------------------------------

    def mark_segment_loaded(
        self,
        domain: str,
        file_path: str,
        segment_key: str,
        build_id: str,
        event_id: str,
    ) -> int:
        """Promote the manifest active row for a successfully loaded segment.

        Only records the *fact of success*: sets
        ``stage_state='loaded'``, ``loaded_at=NOW()``, and fills
        ``event_id``.  Not called on failure (the manifest row stays
        at 'produced', indicating LOAD has not occurred).

        Returns: number of rows updated (normally 1; 0 if no match).
        """
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE build_segment_manifest "
            "   SET stage_state = 'loaded', "
            "       loaded_at = ?, "
            "       event_id = ? "
            " WHERE domain = ? AND file_path = ? AND segment_key = ? "
            "   AND build_id = ? AND status = 'active'",
            (
                datetime.now(timezone.utc).isoformat(),
                event_id,
                domain, file_path, segment_key, build_id,
            ),
        )
        n = cur.rowcount
        self._conn.commit()
        return n

    def mark_segments_loaded_bulk(self, rows: list[dict]) -> int:
        """Bulk-promote multiple segments to 'loaded' in one executemany call.

        Each dict in ``rows``: ``{"domain", "file_path", "segment_key",
        "build_id", "event_id"}``.  Caller passes only successfully loaded
        segments.

        Matching key is ``status='active'`` only — guarantees one row per
        domain×file×segment, so ``build_id`` matching is omitted.  This
        allows cross-build LOAD promotion (promoting a 'produced' row from
        a previous build during the current LOAD phase).  ``rows[*].build_id``
        is ignored; only ``rows[*].event_id`` is used in the UPDATE.
        """
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        params = [
            (now, r["event_id"], r["domain"], r["file_path"], r["segment_key"])
            for r in rows
        ]
        cur = self._conn.cursor()
        cur.executemany(
            "UPDATE build_segment_manifest "
            "   SET stage_state = 'loaded', "
            "       loaded_at = ?, "
            "       event_id = ? "
            " WHERE domain = ? AND file_path = ? AND segment_key = ? "
            "   AND status = 'active'",
            params,
        )
        n = cur.rowcount
        self._conn.commit()
        return n

    def backfill_file_loaded(self, domain: str) -> int:
        """Bulk-promote all active files in a domain whose every active
        segment is already 'loaded'.  Called once at the end of the LOAD
        step to clean up files whose segments were all promoted in an
        earlier build but whose file_manifest row remained at 'produced'.

        ``mark_file_loaded`` only promotes files that had 'produced'
        segments in the *current* LOAD pass; this backfill catches the
        remainder.

        Idempotent — only promotes files where no active segment is still
        in a non-'loaded' state (NOT EXISTS subquery).

        Returns: number of file rows promoted.
        """
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE build_file_manifest "
            "   SET stage_state = 'loaded' "
            " WHERE domain = ? "
            "   AND status = 'active' "
            "   AND stage_state != 'loaded' "
            "   AND NOT EXISTS ( "
            "       SELECT 1 FROM build_segment_manifest seg "
            "        WHERE seg.domain = build_file_manifest.domain "
            "          AND seg.file_path = build_file_manifest.file_path "
            "          AND seg.status = 'active' "
            "          AND seg.stage_state != 'loaded' "
            "   )",
            (domain,),
        )
        n = cur.rowcount
        self._conn.commit()
        return n

    def mark_file_loaded(
        self, domain: str, file_path: str, build_id: str = "",
    ) -> int:
        """Promote the file_manifest row when all active segments are 'loaded'.

        Called by the caller when a per-file LOAD completes.  If some
        segments are still 'produced', the NOT EXISTS subquery returns
        False and the UPDATE is a no-op (idempotent).

        Matching key is ``status='active'`` only (build_id is ignored;
        the ``build_id`` parameter is kept for backwards compatibility).

        Returns: number of file rows updated (0 means some segments are
        still 'produced').
        """
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE build_file_manifest "
            "   SET stage_state = 'loaded' "
            " WHERE domain = ? AND file_path = ? "
            "   AND status = 'active' "
            "   AND stage_state != 'loaded' "
            "   AND NOT EXISTS ( "
            "       SELECT 1 FROM build_segment_manifest seg "
            "        WHERE seg.domain = build_file_manifest.domain "
            "          AND seg.file_path = build_file_manifest.file_path "
            "          AND seg.status = 'active' "
            "          AND seg.stage_state != 'loaded' "
            "   )",
            (domain, file_path),
        )
        n = cur.rowcount
        self._conn.commit()
        return n

    # ------------------------------------------------------------------
    # Extractor-level (EXTRACT stage)
    # ------------------------------------------------------------------

    def check_extract_hash(
        self, domain: str, extractor_kind: str, input_path: str, input_hash: str,
    ) -> bool:
        """True when the active extract record's input_hash matches (= can skip)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT input_hash FROM build_extract_manifest "
            "WHERE domain = ? AND extractor_kind = ? AND input_path = ? "
            "  AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (domain, extractor_kind, input_path),
        )
        row = cur.fetchone()
        if row is None:
            return False
        return row["input_hash"] == input_hash

    def get_active_extract(
        self, domain: str, extractor_kind: str, input_path: str,
    ) -> sqlite3.Row | None:
        """Return the active record for the given extractor."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM build_extract_manifest "
            "WHERE domain = ? AND extractor_kind = ? AND input_path = ? "
            "  AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (domain, extractor_kind, input_path),
        )
        return cur.fetchone()

    def record_extract(
        self,
        domain: str,
        extractor_kind: str,
        input_path: str,
        input_hash: str,
        output_path: str,
        build_id: str,
    ) -> None:
        """INSERT a new extract record (status='active').

        Any existing active row for the same (domain, extractor_kind,
        input_path) is automatically superseded — same pattern as
        file_manifest.
        """
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE build_extract_manifest SET status = 'superseded' "
            "WHERE domain = ? AND extractor_kind = ? AND input_path = ? "
            "  AND status = 'active'",
            (domain, extractor_kind, input_path),
        )
        cur.execute(
            "INSERT INTO build_extract_manifest "
            "(domain, extractor_kind, input_path, input_hash, output_path, "
            " build_id, built_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                domain,
                extractor_kind,
                input_path,
                input_hash,
                output_path,
                build_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_build_stats(self, build_id: str) -> dict[str, Any]:
        """Return build statistics for the given build_id."""
        cur = self._conn.cursor()

        cur.execute(
            "SELECT COUNT(*) as cnt FROM build_file_manifest WHERE build_id = ?",
            (build_id,),
        )
        files_total = cur.fetchone()["cnt"]

        cur.execute(
            "SELECT COUNT(*) as cnt FROM build_segment_manifest WHERE build_id = ?",
            (build_id,),
        )
        segments_ingested = cur.fetchone()["cnt"]

        cur.execute(
            "SELECT COUNT(*) as cnt FROM build_segment_manifest "
            "WHERE build_id = ? AND old_vector_id IS NOT NULL",
            (build_id,),
        )
        segments_updated = cur.fetchone()["cnt"]

        return {
            "build_id": build_id,
            "files_processed": files_total,
            "segments_ingested": segments_ingested,
            "segments_updated": segments_updated,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the DB connection."""
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
