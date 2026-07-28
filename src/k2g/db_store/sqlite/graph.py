"""SqliteGraphStore — All-in-One SQLite + sqlite-vec GraphStore.

Local-only GraphStore backed by SQLite + sqlite-vec, intended for
environments without a Postgres server (e.g. Colab, personal laptops).
Mirrors the structure of PostgresGraphStore with 1:1 method matching
and structurally satisfies ``GraphStoreProtocol``.

SQL dialect mapping (Postgres → SQLite):
    VARCHAR(n)   → TEXT
    TIMESTAMPTZ  → TEXT (ISO8601, DEFAULT CURRENT_TIMESTAMP)
    vector(1024) → separate vec0 virtual table (events_vec, entities_vec)
    BOOLEAN      → INTEGER (0/1)
    JSONB        → TEXT (built-in JSON1)
    CHAR(3)      → TEXT
    NOW()        → CURRENT_TIMESTAMP
    BIGINT       → INTEGER
    HNSW index   → not needed (vec0 has built-in KNN)
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Literal

from k2g.core.config import DomainConfig
from k2g.db_store.graph_backend import SequentialSource
from k2g.db_store.sqlite.schema import (
    _CREATE_AUDIT_INDEXES_SQL,
    _CREATE_AUDIT_TABLES_SQL,
    _CREATE_BUILD_AUDIT_INDEXES_SQL,
    _CREATE_BUILD_AUDIT_TABLES_SQL,
    _CREATE_COVENANT_META_INDEXES_SQL,
    _CREATE_COVENANT_META_TABLES_SQL,
    _CREATE_CROSS_DOMAIN_INDEXES_SQL,
    _CREATE_CROSS_DOMAIN_TABLES_SQL,
    _CREATE_DOMAIN_REGISTRY_SQL,
    _CREATE_FAILURE_MANIFEST_INDEXES_SQL,
    _CREATE_FAILURE_MANIFEST_TABLES_SQL,
    _CREATE_INDEXES_SQL,
    _CREATE_SHARE_INDEXES_SQL,
    _CREATE_SHARE_TABLES_SQL,
    _CREATE_TIER1_EDGE_TABLES_SQL,
    _CREATE_TIER1_TABLES_SQL,
    _CREATE_TIER2A_INDEXES_SQL,
    _CREATE_TIER2A_TABLES_SQL,
    _CREATE_TIER2B_INDEXES_SQL,
    _CREATE_TIER2B_TABLES_SQL,
    _CREATE_TRAIN_RUN_INDEXES_SQL,
    _CREATE_TRAIN_RUN_TABLES_SQL,
    _CREATE_TRAIN_STATE_INDEXES_SQL,
    _CREATE_TRAIN_STATE_TABLES_SQL,
)

logger = logging.getLogger(__name__)


# DDL constants live in `k2g.db_store.sqlite.schema` (moved 2026-04-26).
# Keep in sync with postgres/schema.py and  on changes.


# ============================================================================
# SqliteGraphStore
# ============================================================================


def _phase1b_sqlite(desc: str) -> NotImplementedError:
    return NotImplementedError(f"Phase 1b: {desc}")


def _migrate_groups_unique(cur: Any, conn: Any) -> bool:
    """Replace the legacy global ``groups.name UNIQUE`` with
    ``UNIQUE(name, domain, type)`` (adding ``type`` when absent).

    A global name key cannot represent a multi-domain install — the same tag
    name legitimately exists in two domains, and a server-side ``'system'``
    mirror legitimately shadows a same-named user tag. It also makes archives
    from a domain-scoped source unimportable (observed:
    ``UNIQUE constraint failed: groups.name``).

    SQLite cannot drop a column-level UNIQUE (it is an implicit index), so the
    table must be rebuilt. Guarded to run at most once: it is a no-op as soon
    as the implicit ``sqlite_autoindex`` on ``name`` alone is gone.

    Returns True when a rebuild happened.
    """
    # Does a single-column unique index on `name` still exist? Named indexes
    # are ours and are recreated below; only the implicit one blocks us.
    cur.execute("PRAGMA index_list(groups)")
    legacy = None
    for row in cur.fetchall():
        idx = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        unique = row["unique"] if isinstance(row, sqlite3.Row) else row[2]
        if not unique:
            continue
        cur.execute(f"PRAGMA index_info({idx})")
        cols = [
            (r["name"] if isinstance(r, sqlite3.Row) else r[2])
            for r in cur.fetchall()
        ]
        if cols == ["name"]:
            legacy = idx
            break
    if legacy is None:
        return False

    cur.execute("PRAGMA table_info(groups)")
    old_cols = [
        (r["name"] if isinstance(r, sqlite3.Row) else r[1]) for r in cur.fetchall()
    ]
    has_type = "type" in old_cols
    carried = ", ".join(old_cols)
    select = carried if has_type else f"{carried}, 'user'"
    target = carried if has_type else f"{carried}, type"

    logger.info("migration: rebuilding groups to UNIQUE(name, domain, type)")
    # PRAGMA foreign_keys is a no-op inside a transaction; close any open one.
    conn.commit()
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        cur.execute("BEGIN")
        # legacy_alter_table=OFF (the default) makes RENAME rewrite references
        # in other tables' FK clauses, which is exactly what we do NOT want
        # here — we rename our temp table into place and the existing FKs
        # already point at `groups`.
        cur.execute("PRAGMA legacy_alter_table = ON")
        cur.execute("ALTER TABLE groups RENAME TO groups_legacy_unique")
        for sql in _CREATE_TIER1_TABLES_SQL:
            if "CREATE TABLE IF NOT EXISTS groups" in sql:
                cur.execute(sql)
                break
        cur.execute(
            f"INSERT INTO groups ({target}) "  # noqa: S608 — names from PRAGMA
            f"SELECT {select} FROM groups_legacy_unique"
        )
        cur.execute("DROP TABLE groups_legacy_unique")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            cur.execute("PRAGMA legacy_alter_table = OFF")
            cur.execute("PRAGMA foreign_keys = ON")
        except Exception:  # noqa: BLE001
            pass
    return True


class SqliteGraphStore:
    """SQLite + sqlite-vec backed GraphStore.

    Structurally satisfies GraphStoreProtocol with 1:1 method matching to
    PostgresGraphStore. All DDL, connection management, and query methods
    are fully implemented using sqlite3 + raw SQL.

    All-in-One file: graph tables and vec0 virtual tables coexist in a
    single SQLite file. A separate file from content_store.db is
    recommended to avoid role confusion.
    """

    def __init__(
        self,
        path: str,
        *,
        load_vec: bool = True,
        embedding_dim: int = 1024,
        embedding_model: str | None = None,
    ) -> None:
        """Args:
            path: SQLite file path, e.g. ``./data/k2g_all_in_one.db``.
            load_vec: Whether to load the sqlite-vec extension. When False,
                vec0 virtual-table creation is skipped (useful for schema-only
                unit tests).
            embedding_dim: Embedding dimension for vec0 tables. Default 1024.
            embedding_model: Embedding model id; stamped into the DB fingerprint
                so a later open with an incompatible dim/model is blocked.
        """
        self._path = path
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        logger.info("SqliteGraphStore init: path=%s, dim=%d", path, embedding_dim)

        # Ensure the parent directory exists — guards against first-run DATA_DIR
        # creation (tests and new sessions).
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self._conn: sqlite3.Connection = sqlite3.connect(
            path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            # FastAPI sync endpoints run in a thread pool, so we relax the
            # thread-local connection constraint. Concurrent writes are
            # delegated to WAL + SQLite's internal locking.
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

        if load_vec:
            self._load_sqlite_vec()

        # PRAGMAs — enforce FK constraints + WAL concurrency
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        # busy_timeout: the default 0 ms causes an immediate SQLITE_BUSY error
        # on lock contention. Set to 5 s so occasional multi-user / concurrent
        # writes (e.g. background re-computation during ingest) wait and retry
        # instead of failing immediately. No impact on the normal single-user path.
        self._conn.execute("PRAGMA busy_timeout = 5000;")

        self._vec_loaded = load_vec
        self.setup_schema()
        # Refuse to open a DB whose embedding fingerprint (dim/model) differs —
        # mixing vector dimensions/models in one store silently corrupts search.
        self._check_embedding_fingerprint()
        # Tier 2a/2b schemas are also ensured at startup so that idempotent
        # ALTERs for plan_nodes / context_groups / event_template_groups are
        # applied automatically at MCP startup — no manual call required after
        # a schema change. All operations are idempotent (CREATE IF NOT EXISTS /
        # ADD COLUMN IF NOT EXISTS), so the overhead is small (a few ms).
        self.setup_training_schema()
        self.setup_bp30_schema()

    def _load_sqlite_vec(self) -> None:
        """Load the sqlite-vec extension. Raises a clear error on failure."""
        try:
            self._conn.enable_load_extension(True)
        except AttributeError as exc:
            raise RuntimeError(
                "This Python sqlite3 build has load_extension disabled. "
                "Use pysqlite3-binary (`pip install pysqlite3-binary`) or "
                "a statically-linked sqlite3 with extensions enabled."
            ) from exc

        try:
            import sqlite_vec
        except ImportError as exc:
            raise RuntimeError(
                "sqlite-vec required. pip install sqlite-vec "
                "(see 69_Blue_print-32-AllInOneDB §A)."
            ) from exc

        try:
            sqlite_vec.load(self._conn)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "sqlite-vec load failed. Some SQLite builds disable "
                "load_extension; install pysqlite3-binary."
            ) from exc
        finally:
            try:
                self._conn.enable_load_extension(False)
            except AttributeError:
                pass

    def _check_embedding_fingerprint(self) -> None:
        """Stamp (first open) or verify (later opens) the embedding fingerprint.

        Raises ``EmbeddingFingerprintMismatch`` and closes the connection if the
        current embedding dim/model is incompatible with what the DB was built
        with, so the app fails fast instead of corrupting vector search.
        """
        from k2g.db_store.embedding_guard import (
            DIM_KEY,
            MODEL_KEY,
            EmbeddingFingerprintMismatch,
            fingerprint_problems,
            mismatch_message,
        )

        cur = self._conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS k2g_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        rows = {
            r[0]: r[1]
            for r in cur.execute(
                "SELECT key, value FROM k2g_meta WHERE key IN (?, ?)",
                (DIM_KEY, MODEL_KEY),
            ).fetchall()
        }
        cur_dim = str(self._embedding_dim)
        cur_model = (self._embedding_model or "").strip()
        stored_dim = rows.get(DIM_KEY)
        stored_model = rows.get(MODEL_KEY)

        if stored_dim is None:  # new or legacy DB — stamp the current fingerprint
            cur.execute(
                "INSERT OR REPLACE INTO k2g_meta (key, value) VALUES (?, ?), (?, ?)",
                (DIM_KEY, cur_dim, MODEL_KEY, cur_model),
            )
            self._conn.commit()
            return

        problems = fingerprint_problems(stored_dim, stored_model, cur_dim, cur_model)
        if problems:
            self._conn.close()
            raise EmbeddingFingerprintMismatch(
                mismatch_message(stored_dim, stored_model, cur_dim, cur_model, problems)
            )
        if not stored_model and cur_model:  # back-fill model on a dim-only legacy stamp
            cur.execute(
                "INSERT OR REPLACE INTO k2g_meta (key, value) VALUES (?, ?)",
                (MODEL_KEY, cur_model),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # (a) Schema bootstrap — real implementation
    # ------------------------------------------------------------------

    def setup_schema(self) -> None:
        """Tier 1 (Entity / Event / Group / 5 edges) + audit + vec0 virtual tables.

        For existing databases that lack ``events.influence_score``, an
        ALTER TABLE is executed to add the column (default 1.0). New databases
        already include it in the CREATE TABLE definition.
        """
        cur = self._conn.cursor()
        try:
            for sql in _CREATE_TIER1_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_TIER1_EDGE_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_AUDIT_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BUILD_AUDIT_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_FAILURE_MANIFEST_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_TRAIN_STATE_TABLES_SQL:
                cur.execute(sql)
            # Train run audit + entity community assignment
            for sql in _CREATE_TRAIN_RUN_TABLES_SQL:
                cur.execute(sql)
            # Covenant metadata (k2g_covenant + history + source_file)
            for sql in _CREATE_COVENANT_META_TABLES_SQL:
                cur.execute(sql)
            # k2g_source_file.domain — covenant-derived tenant axis (RLS_TABLES
            # parity). Add + backfill from the parent covenant for existing DBs.
            cur.execute("PRAGMA table_info(k2g_source_file)")
            _sf_cols = {row["name"] for row in cur.fetchall()}
            if "domain" not in _sf_cols:
                cur.execute("ALTER TABLE k2g_source_file ADD COLUMN domain TEXT")
                logger.info("migration: added k2g_source_file.domain")
            cur.execute(
                "UPDATE k2g_source_file SET domain = ("
                # source_file.covenant_id = str(covenant.id) → cast for the join
                "SELECT domain FROM k2g_covenant WHERE CAST(id AS TEXT) = covenant_id"
                ") WHERE domain IS NULL"
            )
            # Share group + member + audit
            for sql in _CREATE_SHARE_TABLES_SQL:
                cur.execute(sql)
            # Cross-domain primitives
            for sql in _CREATE_CROSS_DOMAIN_TABLES_SQL:
                cur.execute(sql)
            # Domain soft-registry (empty-domain support; idempotent)
            for sql in _CREATE_DOMAIN_REGISTRY_SQL:
                cur.execute(sql)
            for sql in _CREATE_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_AUDIT_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BUILD_AUDIT_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_FAILURE_MANIFEST_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_TRAIN_STATE_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_TRAIN_RUN_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_COVENANT_META_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_SHARE_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_CROSS_DOMAIN_INDEXES_SQL:
                cur.execute(sql)
            # Legacy migration — ensure events.influence_score exists
            cur.execute("PRAGMA table_info(events)")
            columns = {row["name"] for row in cur.fetchall()}
            if "influence_score" not in columns:
                cur.execute(
                    "ALTER TABLE events ADD COLUMN influence_score "
                    "REAL NOT NULL DEFAULT 1.0",
                )
                logger.info("migration: added events.influence_score")
            # Legacy migration — ensure ner_method / ner_skip_reason exist
            if "ner_method" not in columns:
                cur.execute("ALTER TABLE events ADD COLUMN ner_method TEXT")
                logger.info("migration: added events.ner_method")
            if "ner_skip_reason" not in columns:
                cur.execute("ALTER TABLE events ADD COLUMN ner_skip_reason TEXT")
                logger.info("migration: added events.ner_skip_reason")
            # entity_embedding_meta.coherence (mean resultant length R in [0,1])
            cur.execute("PRAGMA table_info(entity_embedding_meta)")
            eem_columns = {row["name"] for row in cur.fetchall()}
            if "coherence" not in eem_columns:
                try:
                    cur.execute(
                        "ALTER TABLE entity_embedding_meta "
                        "ADD COLUMN coherence REAL",
                    )
                    logger.info("migration: added entity_embedding_meta.coherence")
                except sqlite3.OperationalError as exc:
                    logger.error(
                        "schema_migration_failed",
                        extra={
                            "backend": "sqlite", "table": "entity_embedding_meta",
                            "column": "coherence", "err": str(exc)[:200],
                        },
                    )
            # groups: global `name UNIQUE` -> UNIQUE(name, domain, type)
            _migrate_groups_unique(cur, self._conn)
            # Data Owner / Visibility columns (5 cols, 9 tables, idempotent)
            from k2g.security.data_owner import apply_data_owner_alters_sqlite
            apply_data_owner_alters_sqlite(cur)
            # groups.workspace_id — separate from org_id; no FK in core because
            # it crosses schema boundaries
            cur.execute("PRAGMA table_info(groups)")
            _gcols = {(r["name"] if hasattr(r, "keys") else r[1])
                      for r in cur.fetchall()}
            if "workspace_id" not in _gcols:
                try:
                    cur.execute("ALTER TABLE groups ADD COLUMN workspace_id TEXT")
                    logger.info("migration: added groups.workspace_id")
                except sqlite3.OperationalError as exc:
                    logger.error(
                        "schema_migration_failed",
                        extra={
                            "backend": "sqlite", "table": "groups",
                            "column": "workspace_id", "err": str(exc)[:200],
                        },
                    )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_groups_workspace "
                "ON groups(workspace_id)",
            )
            # Source Lineage: 8-column ALTER on events + 3 new tables
            from k2g.source.lineage_alter import apply_source_lineage_alters_sqlite
            from k2g.db_store.sqlite.source_lineage_schema import (
                setup_source_lineage_schema_sqlite,
            )
            apply_source_lineage_alters_sqlite(cur)
            setup_source_lineage_schema_sqlite(cur)
            # BP-51 — Log System / Cost Reconciliation (Phase A + B + C)
            from k2g.db_store.sqlite.schema import (
                _CREATE_BP51_RUN_SUMMARY_TABLES_SQL,
                _CREATE_BP51_RUN_SUMMARY_INDEXES_SQL,
                _CREATE_BP51_STORAGE_USAGE_TABLES_SQL,
                _CREATE_BP51_STORAGE_USAGE_INDEXES_SQL,
                _CREATE_BP51_BALANCE_TABLES_SQL,
                _CREATE_BP51_BALANCE_INDEXES_SQL,
                _CREATE_BP51_CALIBRATION_TABLES_SQL,
                _CREATE_BP51_CALIBRATION_INDEXES_SQL,
                _CREATE_BP51_SQL_AUDIT_TABLES_SQL,
                _CREATE_BP51_SQL_AUDIT_INDEXES_SQL,
                _CREATE_BP51_VIEWS_SQL,
            )
            for sql in _CREATE_BP51_RUN_SUMMARY_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_RUN_SUMMARY_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_STORAGE_USAGE_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_STORAGE_USAGE_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_BALANCE_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_BALANCE_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_CALIBRATION_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_CALIBRATION_INDEXES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_SQL_AUDIT_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_BP51_SQL_AUDIT_INDEXES_SQL:
                cur.execute(sql)
            # Add owner_id to build_audit if missing (check via PRAGMA table_info
            # then ALTER, same pattern as Data Owner column migrations)
            for table in ("build_audit",):
                cur.execute(f"PRAGMA table_info({table})")
                cols = {(r["name"] if hasattr(r, "keys") else r[1])
                        for r in cur.fetchall()}
                if "owner_id" not in cols:
                    try:
                        cur.execute(
                            f"ALTER TABLE {table} ADD COLUMN owner_id TEXT",
                        )
                        logger.info("migration: added %s.owner_id", table)
                    except sqlite3.OperationalError as exc:
                        logger.error(
                            "schema_migration_failed",
                            extra={
                                "backend": "sqlite", "table": table,
                                "column": "owner_id", "err": str(exc)[:200],
                            },
                        )
            for sql in _CREATE_BP51_VIEWS_SQL:
                cur.execute(sql)
            # Inline-embedding migration: add events.embedding / entities.embedding
            # BLOB columns to unify architecture with PostgresGraphStore.
            # New databases already have these in the CREATE TABLE definition;
            # existing databases receive a guarded ALTER TABLE.
            # BLOB columns can be added without the sqlite-vec extension loaded.
            for _tbl in ("events", "entities"):
                cur.execute(f"PRAGMA table_info({_tbl})")
                _ecols = {(r["name"] if hasattr(r, "keys") else r[1])
                          for r in cur.fetchall()}
                if "embedding" not in _ecols:
                    try:
                        cur.execute(f"ALTER TABLE {_tbl} ADD COLUMN embedding BLOB")
                        logger.info(
                            "inline-embedding: added %s.embedding column", _tbl,
                        )
                    except sqlite3.OperationalError as exc:
                        logger.error(
                            "schema_migration_failed",
                            extra={
                                "backend": "sqlite", "table": _tbl,
                                "column": "embedding", "err": str(exc)[:200],
                            },
                        )
            # If legacy vec0 virtual tables are still present, the database has
            # not been migrated — embeddings are not searchable.
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('events_vec', 'entities_vec')"
            )
            if cur.fetchall():
                logger.warning(
                    "legacy vec0 tables (events_vec/entities_vec) still present; "
                    "embeddings are NOT searchable until you run "
                    "`python -m k2g.db_store.sqlite.migrate_inline_embeddings "
                    "--db %s`",
                    self._path,
                )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        logger.info("SqliteGraphStore Tier1 schema initialized")

    def setup_training_schema(self) -> None:
        """Tier 2a ContextGroup schema setup."""
        cur = self._conn.cursor()
        try:
            for sql in _CREATE_TIER2A_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_TIER2A_INDEXES_SQL:
                cur.execute(sql)
            # Ensure owner columns are present on context_groups as well
            from k2g.security.data_owner import apply_data_owner_alters_sqlite
            apply_data_owner_alters_sqlite(cur)
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        logger.info("SqliteGraphStore Tier2a schema initialized")

    def setup_bp30_schema(self) -> None:
        """Tier 2b ETG / PlanNode / PlanDirectionNode schema setup."""
        cur = self._conn.cursor()
        try:
            for sql in _CREATE_TIER2B_TABLES_SQL:
                cur.execute(sql)
            for sql in _CREATE_TIER2B_INDEXES_SQL:
                cur.execute(sql)
            # Ensure owner columns are present on plan_nodes,
            # plan_direction_nodes, and event_template_groups
            from k2g.security.data_owner import apply_data_owner_alters_sqlite
            apply_data_owner_alters_sqlite(cur)
            # plan_nodes.parent_id enables a chain-to-tree upgrade
            cur.execute("PRAGMA table_info(plan_nodes)")
            _pcols = {(r["name"] if hasattr(r, "keys") else r[1])
                      for r in cur.fetchall()}
            if "parent_id" not in _pcols:
                try:
                    cur.execute(
                        "ALTER TABLE plan_nodes ADD COLUMN parent_id TEXT "
                        "REFERENCES plan_nodes(id) ON DELETE SET NULL",
                    )
                    logger.info("migration: added plan_nodes.parent_id")
                except sqlite3.OperationalError as exc:
                    logger.error(
                        "schema_migration_failed",
                        extra={
                            "backend": "sqlite", "table": "plan_nodes",
                            "column": "parent_id", "err": str(exc)[:200],
                        },
                    )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_nodes_parent "
                "ON plan_nodes(parent_id) WHERE parent_id IS NOT NULL",
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        logger.info("SqliteGraphStore Tier2b schema initialized")

    # ------------------------------------------------------------------
    # (b) Tier 1 writes
    # ------------------------------------------------------------------

    def ensure_group_tree(self, domain_config: DomainConfig) -> dict[str, str]:
        """Recursively insert the group_tree from a domain config into the groups table."""
        name_to_id: dict[str, str] = {}

        def _traverse(nodes, parent_id=None, path=None):
            if path is None:
                path = []
            for node in nodes:
                current_path = path + [node.name]
                summary = " > ".join(current_path)
                gid = self.link_or_create_group(
                    node.name, node.level, node.domain,
                    parent_id=parent_id, summary=summary,
                )
                name_to_id[node.name] = gid
                if node.children:
                    _traverse(node.children, parent_id=gid, path=current_path)

        _traverse(domain_config.group_tree)
        return name_to_id

    def link_or_create_group(
        self,
        name: str,
        level: int,
        domain: str,
        parent_id: str | None = None,
        summary: str = "",
        *,
        discriminator: str | None = None,
        original_name: str | None = None,
        source: str | None = None,
    ) -> str:
        """Return the id for the group with the given name, creating it if absent.

        ``name`` is the canonical key. Uses an atomic INSERT ... ON CONFLICT DO
        NOTHING upsert so that multiple concurrent processes creating the same
        name are safe. The previous SELECT-then-INSERT approach was a
        check-then-act race that caused ``UNIQUE constraint failed: groups.name``
        errors (and SQLite lock-upgrade deadlocks) during concurrent ingest tag
        resolution. Same pattern as ``link_or_create_entity``.
        """
        from k2g.core.models import new_group_id

        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO groups "
            "(id, name, level, domain, parent_id, discriminator, "
            " original_name, source, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
            (
                new_group_id(), name, level, domain, parent_id,
                discriminator or None,
                original_name or None,
                source or None,
                summary or None,
            ),
        )
        # If parent_id is provided and the row has no parent yet, fill it in.
        # Applies equally to newly inserted and pre-existing rows. Explicit
        # reparenting is handled by reparent_group.
        if parent_id:
            cur.execute(
                "UPDATE groups SET parent_id = ? "
                "WHERE name = ? AND (parent_id IS NULL OR parent_id = '')",
                (parent_id, name),
            )
        self._conn.commit()
        cur.execute("SELECT id FROM groups WHERE name = ?", (name,))
        return cur.fetchone()[0]

    def set_group_source(self, group_id: str, source: str) -> None:
        """Set a group's provenance ``source`` (axis label).

        Used to promote a Manager-added tag to the Discovery axis even when
        ``link_or_create_group``'s ON CONFLICT left a pre-existing row's source
        untouched.
        """
        self._conn.execute(
            "UPDATE groups SET source = ? WHERE id = ?", (source, group_id),
        )
        self._conn.commit()

    def register_path_group_with_ancestors(
        self,
        leaf_canonical: str,
        *,
        domain: str,
        summary: str = "",
    ) -> str:
        """Register group nodes for a canonical path and all its ancestors."""
        from k2g.db_store.group_id import expand_path_ancestors

        chain = expand_path_ancestors(leaf_canonical)
        prev_gid: str | None = None
        leaf_gid: str = ""
        for level, (canonical, _is_dir) in enumerate(chain):
            gid = self.link_or_create_group(
                name=canonical,
                level=level,
                domain=domain,
                parent_id=prev_gid,
                summary=summary if canonical == leaf_canonical else "",
                discriminator="path",
                source="build",  # path groups come from code ingestion structure
            )
            prev_gid = gid
            leaf_gid = gid
        return leaf_gid

    def get_group_id(self, group_name: str, domain: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id FROM groups WHERE name = ? AND domain = ?",
            (group_name, domain),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def link_or_create_entity(
        self, name: str, domain: str, type: str | None = None,
    ) -> str:
        """Return the id for the (name, domain) entity, creating it if absent.

        Uses an atomic INSERT ... ON CONFLICT DO NOTHING upsert so multiple
        concurrent processes creating the same (name, domain) pair are safe.
        The old SELECT-then-INSERT approach was a check-then-act race that
        caused ``UNIQUE constraint failed`` errors (and ``database is locked``
        deadlocks from SQLite lock upgrades) during concurrent ingest. Inserting
        first eliminates the shared-lock prior, removing the deadlock.
        Same ON CONFLICT pattern as ``add_entities_bulk``.
        """
        from k2g.core.models import new_entity_id

        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO entities (id, name, domain, type) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name, domain) DO NOTHING",
            (new_entity_id(), name, domain, type or ""),
        )
        self._conn.commit()
        # Fetch the authoritative id regardless of whether the row was just
        # inserted or already existed.
        cur.execute(
            "SELECT id FROM entities WHERE name = ? AND domain = ?",
            (name, domain),
        )
        return cur.fetchone()[0]

    def create_event(
        self,
        vector_id: str,
        domain: str,
        timestamp: str | None,
        order_index: int,
        summary: str = "",
        *,
        owner_id: str | None = None,
        org_id: str | None = None,
        visibility: str = "public",
        acl_json: str | None = None,
        share_group_id: str | None = None,
        # NER metadata — activates columns that already exist in the SQLite schema
        ner_method: str | None = None,
        ner_skip_reason: str | None = None,
    ) -> str:
        """Create an event row with owner and visibility columns.

        When ``owner_id`` is not provided it defaults to NULL, which the
        visibility policy treats as 'public'.
        ``ner_method`` / ``ner_skip_reason`` populate the pre-existing NER
        metadata columns.
        """
        from k2g.core.models import new_event_id

        event_id = new_event_id()
        self._conn.execute(
            """
            INSERT INTO events (id, vector_id, domain, timestamp, order_index,
                                summary, owner_id, org_id, visibility,
                                acl_json, share_group_id,
                                ner_method, ner_skip_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, vector_id, domain,
             str(timestamp) if timestamp else "",
             order_index, summary,
             owner_id, org_id, visibility, acl_json, share_group_id,
             ner_method, ner_skip_reason),
        )
        self._conn.commit()
        return event_id

    def upsert_entity_connection(
        self, entity_id_a: str, entity_id_b: str, event_id: str,
    ) -> None:
        """Upsert an entity_connection row with a_id < b_id ordering and event_count += 1."""
        a, b = (entity_id_a, entity_id_b) if entity_id_a < entity_id_b else (entity_id_b, entity_id_a)
        self._conn.execute(
            """
            INSERT INTO entity_connection (a_id, b_id, event_count)
            VALUES (?, ?, 1)
            ON CONFLICT(a_id, b_id) DO UPDATE SET event_count = event_count + 1
            """,
            (a, b),
        )
        self._conn.commit()

    def link_participated_in(self, entity_id: str, event_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO participated_in (entity_id, event_id)
            VALUES (?, ?)
            ON CONFLICT(entity_id, event_id) DO NOTHING
            """,
            (entity_id, event_id),
        )
        self._conn.commit()

    def link_event_member_of(
        self,
        event_id: str,
        group_id: str,
        *,
        kind: Literal["contains", "refers"] = "contains",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO event_member_of (event_id, group_id, kind)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id, group_id) DO NOTHING
            """,
            (event_id, group_id, kind),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Bulk insert APIs (executemany-based)
    # ------------------------------------------------------------------
    # These methods do NOT commit on their own — callers should wrap them in a
    # ``db.session()`` context manager to group them into a single transaction.

    def add_events_bulk(self, rows: list[dict[str, Any]]) -> list[str]:
        """Bulk-insert events via executemany. Auto-generates id when absent.

        Owner / visibility columns (owner_id, org_id, visibility, acl_json,
        share_group_id) are included in the INSERT when present in the row
        dict; otherwise NULL / 'public' defaults apply.

        Inline embedding: when a row contains an ``embedding`` (list[float])
        it is serialized and written to events.embedding at INSERT time.
        This avoids the issue where the bulk path calls vector upsert before
        creating the events row (a separate, uncommitted connection makes
        that a no-op). Omitted rows receive NULL.
        PgGraphStore.add_events_bulk ignores this key, so PG is unaffected.
        """
        from k2g.core.models import new_event_id
        out_ids: list[str] = []
        params: list[tuple[Any, ...]] = []
        _serialize = None
        for row in rows:
            ev_id = row.get("id") or new_event_id()
            out_ids.append(ev_id)
            emb = row.get("embedding")
            emb_blob = None
            if emb is not None:
                if _serialize is None:
                    import sqlite_vec  # type: ignore[import-not-found]
                    _serialize = sqlite_vec.serialize_float32
                emb_blob = _serialize(list(emb))
            params.append((
                ev_id,
                row.get("vector_id", ""),
                row.get("domain", ""),
                str(row.get("timestamp") or "") or "",
                row.get("order_index", 0),
                row.get("summary", ""),
                row.get("owner_id"),
                row.get("org_id"),
                row.get("visibility") or "public",
                row.get("acl_json"),
                row.get("share_group_id"),
                # NER metadata columns
                row.get("ner_method"),
                row.get("ner_skip_reason"),
                emb_blob,
            ))
        if not params:
            return out_ids
        # ON CONFLICT(id) DO NOTHING: deterministic pre-assigned ids (manifest
        # dedup) are silently skipped on replay. UUID ids from the standard
        # producer never conflict, so they are unaffected. out_ids holds the
        # pre-determined ids; even skipped rows return the correct id so
        # downstream links (participated_in / member_of / sequential_next, all
        # with ON CONFLICT DO NOTHING) re-attach idempotently to the existing event.
        self._conn.executemany(
            """
            INSERT INTO events (id, vector_id, domain, timestamp, order_index,
                                summary, owner_id, org_id, visibility,
                                acl_json, share_group_id,
                                ner_method, ner_skip_reason, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            params,
        )
        return out_ids

    def add_entities_bulk(self, rows: list[dict[str, Any]]) -> list[str]:
        """Bulk-insert entities. Existing (name, domain) pairs are skipped via
        INSERT OR IGNORE — the caller is responsible for managing the id mapping
        separately."""
        from k2g.core.models import new_entity_id
        out_ids: list[str] = []
        params: list[tuple[Any, ...]] = []
        for row in rows:
            ent_id = row.get("id") or new_entity_id()
            out_ids.append(ent_id)
            params.append((
                ent_id,
                row.get("name", ""),
                row.get("domain", ""),
                row.get("type", "") or "",
            ))
        if not params:
            return out_ids
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO entities (id, name, domain, type)
            VALUES (?, ?, ?, ?)
            """,
            params,
        )
        return out_ids

    def link_event_member_of_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk-insert event_member_of rows. Duplicates are silently skipped."""
        params = [
            (r["event_id"], r["group_id"], r.get("kind", "contains"))
            for r in rows
        ]
        if not params:
            return 0
        self._conn.executemany(
            """
            INSERT INTO event_member_of (event_id, group_id, kind)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id, group_id) DO NOTHING
            """,
            params,
        )
        return len(params)

    def link_or_create_entities_bulk(
        self, rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], str]:
        """Bulk MERGE for entities. Returns a ``{(name, domain): id}`` mapping.

        SQLite supports RETURNING (3.35+) but not in combination with
        executemany, so the implementation uses INSERT OR IGNORE followed by
        a separate SELECT.
        """
        from k2g.core.models import new_entity_id
        if not rows:
            return {}
        # Deduplicate rows with the same (name, domain)
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row.get("name", ""), row.get("domain", ""))
            if key not in unique:
                unique[key] = row
        params = [
            (
                new_entity_id(),
                key[0], key[1],
                unique[key].get("type", "") or "",
            )
            for key in unique
        ]
        self._conn.executemany(
            """
            INSERT OR IGNORE INTO entities (id, name, domain, type)
            VALUES (?, ?, ?, ?)
            """,
            params,
        )
        # Fetch the authoritative ids (existing id on conflict, new id otherwise)
        keys = list(unique.keys())
        placeholders = ",".join(["(?, ?)"] * len(keys))
        flat: list[Any] = []
        for name, domain in keys:
            flat.extend([name, domain])
        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT name, domain, id FROM entities
            WHERE (name, domain) IN ({placeholders})
            """,
            flat,
        )
        return {(name, domain): eid for (name, domain, eid) in cur.fetchall()}

    def link_participated_in_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk-insert participated_in rows."""
        params = [(r["entity_id"], r["event_id"]) for r in rows]
        if not params:
            return 0
        self._conn.executemany(
            """
            INSERT INTO participated_in (entity_id, event_id)
            VALUES (?, ?)
            ON CONFLICT(entity_id, event_id) DO NOTHING
            """,
            params,
        )
        return len(params)

    def upsert_entity_connections_bulk(
        self, rows: list[dict[str, Any]],
    ) -> int:
        """Bulk UPSERT entity_connection rows with cumulative event_count.

        ``rows = [{"a_id", "b_id", "count"}]``. The caller is responsible for
        ensuring a_id < b_id ordering and for summing counts for duplicate
        (a, b) pairs before calling this method.
        """
        params = [
            (r["a_id"], r["b_id"], int(r.get("count", 1)))
            for r in rows
        ]
        if not params:
            return 0
        self._conn.executemany(
            """
            INSERT INTO entity_connection (a_id, b_id, event_count)
            VALUES (?, ?, ?)
            ON CONFLICT(a_id, b_id)
            DO UPDATE SET event_count = entity_connection.event_count + excluded.event_count
            """,
            params,
        )
        return len(params)

    def link_sequential_next_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk-insert event_sequential_next rows."""
        params = [
            (r["prev_id"], r["next_id"], r.get("source", "chunk_order"))
            for r in rows
        ]
        if not params:
            return 0
        self._conn.executemany(
            """
            INSERT INTO event_sequential_next (prev_id, next_id, source)
            VALUES (?, ?, ?)
            ON CONFLICT(prev_id, next_id, source) DO NOTHING
            """,
            params,
        )
        return len(params)

    def reparent_group(self, group_id: str, new_parent_id: str) -> None:
        self._conn.execute(
            "UPDATE groups SET parent_id = ? WHERE id = ?",
            (new_parent_id, group_id),
        )
        self._conn.commit()

    def link_sequential_next(
        self,
        prev_event_id: str,
        event_id: str,
        source: SequentialSource = "chunk_order",
    ) -> None:
        """Insert an EVENT_SEQUENTIAL_NEXT edge. The schema CHECK allows 7 source values."""
        # Allowed sources: 'chunk_order','file_name','folder_name','thread',
        # 'topic_segment','user_manual','version up'.
        self._conn.execute(
            """
            INSERT INTO event_sequential_next (prev_id, next_id, source)
            VALUES (?, ?, ?)
            ON CONFLICT(prev_id, next_id, source) DO NOTHING
            """,
            (prev_event_id, event_id, source),
        )
        self._conn.commit()

    def set_user_tag(
        self, node_type: str, node_id: str, user_tag: str | None,
    ) -> None:
        table = {
            "entity": "entities", "entities": "entities",
            "group": "groups", "groups": "groups",
        }.get(node_type)
        if table is None:
            raise ValueError(f"set_user_tag: unsupported node_type={node_type!r}")
        self._conn.execute(
            f"UPDATE {table} SET user_tag = ? WHERE id = ?",
            (user_tag, node_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # BP-87 — Entity merge link replacement (curation track)
    # ------------------------------------------------------------------

    def replace_entity_in_links(
        self, alias_id: str, canonical_id: str,
    ) -> dict[str, int]:
        """Move all ``participated_in`` / ``entity_connection`` rows pointing
        at ``alias_id`` to ``canonical_id``. Idempotent, transaction-safe
        (caller should wrap in ``db.session()``).

        Strategy:
          1. ``participated_in``: delete rows where canonical already has the
             event (no duplicate possible due to PK), then UPDATE remaining
             alias rows.
          2. ``entity_connection``: re-insert each alias-touching pair as a
             canonical pair (with proper ordering for CHECK a_id < b_id),
             using ``ON CONFLICT(a_id, b_id) DO UPDATE SET event_count =
             event_count + excluded.event_count`` to merge duplicates.
             Drop the self-loop case (alias↔canonical pair) and clean up
             leftover alias rows.

        Returns counts dict for audit / logging:
            {
                'participated_in_moved', 'participated_in_dedup',
                'connections_moved', 'connections_merged',
            }
        """
        if alias_id == canonical_id:
            raise ValueError("alias and canonical must differ")

        cur = self._conn.cursor()

        # --- 1. participated_in ---
        # 1a. Dedup: drop alias rows for events canonical already has.
        cur.execute(
            "DELETE FROM participated_in "
            "WHERE entity_id = ? "
            "  AND event_id IN ("
            "    SELECT event_id FROM participated_in WHERE entity_id = ?"
            "  )",
            (alias_id, canonical_id),
        )
        pi_dedup = cur.rowcount or 0
        # 1b. Update the rest.
        cur.execute(
            "UPDATE participated_in SET entity_id = ? WHERE entity_id = ?",
            (canonical_id, alias_id),
        )
        pi_moved = cur.rowcount or 0

        # --- 2. entity_connection ---
        # 2a. Move alias-touching rows (excluding alias↔canonical pair) to
        #     canonical, swapping order as needed to honor CHECK a_id < b_id,
        #     and merging event_count on conflict.
        cur.execute(
            """
            INSERT INTO entity_connection (a_id, b_id, event_count, created_at)
            SELECT
                CASE
                    WHEN ? < (CASE WHEN a_id = ? THEN b_id ELSE a_id END)
                        THEN ?
                    ELSE (CASE WHEN a_id = ? THEN b_id ELSE a_id END)
                END AS new_a,
                CASE
                    WHEN ? < (CASE WHEN a_id = ? THEN b_id ELSE a_id END)
                        THEN (CASE WHEN a_id = ? THEN b_id ELSE a_id END)
                    ELSE ?
                END AS new_b,
                event_count,
                created_at
            FROM entity_connection
            WHERE (a_id = ? OR b_id = ?)
              AND NOT (a_id = ? AND b_id = ?)
              AND NOT (a_id = ? AND b_id = ?)
            ON CONFLICT(a_id, b_id) DO UPDATE
                SET event_count = entity_connection.event_count + excluded.event_count
            """,
            (
                canonical_id, alias_id, canonical_id, alias_id,
                canonical_id, alias_id, alias_id, canonical_id,
                alias_id, alias_id,
                alias_id, canonical_id,
                canonical_id, alias_id,
            ),
        )
        ec_moved = cur.rowcount or 0
        # 2b. Remove all old alias-touching rows (including alias↔canonical).
        cur.execute(
            "DELETE FROM entity_connection WHERE a_id = ? OR b_id = ?",
            (alias_id, alias_id),
        )
        ec_merged = cur.rowcount or 0

        return {
            "participated_in_moved": pi_moved,
            "participated_in_dedup": pi_dedup,
            "connections_moved": ec_moved,
            "connections_merged": ec_merged,
        }

    # ------------------------------------------------------------------
    # (c) Tier 1 queries / statistics
    # ------------------------------------------------------------------

    def get_last_event_id(self, domain: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id FROM events WHERE domain = ? AND deprecated = 0 "
            "ORDER BY order_index DESC LIMIT 1",
            (domain,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def get_order_index(self, domain: str) -> int:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM events "
            "WHERE domain = ? AND deprecated = 0",
            (domain,),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_entity_by_id(self, entity_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_entity_event_counts(
        self, entity_ids: list[str],
    ) -> dict[str, int]:
        if not entity_ids:
            return {}
        placeholders = ",".join(["?"] * len(entity_ids))
        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT entity_id, COUNT(*)
            FROM participated_in
            WHERE entity_id IN ({placeholders})
            GROUP BY entity_id
            """,
            tuple(entity_ids),
        )
        out: dict[str, int] = {eid: 0 for eid in entity_ids}
        for row in cur.fetchall():
            out[str(row[0])] = int(row[1] or 0)
        return out

    # ------------------------------------------------------------------
    # BP-89 — Train run audit
    # ------------------------------------------------------------------

    def train_run_start(
        self,
        kind: str,
        domain: str | None,
        *,
        params: dict[str, Any] | None = None,
        triggered_by: str | None = None,
        event_count: int | None = None,
        entity_count: int | None = None,
        edge_count: int | None = None,
    ) -> str:
        import json
        import uuid
        run_id = f"run_{uuid.uuid4().hex[:24]}"
        self._conn.execute(
            """
            INSERT INTO train_run (
                id, kind, domain, status,
                event_count, entity_count, edge_count,
                params_json, triggered_by
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                run_id, kind, domain,
                event_count, entity_count, edge_count,
                json.dumps(params) if params is not None else None,
                triggered_by,
            ),
        )
        self._conn.commit()
        return run_id

    def train_run_complete(
        self,
        run_id: str,
        *,
        metrics: dict[str, Any] | None = None,
        rows_written: int | None = None,
    ) -> None:
        import json
        self._conn.execute(
            """
            UPDATE train_run
            SET status = 'completed',
                finished_at = CURRENT_TIMESTAMP,
                metrics_json = ?,
                rows_written = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                json.dumps(metrics) if metrics is not None else None,
                rows_written,
                run_id,
            ),
        )
        self._conn.commit()

    def train_run_fail(self, run_id: str, error_text: str) -> None:
        self._conn.execute(
            """
            UPDATE train_run
            SET status = 'failed',
                finished_at = CURRENT_TIMESTAMP,
                error_text = ?
            WHERE id = ? AND status = 'running'
            """,
            (error_text, run_id),
        )
        self._conn.commit()

    def train_run_latest_completed(
        self,
        kind: str,
        *,
        domain: str | None = None,
    ) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        if domain is None:
            cur.execute(
                """
                SELECT * FROM train_run
                WHERE kind = ? AND domain IS NULL AND status = 'completed'
                ORDER BY finished_at DESC LIMIT 1
                """,
                (kind,),
            )
        else:
            cur.execute(
                """
                SELECT * FROM train_run
                WHERE kind = ? AND domain = ? AND status = 'completed'
                ORDER BY finished_at DESC LIMIT 1
                """,
                (kind, domain),
            )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def upsert_entity_community_assignments(
        self,
        run_id: str,
        assignments: list[tuple[str, int]],
    ) -> int:
        if not assignments:
            return 0
        rows = [(run_id, eid, int(cid)) for eid, cid in assignments]
        self._conn.executemany(
            """
            INSERT INTO entity_community_assignment (run_id, entity_id, community_id)
            VALUES (?, ?, ?)
            ON CONFLICT(run_id, entity_id) DO UPDATE
                SET community_id = excluded.community_id
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    def get_entity_community_for_run(
        self,
        run_id: str,
        entity_ids: list[str],
    ) -> dict[str, int]:
        if not entity_ids:
            return {}
        placeholders = ",".join(["?"] * len(entity_ids))
        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT entity_id, community_id
            FROM entity_community_assignment
            WHERE run_id = ? AND entity_id IN ({placeholders})
            """,
            (run_id, *entity_ids),
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}

    def upsert_event_community_assignments(
        self,
        run_id: str,
        assignments: list[tuple[str, int]],
    ) -> int:
        if not assignments:
            return 0
        rows = [(run_id, eid, int(cid)) for eid, cid in assignments]
        self._conn.executemany(
            """
            INSERT INTO event_community_assignment (run_id, event_id, community_id)
            VALUES (?, ?, ?)
            ON CONFLICT(run_id, event_id) DO UPDATE
                SET community_id = excluded.community_id
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    def get_event_community_for_run(
        self,
        run_id: str,
        event_ids: list[str],
    ) -> dict[str, int]:
        if not event_ids:
            return {}
        placeholders = ",".join(["?"] * len(event_ids))
        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT event_id, community_id
            FROM event_community_assignment
            WHERE run_id = ? AND event_id IN ({placeholders})
            """,
            (run_id, *event_ids),
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}

    def list_community_members(
        self,
        run_id: str,
        node_kind: str,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        # Compute weight with a single GROUP BY aggregate + LEFT JOIN rather
        # than a correlated subquery per row (N members → 1 aggregate pass).
        if node_kind == "entity":
            cur.execute(
                """
                SELECT a.entity_id, a.community_id, e.name,
                  COALESCE(w.c, 0) AS weight
                FROM entity_community_assignment a
                JOIN entities e ON e.id = a.entity_id
                LEFT JOIN (
                    SELECT entity_id, COUNT(*) AS c FROM participated_in
                    GROUP BY entity_id
                ) w ON w.entity_id = a.entity_id
                WHERE a.run_id = ?
                """,
                (run_id,),
            )
        elif node_kind == "event":
            cur.execute(
                """
                SELECT a.event_id, a.community_id, ev.summary,
                  COALESCE(w.c, 0) AS weight
                FROM event_community_assignment a
                JOIN events ev ON ev.id = a.event_id
                LEFT JOIN (
                    SELECT event_id, COUNT(*) AS c FROM participated_in
                    GROUP BY event_id
                ) w ON w.event_id = a.event_id
                WHERE a.run_id = ?
                """,
                (run_id,),
            )
        else:
            raise ValueError(f"list_community_members: bad node_kind={node_kind!r}")
        return [
            {
                "node_id": str(r[0]),
                "community_id": int(r[1]),
                "name": r[2] or "",
                "weight": int(r[3] or 0),
            }
            for r in cur.fetchall()
        ]

    def get_event_by_id(self, event_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_connected_entities(
        self, entity_id: str, max_hops: int = 2,
    ) -> list[dict[str, Any]]:
        """BFS over entity_connection (symmetric: a_id < b_id) up to max_hops."""
        cur = self._conn.cursor()
        hops = max(1, min(int(max_hops), 5))
        cur.execute(
            f"""
            WITH RECURSIVE reach(id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT CASE WHEN ec.a_id = reach.id THEN ec.b_id ELSE ec.a_id END,
                       reach.depth + 1
                FROM entity_connection ec
                JOIN reach ON (ec.a_id = reach.id OR ec.b_id = reach.id)
                WHERE reach.depth < {hops}
            )
            SELECT e.* FROM entities e
            JOIN reach ON e.id = reach.id
            WHERE reach.id != ?
            """,
            (entity_id, entity_id),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def list_entity_connections(self, domain: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT ec.a_id, ec.b_id, ec.event_count
              FROM entity_connection ec
              JOIN entities ea ON ea.id = ec.a_id
              JOIN entities eb ON eb.id = ec.b_id
             WHERE ea.domain = ? AND eb.domain = ?
               AND (ea.deprecated IS NULL OR ea.deprecated = 0)
               AND (eb.deprecated IS NULL OR eb.deprecated = 0)
             ORDER BY ec.event_count DESC
            """,
            (domain, domain),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def list_entity_connection_events(
        self, a_id: str, b_id: str, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT e.*
              FROM events e
              JOIN participated_in p1 ON p1.event_id = e.id AND p1.entity_id = ?
              JOIN participated_in p2 ON p2.event_id = e.id AND p2.entity_id = ?
             WHERE (e.deprecated IS NULL OR e.deprecated = 0)
             ORDER BY e.timestamp DESC, e.order_index DESC
             LIMIT ?
            """,
            (a_id, b_id, int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def list_events_paged(
        self,
        domain: str,
        *,
        order_by: str = "timestamp",
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        # Allowlist guards SQL injection via dynamic ORDER BY column.
        allowed = {"timestamp", "order_index", "created_at"}
        col = order_by if order_by in allowed else "timestamp"
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM events WHERE domain = ? "
            "AND (deprecated IS NULL OR deprecated = 0)",
            (domain,),
        )
        total = int(cur.fetchone()[0] or 0)
        offset = max(0, (int(page) - 1) * int(size))
        cur.execute(
            f"SELECT * FROM events WHERE domain = ? "
            f"AND (deprecated IS NULL OR deprecated = 0) "
            f"ORDER BY {col} DESC LIMIT ? OFFSET ?",
            (domain, int(size), offset),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows, total

    def get_statistics(self, domain: str | None = None) -> dict[str, int]:
        cur = self._conn.cursor()
        where = "WHERE domain = ?" if domain else ""
        params: tuple[Any, ...] = (domain,) if domain else ()
        stats: dict[str, int] = {}
        for table in ("entities", "events", "groups", "context_groups"):
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table} {where}", params)
                stats[table] = int(cur.fetchone()[0] or 0)
            except sqlite3.Error:
                stats[table] = 0
        return stats

    def get_all_entity_names(self, limit: int = 10000) -> list[str]:
        cur = self._conn.cursor()
        cur.execute("SELECT name FROM entities LIMIT ?", (int(limit),))
        return [row[0] for row in cur.fetchall()]

    def list_entities(
        self,
        domain: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        cur = self._conn.cursor()
        conds: list[str] = []
        params: list[Any] = []
        if domain:
            conds.append("domain = ?")
            params.append(domain)
        if not include_deleted:
            conds.append("(deprecated IS NULL OR deprecated = 0)")
        where = f"WHERE {' AND '.join(conds)}" if conds else ""

        cur.execute(f"SELECT COUNT(*) FROM entities {where}", params)
        total = int(cur.fetchone()[0] or 0)

        offset = max(0, (int(page) - 1) * int(size))
        cur.execute(
            f"SELECT * FROM entities {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, int(size), offset),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows, total

    def search_entities_by_name(
        self, name: str, domain: str | list[str] | None = None, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search. Uses IN when domain is a list, = otherwise."""
        cur = self._conn.cursor()
        conds = ["LOWER(name) LIKE ?"]
        params: list[Any] = [f"%{name.lower()}%"]
        if domain:
            if isinstance(domain, list):
                placeholders = ", ".join(["?"] * len(domain))
                conds.append(f"domain IN ({placeholders})")
                params.extend(domain)
            else:
                conds.append("domain = ?")
                params.append(domain)
        cur.execute(
            f"SELECT * FROM entities WHERE {' AND '.join(conds)} LIMIT ?",
            (*params, int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def list_groups(self, domain: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM groups WHERE domain = ? ORDER BY parent_id, id",
            (domain,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def search_groups_by_name(
        self,
        name: str,
        domain: str | list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search over group names (tags)."""
        cur = self._conn.cursor()
        # SQLite may store deprecated as 'f'/'t' strings; compare against
        # multiple forms to avoid silently dropping rows.
        conds = ["LOWER(name) LIKE ?", "(deprecated IN (0, 'f', 'false') OR deprecated IS NULL)"]
        params: list[Any] = [f"%{name.lower()}%"]
        if domain:
            if isinstance(domain, list):
                placeholders = ", ".join(["?"] * len(domain))
                conds.append(f"domain IN ({placeholders})")
                params.extend(domain)
            else:
                conds.append("domain = ?")
                params.append(domain)
        cur.execute(
            f"SELECT * FROM groups WHERE {' AND '.join(conds)} "
            f"ORDER BY LENGTH(name) ASC, name ASC LIMIT ?",
            (*params, int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def merge_groups(self, alias_id: str, canonical_id: str) -> dict[str, Any]:
        """Merge alias tag into canonical: re-point memberships, re-parent the
        alias's children, soft-delete the alias. Levels are recomputed since
        re-parenting can change subtree depth.

        (groups.name is globally UNIQUE, so two tags can never share a child
        name — no same-name child collision is possible.)
        """
        if alias_id == canonical_id:
            raise ValueError("alias and canonical must differ")
        try:
            cur = self._conn.cursor()
            a = cur.execute("SELECT deprecated FROM groups WHERE id = ?", (alias_id,)).fetchone()
            c = cur.execute(
                "SELECT deprecated, domain FROM groups WHERE id = ?", (canonical_id,)
            ).fetchone()
            if a is None:
                raise ValueError(f"alias group not found: {alias_id}")
            if c is None:
                raise ValueError(f"canonical group not found: {canonical_id}")
            if c[0]:
                raise ValueError(f"canonical group is deprecated: {canonical_id}")
            cur.execute(
                "DELETE FROM event_member_of WHERE group_id = ? "
                "AND event_id IN (SELECT event_id FROM event_member_of WHERE group_id = ?)",
                (alias_id, canonical_id),
            )
            dedup = cur.rowcount or 0
            cur.execute(
                "UPDATE event_member_of SET group_id = ? WHERE group_id = ?",
                (canonical_id, alias_id),
            )
            moved = cur.rowcount or 0
            cur.execute(
                "UPDATE groups SET parent_id = ? WHERE parent_id = ?",
                (canonical_id, alias_id),
            )
            reparented = cur.rowcount or 0
            cur.execute("UPDATE groups SET deprecated = 1 WHERE id = ?", (alias_id,))
            if c[1]:
                self._recompute_levels_tx(cur, c[1])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {
            "alias_id": alias_id, "canonical_id": canonical_id,
            "memberships_moved": int(moved), "memberships_dedup": int(dedup),
            "children_reparented": int(reparented),
        }

    def _descendant_ids(self, cur: Any, group_id: str) -> set[str]:
        rows = cur.execute(
            """
            WITH RECURSIVE d(id) AS (
                SELECT id FROM groups WHERE parent_id = ?
                UNION ALL
                SELECT g.id FROM groups g JOIN d ON g.parent_id = d.id
            )
            SELECT id FROM d
            """,
            (group_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def move_group(self, group_id: str, new_parent_id: str | None) -> dict[str, Any]:
        """Re-parent a tag (and its whole subtree) under ``new_parent_id``.

        ``new_parent_id=None`` moves it to the domain root. Rejects moves that
        would create a cycle (under itself or a descendant). Recomputes levels.
        """
        cur = self._conn.cursor()
        g = cur.execute("SELECT domain FROM groups WHERE id = ?", (group_id,)).fetchone()
        if g is None:
            raise ValueError(f"group not found: {group_id}")
        domain = g[0]
        new_parent_id = new_parent_id or None
        if new_parent_id:
            if new_parent_id == group_id:
                raise ValueError("cannot move a tag under itself")
            p = cur.execute(
                "SELECT id FROM groups WHERE id = ?", (new_parent_id,)
            ).fetchone()
            if p is None:
                raise ValueError(f"new parent not found: {new_parent_id}")
            if new_parent_id in self._descendant_ids(cur, group_id):
                raise ValueError("cannot move a tag under its own descendant")
        try:
            cur.execute(
                "UPDATE groups SET parent_id = ? WHERE id = ?",
                (new_parent_id, group_id),
            )
            self._recompute_levels_tx(cur, domain)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {"group_id": group_id, "new_parent_id": new_parent_id, "domain": domain}

    def _recompute_levels_tx(self, cur: Any, domain: str) -> int:
        """Recompute ``groups.level`` = tree depth (root=0) for one domain."""
        rows = cur.execute(
            "SELECT id, parent_id FROM groups WHERE domain = ?", (domain,)
        ).fetchall()
        parent = {r[0]: (r[1] or None) for r in rows}
        memo: dict[str, int] = {}

        def depth(gid: str, seen: set[str]) -> int:
            if gid in memo:
                return memo[gid]
            p = parent.get(gid)
            if p is None or p not in parent or p in seen:
                memo[gid] = 0          # root, cross-domain parent, or cycle
                return 0
            seen.add(gid)
            memo[gid] = depth(p, seen) + 1
            return memo[gid]

        updates = [(depth(gid, set()), gid) for gid in parent]
        cur.executemany("UPDATE groups SET level = ? WHERE id = ?", updates)
        return len(updates)

    def recompute_group_levels(self, domain: str) -> dict[str, Any]:
        """Public maintenance entry: refresh ``level`` for an entire domain."""
        try:
            cur = self._conn.cursor()
            n = self._recompute_levels_tx(cur, domain)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {"domain": domain, "groups_releveled": int(n)}

    def list_event_ids_by_group(
        self, group_id_or_name: "str | list[str]",
    ) -> list[str]:
        """Return deduplicated event ids for a group and all its descendants.

        Matches by groups.id or groups.name. When a list is given, returns
        the union of sub-trees for each group.
        """
        if isinstance(group_id_or_name, str):
            seeds = [group_id_or_name]
        else:
            seeds = list(group_id_or_name)
        if not seeds:
            return []
        cur = self._conn.cursor()
        placeholders = ",".join(["?"] * len(seeds))
        cur.execute(
            f"""
            WITH RECURSIVE descendants(id) AS (
                SELECT id FROM groups WHERE id IN ({placeholders}) OR name IN ({placeholders})
                UNION ALL
                SELECT g.id FROM groups g JOIN descendants d ON g.parent_id = d.id
            )
            SELECT DISTINCT em.event_id
              FROM event_member_of em
             WHERE em.group_id IN (SELECT id FROM descendants)
            """,
            (*seeds, *seeds),
        )
        return [r[0] for r in cur.fetchall()]

    def get_entity_events(
        self,
        entity_id: str,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        cur = self._conn.cursor()
        deleted_filter = "" if include_deleted else " AND (e.deprecated IS NULL OR e.deprecated = 0)"
        cur.execute(
            f"SELECT COUNT(*) FROM participated_in pi "
            f"JOIN events e ON e.id = pi.event_id "
            f"WHERE pi.entity_id = ?{deleted_filter}",
            (entity_id,),
        )
        total = int(cur.fetchone()[0] or 0)

        offset = max(0, (int(page) - 1) * int(size))
        cur.execute(
            f"SELECT e.* FROM participated_in pi "
            f"JOIN events e ON e.id = pi.event_id "
            f"WHERE pi.entity_id = ?{deleted_filter} "
            f"ORDER BY e.order_index DESC LIMIT ? OFFSET ?",
            (entity_id, int(size), offset),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows, total

    def get_group_events(
        self,
        group_id: str,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        cur = self._conn.cursor()
        deleted_filter = "" if include_deleted else " AND (e.deprecated IS NULL OR e.deprecated = 0)"
        cur.execute(
            f"SELECT COUNT(*) FROM event_member_of m "
            f"JOIN events e ON e.id = m.event_id "
            f"WHERE m.group_id = ?{deleted_filter}",
            (group_id,),
        )
        total = int(cur.fetchone()[0] or 0)

        offset = max(0, (int(page) - 1) * int(size))
        cur.execute(
            f"SELECT e.* FROM event_member_of m "
            f"JOIN events e ON e.id = m.event_id "
            f"WHERE m.group_id = ?{deleted_filter} "
            f"ORDER BY e.order_index DESC LIMIT ? OFFSET ?",
            (group_id, int(size), offset),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows, total

    def get_adjacent_events(self, event_id: str) -> dict[str, Any]:
        # JOIN events filters out inaccessible or absent events from
        # prev_id/next_id. event_sequential_next has no RLS filter, so
        # returning raw ids without a JOIN would leak blocked event ids
        # (fail-open) and create dangling references in multi-tenant Postgres
        # environments. The JOIN acts as the RLS filter. In SQLite the
        # query_filter is a no-op, so behavior is identical.
        cur = self._conn.cursor()
        cur.execute(
            "SELECT sn.prev_id FROM event_sequential_next sn "
            "JOIN events e ON e.id = sn.prev_id "
            "WHERE sn.next_id = ? LIMIT 1",
            (event_id,),
        )
        prev = cur.fetchone()
        cur.execute(
            "SELECT sn.next_id FROM event_sequential_next sn "
            "JOIN events e ON e.id = sn.next_id "
            "WHERE sn.prev_id = ? LIMIT 1",
            (event_id,),
        )
        nxt = cur.fetchone()
        return {
            "prev_id": prev[0] if prev else None,
            "next_id": nxt[0] if nxt else None,
        }

    def get_event_context(self, event_id: str) -> dict[str, Any]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT e.id, e.name FROM participated_in pi "
            "JOIN entities e ON e.id = pi.entity_id WHERE pi.event_id = ?",
            (event_id,),
        )
        entities = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        cur.execute(
            "SELECT g.id, g.name FROM event_member_of m "
            "JOIN groups g ON g.id = m.group_id WHERE m.event_id = ?",
            (event_id,),
        )
        groups = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        return {"entities": entities, "groups": groups}

    def list_domains(self) -> list[str]:
        cur = self._conn.cursor()
        cur.execute("SELECT DISTINCT domain FROM events ORDER BY domain")
        return [row[0] for row in cur.fetchall() if row[0]]

    # --- Domain soft-registry (empty-domain support) ------------------

    def register_domain(self, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO domain_registry (name) VALUES (?)", (name,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_registered_domains(self) -> list[str]:
        cur = self._conn.cursor()
        cur.execute("SELECT name FROM domain_registry ORDER BY name")
        return [row[0] for row in cur.fetchall() if row[0]]

    def list_managed_domains(self) -> list[str]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT name FROM domain_registry "
            "UNION SELECT domain FROM events "
            "UNION SELECT domain FROM entities "
            "UNION SELECT domain FROM groups",
        )
        return sorted({row[0] for row in cur.fetchall() if row[0]})

    def domain_data_counts(self, name: str) -> dict[str, int]:
        cur = self._conn.cursor()

        def _count(table: str) -> int:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE domain = ?", (name,))
            return int(cur.fetchone()[0])

        return {
            "events": _count("events"),
            "entities": _count("entities"),
            "groups": _count("groups"),
        }

    def delete_domain(self, name: str) -> dict[str, Any]:
        counts = self.domain_data_counts(name)
        if counts["events"] > 0 or counts["entities"] > 0:
            return {"ok": False, "reason": "has_data", **counts}
        cur = self._conn.cursor()
        try:
            # Purge auto-seeded group scaffolding, then the registry row.
            cur.execute("DELETE FROM groups WHERE domain = ?", (name,))
            cur.execute("DELETE FROM domain_registry WHERE name = ?", (name,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {"ok": True, **counts}

    def rename_domain(self, old: str, new: str) -> dict[str, Any]:
        new = (new or "").strip()
        if not new:
            return {"ok": False, "reason": "empty_name"}
        if new == old:
            return {"ok": False, "reason": "same_name"}
        if new in self.list_managed_domains():
            return {"ok": False, "reason": "exists"}
        cur = self._conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = [row[0] for row in cur.fetchall()]
        updated: list[str] = []
        try:
            for table in tables:
                cur.execute(f"PRAGMA table_info({table})")
                cols = {row[1] for row in cur.fetchall()}
                touched = False
                if "domain" in cols:
                    cur.execute(
                        f"UPDATE {table} SET domain = ? WHERE domain = ?",
                        (new, old),
                    )
                    touched = touched or cur.rowcount > 0
                if "source_domain" in cols:  # k2g_domain_share
                    cur.execute(
                        f"UPDATE {table} SET source_domain = ? "
                        f"WHERE source_domain = ?",
                        (new, old),
                    )
                    touched = touched or cur.rowcount > 0
                if touched:
                    updated.append(table)
            # Rename the registry row too (no-op if old was a data-only domain).
            cur.execute(
                "UPDATE OR IGNORE domain_registry SET name = ? WHERE name = ?",
                (new, old),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {"ok": True, "tables": sorted(updated)}

    def check_entities_exist(self, names: list[str]) -> list[dict[str, Any]]:
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT name, id, user_tag FROM entities WHERE name IN ({placeholders})",
            tuple(names),
        )
        return [{"name": r[0], "id": r[1], "user_tag": r[2]} for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # (d) Jaccard / chain queries
    # ------------------------------------------------------------------

    def link_event_jaccard_connected(
        self,
        event_id: str,
        other_id: str,
        *,
        entity_jaccard: float,
        group_jaccard: float,
        entity_intersection: int,
        group_intersection: int,
    ) -> None:
        """UPSERT a row into event_jaccard_connected with a_id < b_id ordering."""
        a, b = (event_id, other_id) if event_id < other_id else (other_id, event_id)
        self._conn.execute(
            """
            INSERT INTO event_jaccard_connected
                (a_id, b_id, entity_jaccard, group_jaccard,
                 entity_intersection, group_intersection)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(a_id, b_id) DO UPDATE SET
                entity_jaccard      = excluded.entity_jaccard,
                group_jaccard       = excluded.group_jaccard,
                entity_intersection = excluded.entity_intersection,
                group_intersection  = excluded.group_intersection
            """,
            (a, b, entity_jaccard, group_jaccard,
             entity_intersection, group_intersection),
        )
        self._conn.commit()

    def compute_jaccard_connected(
        self,
        domain: str,
        *,
        scope: str = "group",
        theta_e: float = 0.4,
        theta_g: float = 0.3,
        min_group_intersection: int = 2,
        stop_groups: set[str] | None = None,
        max_entity_degree: int | None = 200,
        max_group_degree: int | None = 200,
        exclude_stopwords: bool = True,
    ) -> int:
        """Graph-based shared-entity/group prefilter — O(M).

        Matches the semantics of the PostgresGraphStore implementation. A SQL
        self-join extracts only candidate pairs (at least 1 shared entity or
        group), then Jaccard is computed in Python. This is much faster than
        the legacy O(N²) approach. ``scope`` is currently unused (result is
        entity_jaccard OR group_jaccard).

        Degree cap: entities (and groups) appearing in more than
        ``max_entity_degree`` events are hub/stopword nodes — they produce
        O(degree²) candidate explosion in the self-join and are poor
        discriminators. They are excluded from both candidate extraction and
        Jaccard computation. Same semantics as the high_deg_ent/high_deg_grp
        CTEs in the Postgres implementation. Pass None to disable.
        Because the batch scans the whole domain anyway, hub detection uses a
        domain-wide GROUP BY (contrast with the event-scoped detection in
        ``compute_jaccard_for_event``).

        ``exclude_stopwords`` (default True): entities tagged
        user_tag='stopword' are also added to hub_ents, removing
        user-curated noise in the mid-degree range (below the degree cap)
        that would otherwise not be excluded automatically.
        Same default-on semantics as ``leiden_community.build_entity_graph``.
        """
        del scope
        stop_set: set[str] = set(stop_groups) if stop_groups else set()

        cur = self._conn.cursor()

        # Degree cap — detect hub entities/groups (>cap events) domain-wide.
        hub_ents: set[str] = set()
        if max_entity_degree is not None:
            cur.execute(
                "SELECT pi.entity_id FROM participated_in pi "
                "JOIN events e ON e.id = pi.event_id "
                "WHERE e.domain = ? AND e.deprecated = 0 "
                "GROUP BY pi.entity_id HAVING COUNT(*) > ?",
                (domain, max_entity_degree),
            )
            hub_ents = {r[0] for r in cur.fetchall()}
        # Also include user_tag='stopword' entities (user-curated, OR with degree cap).
        if exclude_stopwords:
            cur.execute(
                "SELECT id FROM entities WHERE user_tag = 'stopword' "
                "AND domain = ?",
                (domain,),
            )
            hub_ents.update(r[0] for r in cur.fetchall())
        hub_grps: set[str] = set()
        if max_group_degree is not None:
            cur.execute(
                "SELECT em.group_id FROM event_member_of em "
                "JOIN events e ON e.id = em.event_id "
                "WHERE e.domain = ? AND e.deprecated = 0 "
                "GROUP BY em.group_id HAVING COUNT(*) > ?",
                (domain, max_group_degree),
            )
            hub_grps = {r[0] for r in cur.fetchall()}

        entity_sets: dict[str, set[str]] = {}
        cur.execute(
            "SELECT pi.event_id, pi.entity_id "
            "FROM participated_in pi "
            "JOIN events e ON e.id = pi.event_id "
            "WHERE e.domain = ? AND e.deprecated = 0",
            (domain,),
        )
        for ev_id, ent_id in cur.fetchall():
            if ent_id in hub_ents:
                continue
            entity_sets.setdefault(ev_id, set()).add(ent_id)

        group_sets: dict[str, set[str]] = {}
        cur.execute(
            "SELECT em.event_id, em.group_id "
            "FROM event_member_of em "
            "JOIN events e ON e.id = em.event_id "
            "WHERE e.domain = ? AND e.deprecated = 0",
            (domain,),
        )
        for ev_id, g_id in cur.fetchall():
            if g_id in stop_set or g_id in hub_grps:
                continue
            group_sets.setdefault(ev_id, set()).add(g_id)

        # SQL self-join to extract candidate pairs — hub entities/groups excluded
        # to prevent O(degree²) candidate explosion.
        candidate_pairs: set[tuple[str, str]] = set()

        ent_clause = ""
        ent_params: list[str] = [domain, domain]
        if hub_ents:
            ph = ",".join("?" for _ in hub_ents)
            ent_clause = f" AND pi1.entity_id NOT IN ({ph})"
            ent_params.extend(hub_ents)
        cur.execute(
            f"""
            SELECT DISTINCT pi1.event_id AS a_id, pi2.event_id AS b_id
              FROM participated_in pi1
              JOIN participated_in pi2
                ON pi1.entity_id = pi2.entity_id
               AND pi1.event_id < pi2.event_id
              JOIN events ea ON ea.id = pi1.event_id
              JOIN events eb ON eb.id = pi2.event_id
             WHERE ea.domain = ? AND ea.deprecated = 0
               AND eb.domain = ? AND eb.deprecated = 0{ent_clause}
            """,
            ent_params,
        )
        for a, b in cur.fetchall():
            candidate_pairs.add((a, b))

        grp_exclude = stop_set | hub_grps
        grp_clause = ""
        grp_params: list[str] = [domain, domain]
        if grp_exclude:
            ph = ",".join("?" for _ in grp_exclude)
            grp_clause = f" AND em1.group_id NOT IN ({ph})"
            grp_params.extend(grp_exclude)
        cur.execute(
            f"""
            SELECT DISTINCT em1.event_id AS a_id, em2.event_id AS b_id
              FROM event_member_of em1
              JOIN event_member_of em2
                ON em1.group_id = em2.group_id
               AND em1.event_id < em2.event_id
              JOIN events ea ON ea.id = em1.event_id
              JOIN events eb ON eb.id = em2.event_id
             WHERE ea.domain = ? AND ea.deprecated = 0
               AND eb.domain = ? AND eb.deprecated = 0{grp_clause}
            """,
            grp_params,
        )
        for a, b in cur.fetchall():
            candidate_pairs.add((a, b))

        n_total = len(entity_sets.keys() | group_sets.keys())
        n_pairs_full = n_total * (n_total - 1) // 2 if n_total > 1 else 0
        logger.info(
            "Jaccard candidates: domain=%s events=%d full_pairs=%d "
            "candidate_pairs=%d (%.4f%% of full)",
            domain, n_total, n_pairs_full, len(candidate_pairs),
            (100.0 * len(candidate_pairs) / n_pairs_full) if n_pairs_full else 0.0,
        )

        try:
            from tqdm import tqdm  # type: ignore[import-not-found]
            iterator: object = tqdm(
                candidate_pairs,
                desc=f"Jaccard [{domain}]",
                unit="pair",
                leave=True,
                dynamic_ncols=True,
            )
        except ImportError:
            iterator = candidate_pairs

        # Store-all mode: the write gate on theta_e has been removed. Any
        # entity_jaccard > 0 (actual overlap) is always stored; thresholding
        # is deferred to read-time (e.g. Leiden resolution), like a parameter.
        # The explosion guard is the hub degree cap. Group-only edges are
        # preserved by the group trigger. theta_e is kept in the signature for
        # API stability even though it is no longer used for writes.
        created = 0
        rows: list[tuple[str, str, float, float, int, int]] = []
        for ev_a, ev_b in iterator:  # type: ignore[assignment]
            ents_a = entity_sets.get(ev_a, set())
            ents_b = entity_sets.get(ev_b, set())
            grps_a = group_sets.get(ev_a, set())
            grps_b = group_sets.get(ev_b, set())

            ent_isect = len(ents_a & ents_b)
            ent_union = len(ents_a | ents_b)
            entity_jaccard = ent_isect / ent_union if ent_union else 0.0

            grp_isect = len(grps_a & grps_b)
            grp_union = len(grps_a | grps_b)
            group_jaccard = grp_isect / grp_union if grp_union else 0.0

            trigger_e = entity_jaccard > 0
            trigger_g = (
                group_jaccard >= theta_g
                and grp_isect >= min_group_intersection
            )
            if not (trigger_e or trigger_g):
                continue
            a, b = (ev_a, ev_b) if ev_a < ev_b else (ev_b, ev_a)
            rows.append((a, b, entity_jaccard, group_jaccard, ent_isect, grp_isect))
            created += 1

        # Batch insert in a single transaction via executemany (~1M rows).
        if rows:
            self._conn.executemany(
                """
                INSERT INTO event_jaccard_connected
                    (a_id, b_id, entity_jaccard, group_jaccard,
                     entity_intersection, group_intersection)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(a_id, b_id) DO UPDATE SET
                    entity_jaccard      = excluded.entity_jaccard,
                    group_jaccard       = excluded.group_jaccard,
                    entity_intersection = excluded.entity_intersection,
                    group_intersection  = excluded.group_intersection
                """,
                rows,
            )
            self._conn.commit()

        logger.info(
            "Jaccard Connected edges written: domain=%s store-all(write theta_e gate removed) "
            "theta_g=%.2f stop=%d hub_ent=%d hub_grp=%d candidates=%d created=%d",
            domain, theta_g, len(stop_set),
            len(hub_ents), len(hub_grps), len(candidate_pairs), created,
        )
        return created

    def compute_jaccard_for_event(
        self,
        domain: str,
        event_id: str,
        *,
        theta_e: float = 0.4,
        theta_g: float = 0.3,
        min_group_intersection: int = 2,
        max_entity_degree: int | None = 200,
        max_group_degree: int | None = 200,
        exclude_stopwords: bool = True,
    ) -> int:
        """Incremental Jaccard computation for a single event.

        Narrows the ``compute_jaccard_connected`` algorithm to event_id only —
        candidates are events sharing at least 1 entity or group with event_id.
        Idempotent: existing rows touching event_id are deleted before
        recomputing.

        Degree cap: hub entities appearing in more than ``max_entity_degree``
        events are poor discriminators and cause candidate explosion. They are
        excluded from Jaccard, bounding per-event cost independently of domain
        size (critical for deployment throughput). Hub detection uses a
        GROUP BY scoped to this event's entities, not a full domain scan.
        Pass None to disable.

        ``exclude_stopwords`` (default True): user_tag='stopword' entities are
        also added to hub_ents, removing user-curated noise in the mid-degree
        range. The check is scoped to this event's entities via an IN clause,
        avoiding a domain scan.
        """
        cur = self._conn.cursor()

        # Raw entity/group sets for this event
        cur.execute(
            "SELECT entity_id FROM participated_in WHERE event_id = ?",
            (event_id,),
        )
        raw_ents = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT group_id FROM event_member_of WHERE event_id = ?",
            (event_id,),
        )
        raw_grps = {r[0] for r in cur.fetchall()}

        # Degree cap — detect hubs (>cap events) among this event's entities/groups
        # only. The IN-clause GROUP BY hits the PK index — no full domain scan.
        hub_ents: set[str] = set()
        if max_entity_degree is not None and raw_ents:
            ph = ",".join("?" for _ in raw_ents)
            cur.execute(
                f"SELECT entity_id FROM participated_in "
                f"WHERE entity_id IN ({ph}) "
                f"GROUP BY entity_id HAVING COUNT(*) > ?",
                (*raw_ents, max_entity_degree),
            )
            hub_ents = {r[0] for r in cur.fetchall()}
        # user_tag='stopword' union — IN-clause scoped to this event (no domain scan).
        if exclude_stopwords and raw_ents:
            ph = ",".join("?" for _ in raw_ents)
            cur.execute(
                f"SELECT id FROM entities "
                f"WHERE id IN ({ph}) AND user_tag = 'stopword'",
                tuple(raw_ents),
            )
            hub_ents.update(r[0] for r in cur.fetchall())
        hub_grps: set[str] = set()
        if max_group_degree is not None and raw_grps:
            ph = ",".join("?" for _ in raw_grps)
            cur.execute(
                f"SELECT group_id FROM event_member_of "
                f"WHERE group_id IN ({ph}) "
                f"GROUP BY group_id HAVING COUNT(*) > ?",
                (*raw_grps, max_group_degree),
            )
            hub_grps = {r[0] for r in cur.fetchall()}
        e_ents = raw_ents - hub_ents
        e_grps = raw_grps - hub_grps

        # Idempotent: remove existing Jaccard rows touching this event
        cur.execute(
            "DELETE FROM event_jaccard_connected WHERE a_id = ? OR b_id = ?",
            (event_id, event_id),
        )
        if not e_ents and not e_grps:
            self._conn.commit()
            return 0

        # Candidates: events sharing at least 1 non-hub entity/group with event_id
        cands: set[str] = set()
        if e_ents:
            ph = ",".join("?" for _ in e_ents)
            cur.execute(
                f"SELECT DISTINCT pi.event_id FROM participated_in pi "
                f"JOIN events e ON e.id = pi.event_id "
                f"WHERE pi.entity_id IN ({ph}) AND pi.event_id != ? "
                f"  AND e.domain = ? AND e.deprecated = 0",
                (*e_ents, event_id, domain),
            )
            cands.update(r[0] for r in cur.fetchall())
        if e_grps:
            ph = ",".join("?" for _ in e_grps)
            cur.execute(
                f"SELECT DISTINCT em.event_id FROM event_member_of em "
                f"JOIN events e ON e.id = em.event_id "
                f"WHERE em.group_id IN ({ph}) AND em.event_id != ? "
                f"  AND e.domain = ? AND e.deprecated = 0",
                (*e_grps, event_id, domain),
            )
            cands.update(r[0] for r in cur.fetchall())
        if not cands:
            self._conn.commit()
            return 0

        # Bulk-fetch entity/group sets for all candidates (excluding hubs)
        cph = ",".join("?" for _ in cands)
        f_ents: dict[str, set[str]] = {}
        cur.execute(
            f"SELECT event_id, entity_id FROM participated_in "
            f"WHERE event_id IN ({cph})",
            tuple(cands),
        )
        for ev, ent in cur.fetchall():
            if ent not in hub_ents:
                f_ents.setdefault(ev, set()).add(ent)
        f_grps: dict[str, set[str]] = {}
        cur.execute(
            f"SELECT event_id, group_id FROM event_member_of "
            f"WHERE event_id IN ({cph})",
            tuple(cands),
        )
        for ev, g in cur.fetchall():
            if g not in hub_grps:
                f_grps.setdefault(ev, set()).add(g)

        created = 0
        for f in cands:
            fe = f_ents.get(f, set())
            fg = f_grps.get(f, set())
            ent_isect = len(e_ents & fe)
            ent_union = len(e_ents | fe)
            entity_jaccard = ent_isect / ent_union if ent_union else 0.0
            grp_isect = len(e_grps & fg)
            grp_union = len(e_grps | fg)
            group_jaccard = grp_isect / grp_union if grp_union else 0.0
            # Store-all: entity_jaccard > 0 always written; theta_e is read-time.
            trigger_e = entity_jaccard > 0
            trigger_g = (
                group_jaccard >= theta_g
                and grp_isect >= min_group_intersection
            )
            if not (trigger_e or trigger_g):
                continue
            self.link_event_jaccard_connected(
                event_id, f,
                entity_jaccard=entity_jaccard,
                group_jaccard=group_jaccard,
                entity_intersection=ent_isect,
                group_intersection=grp_isect,
            )
            created += 1
        self._conn.commit()
        return created

    def get_jaccard_neighbors(
        self, event_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return Jaccard neighbors of an event from event_jaccard_connected.

        Ordered by score = max(entity_jaccard, group_jaccard) descending.
        """
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT CASE WHEN a_id = ? THEN b_id ELSE a_id END AS nb,
                   MAX(COALESCE(entity_jaccard, 0),
                       COALESCE(group_jaccard, 0)) AS score
              FROM event_jaccard_connected
             WHERE a_id = ? OR b_id = ?
             ORDER BY score DESC
             LIMIT ?
            """,
            (event_id, event_id, event_id, int(limit)),
        )
        return [
            {"event_id": r[0], "score": float(r[1] or 0.0)}
            for r in cur.fetchall()
        ]

    def mark_deprecated(self, node_type: str, node_id: str) -> bool:
        """Set deprecated=1 on one row in events, entities, groups, or context_groups."""
        table = {
            "event": "events", "events": "events",
            "entity": "entities", "entities": "entities",
            "group": "groups", "groups": "groups",
            "context_group": "context_groups", "context_groups": "context_groups",
        }.get(node_type)
        if table is None:
            raise ValueError(f"mark_deprecated: unsupported node_type={node_type!r}")
        cur = self._conn.cursor()
        cur.execute(
            f"UPDATE {table} SET deprecated = 1 WHERE id = ? AND deprecated = 0",
            (node_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_events_deprecated_by_group(self, group_id: str) -> int:
        """Set deprecated=1 on all events belonging to the specified group."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE events
               SET deprecated = 1
             WHERE deprecated = 0
               AND id IN (SELECT event_id FROM event_member_of WHERE group_id = ?)
            """,
            (group_id,),
        )
        self._conn.commit()
        return cur.rowcount

    def find_group_by_path(self, relative_path: str, domain: str) -> str | None:
        """Find a group whose name ends with relative_path within the given domain."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id FROM groups "
            "WHERE domain = ? AND deprecated = 0 AND name LIKE ? "
            "LIMIT 1",
            (domain, f"%{relative_path}"),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def list_event_ids(self, domain: str) -> list[str]:
        """List non-deprecated event ids for a domain, ordered by order_index ascending."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id FROM events "
            "WHERE domain = ? AND deprecated = 0 "
            "ORDER BY order_index",
            (domain,),
        )
        return [row[0] for row in cur.fetchall()]

    def iter_sequential_pairs(self, domain: str) -> list[tuple[str, str]]:
        """Return (prev_id, next_id) tuples for all EVENT_SEQUENTIAL_NEXT edges in the domain."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT sn.prev_id, sn.next_id
              FROM event_sequential_next sn
              JOIN events e ON e.id = sn.prev_id
             WHERE e.domain = ? AND e.deprecated = 0
            """,
            (domain,),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]

    def iter_jaccard_pairs(self, domain: str) -> list[tuple[str, str, float]]:
        """Return (a_id, b_id, max(entity_jaccard, group_jaccard)) for all Jaccard edges in the domain."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT j.a_id, j.b_id,
                   CASE WHEN COALESCE(j.entity_jaccard, 0) > COALESCE(j.group_jaccard, 0)
                        THEN COALESCE(j.entity_jaccard, 0)
                        ELSE COALESCE(j.group_jaccard, 0)
                   END AS score
              FROM event_jaccard_connected j
              JOIN events ea ON ea.id = j.a_id
              JOIN events eb ON eb.id = j.b_id
             WHERE ea.domain = ? AND eb.domain = ?
               AND ea.deprecated = 0 AND eb.deprecated = 0
            """,
            (domain, domain),
        )
        return [(row[0], row[1], float(row[2] or 0.0)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # (e) ContextGroup training
    # ------------------------------------------------------------------

    _CG_COLUMNS: tuple[str, ...] = (
        "id", "name", "stage", "cluster_source", "training_method",
        "confidence", "member_count_own", "member_count_total",
        "depth", "version", "narrative_summary", "order_index", "domain",
        "parent_id", "plan_stage", "plan_id", "expected_entities",
        "abandon_reason", "template_id", "transition_pattern",
        "source_cg_ids", "instance_count",
    )

    def create_context_group(self, cg: dict[str, Any]) -> str:
        cols = [c for c in self._CG_COLUMNS if c in cg]
        placeholders = ",".join("?" for _ in cols)
        values = tuple(cg[c] for c in cols)
        self._conn.execute(
            f"INSERT INTO context_groups ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()
        return str(cg["id"])

    def get_context_group(self, cg_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM context_groups WHERE id = ?", (cg_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def delete_context_group(self, cg_id: str) -> None:
        # event_belongs_to_context has no ON DELETE CASCADE (target_id is not
        # a FK). Clean up manually before deleting the CG row.
        self._conn.execute(
            "DELETE FROM event_belongs_to_context WHERE target_id = ? AND target_kind = 'CG '",
            (cg_id,),
        )
        self._conn.execute(
            "DELETE FROM realized_as WHERE from_id = ? AND from_kind = 'CG '",
            (cg_id,),
        )
        self._conn.execute(
            "DELETE FROM plan_from WHERE target_id = ? AND target_kind = 'CG '",
            (cg_id,),
        )
        self._conn.execute(
            "DELETE FROM plan_next WHERE (from_id = ? AND from_kind = 'CG ') "
            "OR (to_id = ? AND to_kind = 'CG ')",
            (cg_id, cg_id),
        )
        self._conn.execute("DELETE FROM context_groups WHERE id = ?", (cg_id,))
        self._conn.commit()

    def update_cg_stage(self, cg_id: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE context_groups SET stage = ? WHERE id = ?", (stage, cg_id),
        )
        self._conn.commit()

    def update_cg_name(self, cg_id: str, name: str) -> None:
        self._conn.execute(
            "UPDATE context_groups SET name = ? WHERE id = ?", (name, cg_id),
        )
        self._conn.commit()

    def update_cg_member_counts(self, cg_id: str, own: int, total: int) -> None:
        self._conn.execute(
            "UPDATE context_groups SET member_count_own = ?, member_count_total = ? WHERE id = ?",
            (int(own), int(total), cg_id),
        )
        self._conn.commit()

    def update_cg_narrative(self, cg_id: str, summary: str) -> None:
        self._conn.execute(
            "UPDATE context_groups SET narrative_summary = ? WHERE id = ?",
            (summary, cg_id),
        )
        self._conn.commit()

    def update_cg_order(self, cg_id: str, order_index: int) -> None:
        self._conn.execute(
            "UPDATE context_groups SET order_index = ? WHERE id = ?",
            (int(order_index), cg_id),
        )
        self._conn.commit()

    def link_event_belongs_to_context(
        self, event_id: str, cg_id: str, kind: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO event_belongs_to_context "
            "(event_id, target_id, target_kind, kind) VALUES (?, ?, 'CG ', ?) "
            "ON CONFLICT(event_id, target_id, target_kind) DO UPDATE SET kind = excluded.kind",
            (event_id, cg_id, kind),
        )
        self._conn.commit()

    def link_cg_child_of(
        self, child_cg_id: str, parent_cg_id: str, depth: int = 1,
    ) -> None:
        self._conn.execute(
            "UPDATE context_groups SET parent_id = ?, depth = ? WHERE id = ?",
            (parent_cg_id, int(depth), child_cg_id),
        )
        self._conn.commit()

    def link_cg_realized_as(self, cg_id: str, event_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO realized_as (from_id, from_kind, event_id) "
            "VALUES (?, 'CG ', ?)",
            (cg_id, event_id),
        )
        self._conn.commit()

    def link_plan_from(self, event_id: str, cg_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO plan_from (event_id, target_id, target_kind) "
            "VALUES (?, ?, 'CG ')",
            (event_id, cg_id),
        )
        self._conn.commit()

    def link_plan_next(
        self, from_cg_id: str, to_cg_id: str, order: int = 0,
    ) -> None:
        self._conn.execute(
            "INSERT INTO plan_next (from_id, from_kind, to_id, to_kind, order_idx) "
            "VALUES (?, 'CG ', ?, 'CG ', ?) "
            "ON CONFLICT(from_id, from_kind, to_id, to_kind) DO UPDATE SET order_idx = excluded.order_idx",
            (from_cg_id, to_cg_id, int(order)),
        )
        self._conn.commit()

    def get_cg_members(self, cg_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT event_id FROM event_belongs_to_context "
            "WHERE target_id = ? AND target_kind = 'CG ' LIMIT 1000",
            (cg_id,),
        )
        # Legacy Neo4j returned {id, labels}. The SQL side stores only Events,
        # so labels is always ["Event"].
        return [{"id": row[0], "labels": ["Event"]} for row in cur.fetchall()]

    def get_cg_events(self, cg_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT e.* FROM events e "
            "JOIN event_belongs_to_context m ON m.event_id = e.id "
            "WHERE m.target_id = ? AND m.target_kind = 'CG ' "
            "ORDER BY e.order_index ASC",
            (cg_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_contexts_of_event(self, event_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT cg.* FROM context_groups cg "
            "JOIN event_belongs_to_context m ON m.target_id = cg.id "
            "WHERE m.event_id = ? AND m.target_kind = 'CG '",
            (event_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_cg_children(self, cg_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM context_groups WHERE parent_id = ? "
            "ORDER BY order_index ASC",
            (cg_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_cg_hierarchy(self, cg_id: str) -> dict[str, Any]:
        children = self.get_cg_children(cg_id)
        # Ancestors: self → parent chain, resolved via SQLite WITH RECURSIVE.
        cur = self._conn.cursor()
        cur.execute(
            """
            WITH RECURSIVE chain(id, depth) AS (
                SELECT id, 0 FROM context_groups WHERE id = ?
                UNION ALL
                SELECT cg.parent_id, chain.depth + 1
                FROM context_groups cg
                JOIN chain ON cg.id = chain.id
                WHERE cg.parent_id IS NOT NULL AND chain.depth < 10
            )
            SELECT cg.* FROM context_groups cg
            JOIN chain ON cg.id = chain.id
            ORDER BY chain.depth ASC
            """,
            (cg_id,),
        )
        cols = [d[0] for d in cur.description]
        ancestors = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"children": children, "ancestors": ancestors}

    def list_cg_by_stage(self, stage: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM context_groups WHERE stage = ? "
            "ORDER BY member_count_total DESC",
            (stage,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def count_cg_by_stage(self) -> dict[str, int]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT stage, COUNT(*) FROM context_groups GROUP BY stage"
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}

    def list_control_nodes(
        self, domain: str, include_seed: bool = False,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        stage_filter = "" if include_seed else " AND stage != 'seed'"
        cur.execute(
            f"SELECT * FROM context_groups "
            f"WHERE training_method = 'structural_summary' AND domain = ?{stage_filter} "
            f"ORDER BY order_index ASC, created_at ASC",
            (domain,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_cg_plan_attrs(
        self,
        cg_id: str,
        plan_stage: str,
        plan_id: str,
        expected_entities_json: str,
        abandon_reason: str = "",
    ) -> None:
        self._conn.execute(
            "UPDATE context_groups SET plan_stage = ?, plan_id = ?, "
            "expected_entities = ?, abandon_reason = ? WHERE id = ?",
            (plan_stage, plan_id, expected_entities_json, abandon_reason, cg_id),
        )
        self._conn.commit()

    def update_cg_plan_stage(
        self,
        cg_id: str,
        plan_stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        if abandon_reason is not None:
            self._conn.execute(
                "UPDATE context_groups SET plan_stage = ?, abandon_reason = ? WHERE id = ?",
                (plan_stage, abandon_reason, cg_id),
            )
        else:
            self._conn.execute(
                "UPDATE context_groups SET plan_stage = ? WHERE id = ?",
                (plan_stage, cg_id),
            )
        if realized_event_id:
            self._conn.execute(
                "INSERT OR IGNORE INTO realized_as (from_id, from_kind, event_id) "
                "VALUES (?, 'CG ', ?)",
                (cg_id, realized_event_id),
            )
        self._conn.commit()

    def list_plan_seed_cgs(self, domain: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM context_groups "
            "WHERE cluster_source = 'plan' AND order_index = 0 AND domain = ? "
            "ORDER BY created_at ASC",
            (domain,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_plan_chain(self, start_cg_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            WITH RECURSIVE chain(id, depth) AS (
                SELECT ?, 0
                UNION ALL
                SELECT pn.to_id, chain.depth + 1
                FROM plan_next pn
                JOIN chain ON pn.from_id = chain.id
                WHERE pn.from_kind = 'CG ' AND pn.to_kind = 'CG '
                  AND chain.depth < 100
            )
            SELECT cg.* FROM context_groups cg
            JOIN chain ON cg.id = chain.id
            ORDER BY chain.depth ASC
            """,
            (start_cg_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def list_abandoned_plans(
        self, domain: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM context_groups "
            "WHERE cluster_source = 'plan' AND plan_stage = 'abandoned' AND domain = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (domain, int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_cg_template_attrs(
        self,
        cg_id: str,
        template_id: str,
        transition_pattern_json: str,
        source_cg_ids_json: str,
        instance_count: int,
    ) -> None:
        self._conn.execute(
            "UPDATE context_groups SET template_id = ?, transition_pattern = ?, "
            "source_cg_ids = ?, instance_count = ? WHERE id = ?",
            (
                template_id, transition_pattern_json, source_cg_ids_json,
                int(instance_count), cg_id,
            ),
        )
        self._conn.commit()

    def list_cg_templates(self, domain: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM context_groups "
            "WHERE cluster_source = 'template' AND domain = ? "
            "ORDER BY instance_count DESC",
            (domain,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # (f) Tier 2b — ETG / PlanNode / PlanDirectionNode
    # ------------------------------------------------------------------

    _ETG_COLUMNS: tuple[str, ...] = (
        "id", "name", "domain", "transition_pattern", "structure_description",
        "entity_list", "confidence", "instance_count",
    )

    _PLAN_NODE_COLUMNS: tuple[str, ...] = (
        "id", "plan_id", "etg_id", "domain", "name", "stage",
        "objective_summary", "structure_description", "entity_ids",
        "entity_summary", "order_index", "realized_event_id",
        "parent_id",  # v2: chain → tree structure
        "abandon_reason",
    )

    _DIR_NODE_COLUMNS: tuple[str, ...] = (
        "id", "plan_id", "domain", "goal_text", "stage", "entity_ids",
        "entity_summary", "order_index", "realized_event_id", "abandon_reason",
    )

    def create_etg(self, etg: dict[str, Any]) -> str:
        cols = [c for c in self._ETG_COLUMNS if c in etg]
        placeholders = ",".join("?" for _ in cols)
        values = tuple(etg[c] for c in cols)
        self._conn.execute(
            f"INSERT INTO event_template_groups ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()
        return str(etg["id"])

    def get_etg(self, etg_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM event_template_groups WHERE id = ?", (etg_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def list_etgs(self, domain: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM event_template_groups WHERE domain = ? "
            "ORDER BY confidence DESC, created_at DESC",
            (domain,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def link_event_belongs_to_etg(
        self, event_id: str, etg_id: str, order: int = 0,
    ) -> None:
        self._conn.execute(
            "INSERT INTO event_belongs_to_context "
            "(event_id, target_id, target_kind, order_index) VALUES (?, ?, 'ETG', ?) "
            "ON CONFLICT(event_id, target_id, target_kind) DO UPDATE SET order_index = excluded.order_index",
            (event_id, etg_id, int(order)),
        )
        self._conn.commit()

    def get_etg_events(self, etg_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT e.* FROM events e "
            "JOIN event_belongs_to_context m ON m.event_id = e.id "
            "WHERE m.target_id = ? AND m.target_kind = 'ETG' "
            "ORDER BY m.order_index ASC, e.order_index ASC",
            (etg_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def create_plan_node(self, pn: dict[str, Any]) -> str:
        cols = [c for c in self._PLAN_NODE_COLUMNS if c in pn]
        placeholders = ",".join("?" for _ in cols)
        values = tuple(pn[c] for c in cols)
        self._conn.execute(
            f"INSERT INTO plan_nodes ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()
        return str(pn["id"])

    def get_plan_node(self, plan_node_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM plan_nodes WHERE id = ?", (plan_node_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_plan_chain_v2(self, plan_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM plan_nodes WHERE plan_id = ? ORDER BY order_index ASC",
            (plan_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def update_plan_node_stage(
        self,
        plan_node_id: str,
        stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        sets = ["stage = ?"]
        params: list[Any] = [stage]
        if realized_event_id is not None:
            sets.append("realized_event_id = ?")
            params.append(realized_event_id)
        if abandon_reason is not None:
            sets.append("abandon_reason = ?")
            params.append(abandon_reason)
        params.append(plan_node_id)
        self._conn.execute(
            f"UPDATE plan_nodes SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        if realized_event_id:
            self._conn.execute(
                "INSERT OR IGNORE INTO realized_as (from_id, from_kind, event_id) "
                "VALUES (?, 'PLN', ?)",
                (plan_node_id, realized_event_id),
            )
        self._conn.commit()

    def link_plan_from_etg(self, event_id: str, plan_node_id: str) -> None:
        self._conn.execute(
            "INSERT INTO plan_from (event_id, target_id, target_kind, source_type) "
            "VALUES (?, ?, 'PLN', 'etg_event') "
            "ON CONFLICT(event_id, target_id, target_kind) DO UPDATE SET source_type = excluded.source_type",
            (event_id, plan_node_id),
        )
        self._conn.commit()

    def link_plan_next_plan(
        self, from_plan_id: str, to_plan_id: str, order: int = 0,
    ) -> None:
        self._conn.execute(
            "INSERT INTO plan_next (from_id, from_kind, to_id, to_kind, order_idx) "
            "VALUES (?, 'PLN', ?, 'PLN', ?) "
            "ON CONFLICT(from_id, from_kind, to_id, to_kind) DO UPDATE SET order_idx = excluded.order_idx",
            (from_plan_id, to_plan_id, int(order)),
        )
        self._conn.commit()

    def link_realized_as_plan(self, plan_node_id: str, event_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO realized_as (from_id, from_kind, event_id) "
            "VALUES (?, 'PLN', ?)",
            (plan_node_id, event_id),
        )
        self._conn.commit()

    def list_active_plans(
        self, domain: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT plan_id, MIN(created_at) AS first_created, COUNT(*) AS node_count "
            "FROM plan_nodes WHERE domain = ? AND stage IN ('draft','planned','active') "
            "GROUP BY plan_id ORDER BY first_created DESC LIMIT ?",
            (domain, int(limit)),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def create_direction_node(self, dn: dict[str, Any]) -> str:
        cols = [c for c in self._DIR_NODE_COLUMNS if c in dn]
        placeholders = ",".join("?" for _ in cols)
        values = tuple(dn[c] for c in cols)
        self._conn.execute(
            f"INSERT INTO plan_direction_nodes ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()
        return str(dn["id"])

    def get_direction_node(self, dir_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM plan_direction_nodes WHERE id = ?", (dir_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_direction_chain(self, plan_id: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM plan_direction_nodes WHERE plan_id = ? ORDER BY order_index ASC",
            (plan_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def update_direction_stage(
        self,
        dir_id: str,
        stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        sets = ["stage = ?"]
        params: list[Any] = [stage]
        if realized_event_id is not None:
            sets.append("realized_event_id = ?")
            params.append(realized_event_id)
        if abandon_reason is not None:
            sets.append("abandon_reason = ?")
            params.append(abandon_reason)
        params.append(dir_id)
        self._conn.execute(
            f"UPDATE plan_direction_nodes SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        if realized_event_id:
            self._conn.execute(
                "INSERT OR IGNORE INTO realized_as (from_id, from_kind, event_id) "
                "VALUES (?, 'DIR', ?)",
                (dir_id, realized_event_id),
            )
        self._conn.commit()

    def link_plan_next_direction(
        self, from_dir_id: str, to_dir_id: str, order: int = 0,
    ) -> None:
        self._conn.execute(
            "INSERT INTO plan_next (from_id, from_kind, to_id, to_kind, order_idx) "
            "VALUES (?, 'DIR', ?, 'DIR', ?) "
            "ON CONFLICT(from_id, from_kind, to_id, to_kind) DO UPDATE SET order_idx = excluded.order_idx",
            (from_dir_id, to_dir_id, int(order)),
        )
        self._conn.commit()

    def link_realized_as_direction(self, dir_id: str, event_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO realized_as (from_id, from_kind, event_id) "
            "VALUES (?, 'DIR', ?)",
            (dir_id, event_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # (g) Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            return True
        except sqlite3.Error as exc:
            logger.exception("db_connection_lost", extra={
                "backend": "sqlite", "operation": "ping",
                "err": str(exc)[:200],
            })
            return False

    def close(self) -> None:
        if hasattr(self, "_conn") and self._conn is not None:
            try:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]
                logger.info("SqliteGraphStore connection closed")
            except sqlite3.Error as exc:
                logger.warning("SqliteGraphStore close error: %s", exc)
