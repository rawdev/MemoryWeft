"""PostgresGraphStore — Postgres + pgvector backend GraphStore.

Structurally satisfies `k2g.stores.graph_store_protocol.GraphStoreProtocol`.
All Tier 1/2a/2b methods are fully implemented with psycopg2 + raw SQL.

JSONB column binding: ``_build_insert_with_jsonb`` automatically converts
dict/list values to JSON strings with ``%s::jsonb`` casting. Callers may
pass either a dict/list or an already-serialised JSON string.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection

from k2g.core.config import DomainConfig
from k2g.db_store.graph_backend import SequentialSource
from k2g.db_store.postgres.schema import (
    CREATE_AUDIT_INDEXES_SQL,
    CREATE_AUDIT_TABLES_SQL,
    CREATE_BUILD_AUDIT_INDEXES_SQL,
    CREATE_BUILD_AUDIT_TABLES_SQL,
    CREATE_COVENANT_META_INDEXES_SQL,
    CREATE_COVENANT_META_TABLES_SQL,
    CREATE_CROSS_DOMAIN_INDEXES_SQL,
    CREATE_CROSS_DOMAIN_TABLES_SQL,
    CREATE_DOMAIN_REGISTRY_SQL,
    CREATE_EXTENSIONS_SQL,
    CREATE_FAILURE_MANIFEST_INDEXES_SQL,
    CREATE_FAILURE_MANIFEST_TABLES_SQL,
    CREATE_INDEXES_SQL,
    CREATE_SHARE_INDEXES_SQL,
    CREATE_SHARE_TABLES_SQL,
    CREATE_TIER1_EDGE_TABLES_SQL,
    CREATE_TIER1_TABLES_SQL,
    CREATE_TIER2A_INDEXES_SQL,
    CREATE_TIER2A_TABLES_SQL,
    CREATE_TIER2B_INDEXES_SQL,
    CREATE_TIER2B_TABLES_SQL,
    CREATE_TRAIN_RUN_INDEXES_SQL,
    CREATE_TRAIN_RUN_TABLES_SQL,
    CREATE_TRAIN_STATE_INDEXES_SQL,
    CREATE_TRAIN_STATE_TABLES_SQL,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema bootstrap version guards (perf: skip the ~100-statement DDL replay on
# every connect once a DB is already at the current version).
#
# setup_schema* run ~100 idempotent ``CREATE/ALTER ... IF NOT EXISTS`` round
# trips. Over a remote pooler (Supabase) each is a network round trip, so a
# cold UI/MCP start pays seconds of redundant DDL even on a fully-bootstrapped
# DB. We record a per-component version in ``k2g_schema_meta`` and, when it
# already matches, skip straight past the DDL (one cheap SELECT instead).
#
# ⚠️ BUMP the relevant constant whenever you ADD or MODIFY any DDL inside the
# matching ``setup_*`` method — otherwise already-bootstrapped DBs will NEVER
# replay the new migration (they'll see the old version and skip). A fresh DB
# is unaffected (no row → runs the full DDL → records the version).
_TIER1_SCHEMA_VERSION = 1
_TIER2A_SCHEMA_VERSION = 1
_TIER2B_SCHEMA_VERSION = 1


_TIMESTAMP_PLACEHOLDER = {
    "", "null", "none", "undefined", "n/a", "na", "nat", "nan",
}


import re as _re

# Non-standard timestamp patterns commonly returned by LLMs, normalised to
# ISO 8601. Data-preserving: anything parseable is kept.
_TIMESTAMP_NORMALIZE_PATTERNS = [
    # 2026-04-17-142233 → 2026-04-17T14:22:33
    (_re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})(\d{2})$"),
     r"\1T\2:\3:\4"),
    # 20260417_142233 / 20260417-142233 → 2026-04-17T14:22:33
    (_re.compile(r"^(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})$"),
     r"\1-\2-\3T\4:\5:\6"),
    # 20260417T142233 / 20260417142233 → 2026-04-17T14:22:33
    (_re.compile(r"^(\d{4})(\d{2})(\d{2})T?(\d{2})(\d{2})(\d{2})$"),
     r"\1-\2-\3T\4:\5:\6"),
    # 2026-04-17_14:22:33 → 2026-04-17T14:22:33
    (_re.compile(r"^(\d{4}-\d{2}-\d{2})[_ ](\d{2}:\d{2}:\d{2})$"),
     r"\1T\2"),
    # 2026/04/17 14:22:33 / 2026/04/17T14:22:33 → 2026-04-17T14:22:33
    (_re.compile(r"^(\d{4})/(\d{2})/(\d{2})[ T](\d{2}:\d{2}:\d{2})$"),
     r"\1-\2-\3T\4"),
    # 2026/04/17 → 2026-04-17
    (_re.compile(r"^(\d{4})/(\d{2})/(\d{2})$"),
     r"\1-\2-\3"),
    # 2026.04.17 → 2026-04-17
    (_re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$"),
     r"\1-\2-\3"),
]


def _normalize_timestamp(timestamp: Any) -> str | None:
    """Normalise a timestamp string from an LLM to a PostgreSQL timestamptz.

    1. None / placeholder ('null', 'none', etc.) → None
    2. Non-standard patterns → attempt ISO 8601 conversion (dash-separated
       time components, slash or dot separators, etc.). Data-preserving.
    3. Passes ``datetime.fromisoformat`` validation → returned as-is.
    4. Anything else (partial timestamps like '2026', free text) → None

    Example LLM responses that normalise successfully:
    - '2026-04-17-142233' → '2026-04-17T14:22:33'
    - '20260417_142233'   → '2026-04-17T14:22:33'
    - '2026/04/17'        → '2026-04-17'
    """
    if timestamp is None:
        return None
    s = str(timestamp).strip()
    if not s or s.lower() in _TIMESTAMP_PLACEHOLDER:
        return None

    # Attempt conversion from non-standard format to ISO 8601.
    for pattern, replacement in _TIMESTAMP_NORMALIZE_PATTERNS:
        if pattern.match(s):
            s = pattern.sub(replacement, s)
            break

    # Normalise 'Z' suffix to '+00:00' for broad fromisoformat compatibility.
    test = s[:-1] + "+00:00" if s.endswith("Z") else s
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(test)
    except (ValueError, TypeError):
        # Partial timestamps, free-form text, or malformed input → NULL.
        return None
    # Python's fromisoformat accepts some non-standard separators (e.g.
    # '2026-04-13~14') that Postgres timestamptz rejects.  Re-serialise
    # via the parsed object to produce a strict ISO 8601 string while
    # preserving the original meaning.
    return parsed.isoformat()


# DDL definitions have been moved to ``k2g.db_store.postgres.schema`` (2026-04-26).
# Keep sqlite/schema.py and related docs in sync when modifying.


# ============================================================================
# PostgresGraphStore
# ============================================================================


class PostgresGraphStore:
    """Postgres + pgvector GraphStore (Phase 1b complete, 2026-04-26).

    Structurally satisfies GraphStoreProtocol.  All Tier 1/2a/2b methods
    are fully implemented with 1-to-1 parity with SqliteGraphStore
    (12 Tier-1 queries + 13 ContextGroup + 16 ETG/Plan/Direction methods).

    JSONB column binding: ``_build_insert_with_jsonb`` automatically
    converts dict/list values to JSON strings with ``%s::jsonb`` casting.
    Callers may pass either a dict/list or an already-serialised JSON string.
    """

    def __init__(
        self,
        dsn: str,
        *,
        embedding_dim: int = 1024,
        embedding_model: str | None = None,
    ) -> None:
        """Args:
            dsn: psycopg2 connection string, e.g.
                ``postgresql://user:pw@host/db``.
            embedding_dim: pgvector dimension matching
                ``settings.embedding_dim``.  Determines the vector
                column width for new databases (default 1024).
            embedding_model: Embedding model id; stamped into the DB fingerprint
                so a later open with an incompatible dim/model is blocked.
        """
        self._dsn = dsn
        self._embedding_dim = int(embedding_dim)
        self._embedding_model = embedding_model
        logger.info("PostgresGraphStore init: dim=%d", self._embedding_dim)
        self._conn: PgConnection = psycopg2.connect(
            dsn,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        self._conn.autocommit = False
        # In read-only MCP environments the schema setup can be skipped to
        # avoid ALTER lock contention against a Supabase pooler, which
        # caused first-call hangs (2026-05-12 incident).
        # The build-time entrypoint (k2g-build-memory) ignores this flag
        # and always runs the full setup.
        import os
        if os.environ.get("K2G_DB_SKIP_SCHEMA_SETUP", "").strip().lower() in ("1", "true", "yes"):
            logger.info(
                "PostgresGraphStore: schema setup skip (K2G_DB_SKIP_SCHEMA_SETUP=true)"
            )
        else:
            self.setup_schema()
            # Tier 2a/2b schemas are also ensured at startup so that
            # idempotent ALTERs for plan_nodes / context_groups /
            # event_template_groups are applied without a manual call
            # after schema changes.  All statements use
            # CREATE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
            self.setup_training_schema()
            self.setup_bp30_schema()
            # Block opening with an incompatible embedding dim/model — mixing
            # vector dimensions/models in one store silently corrupts search.
            self._check_embedding_fingerprint()

    def _check_embedding_fingerprint(self) -> None:
        """Stamp (first open) or verify (later opens) the embedding fingerprint.

        Raises ``EmbeddingFingerprintMismatch`` if the current embedding
        dim/model is incompatible with what the DB was built with.
        """
        from k2g.db_store.embedding_guard import (
            DIM_KEY,
            MODEL_KEY,
            EmbeddingFingerprintMismatch,
            fingerprint_problems,
            mismatch_message,
        )

        cur_dim = str(self._embedding_dim)
        cur_model = (self._embedding_model or "").strip()
        with self._conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS k2g_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            cur.execute(
                "SELECT key, value FROM k2g_meta WHERE key IN (%s, %s)",
                (DIM_KEY, MODEL_KEY),
            )
            rows = {r["key"]: r["value"] for r in cur.fetchall()}
            stored_dim = rows.get(DIM_KEY)
            stored_model = rows.get(MODEL_KEY)

            if stored_dim is None:  # new or legacy DB — stamp the fingerprint
                cur.execute(
                    "INSERT INTO k2g_meta (key, value) VALUES (%s, %s), (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (DIM_KEY, cur_dim, MODEL_KEY, cur_model),
                )
                self._conn.commit()
                return

            problems = fingerprint_problems(stored_dim, stored_model, cur_dim, cur_model)
            if problems:
                self._conn.rollback()
                raise EmbeddingFingerprintMismatch(
                    mismatch_message(stored_dim, stored_model, cur_dim, cur_model, problems)
                )
            if not stored_model and cur_model:  # back-fill model on a dim-only stamp
                cur.execute(
                    "INSERT INTO k2g_meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (MODEL_KEY, cur_model),
                )
                self._conn.commit()

    # ------------------------------------------------------------------
    # (a) Schema bootstrap — implementation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Schema version bookkeeping (perf guard — see module-level constants).
    # ------------------------------------------------------------------
    def _schema_up_to_date(
        self, component: str, version: int, sentinel_table: str
    ) -> bool:
        """True when the tier's DDL can be skipped (perf guard + adopt).

        Three cases, all decided with a couple of cheap queries:
        1. ``k2g_schema_meta`` has a row → skip iff it matches ``version``.
        2. No row, but the tier's core table (``sentinel_table``) already
           exists → the DB was bootstrapped by an older build (pre version
           guard). **Adopt** it: record ``version`` and skip the DDL. This is
           the critical case — re-running the ~100 idempotent DDL statements on
           a *populated* table needs an ``ALTER TABLE ... ACCESS EXCLUSIVE``
           lock and can be canceled by a remote pooler's ``statement_timeout``
           (2026-05-12 Supabase incident). Adopting avoids that replay entirely.
        3. No row and no sentinel table → genuinely fresh DB → return False so
           the caller runs the full DDL once (then records the version).

        ``RealDictCursor`` → rows are dict-like, keyed by column name. Probe
        failures fall back to False (run the idempotent DDL) — never abort the
        connect.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS k2g_schema_meta ("
                    "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
                )
                cur.execute(
                    "SELECT version FROM k2g_schema_meta WHERE component = %s",
                    (component,),
                )
                row = cur.fetchone()
                if row is not None:
                    self._conn.commit()
                    return int(row["version"]) == version
                # No recorded version — adopt the tier if it already exists.
                cur.execute("SELECT to_regclass(%s) AS reg", (sentinel_table,))
                already = cur.fetchone()["reg"] is not None
                if already:
                    self._mark_schema_version(cur, component, version)
                    self._conn.commit()
                    logger.info(
                        "Adopted existing %s schema as v%d (skipping DDL replay)",
                        component, version,
                    )
                    return True
            self._conn.commit()
            return False
        except psycopg2.Error:
            self._conn.rollback()
            return False

    def _mark_schema_version(self, cur: Any, component: str, version: int) -> None:
        """Upsert the recorded schema version (called inside the setup txn)."""
        cur.execute(
            "INSERT INTO k2g_schema_meta (component, version) VALUES (%s, %s) "
            "ON CONFLICT (component) DO UPDATE SET version = EXCLUDED.version",
            (component, version),
        )

    def setup_schema(self) -> None:
        """Tier 1 (Entity / Event / Group / 5 edges + entity_embedding_meta)
        + pgvector extension + HNSW indexes."""
        # domain_registry was added to Tier 1 after the version guard, so a DB
        # that adopts/skips Tier 1 (its ``events`` table already present) would
        # never get it → register_domain / list_managed_domains raise "relation
        # domain_registry does not exist". Ensure it unconditionally — it is tiny
        # and idempotent and takes no lock on existing tables.
        try:
            with self._conn.cursor() as cur:
                for sql in CREATE_DOMAIN_REGISTRY_SQL:
                    cur.execute(sql)
            self._conn.commit()
        except psycopg2.Error:
            self._conn.rollback()
        if self._schema_up_to_date("tier1", _TIER1_SCHEMA_VERSION, "public.events"):
            logger.debug("Tier1 schema already at v%d — skip DDL", _TIER1_SCHEMA_VERSION)
            return
        with self._conn.cursor() as cur:
            for sql in CREATE_EXTENSIONS_SQL:
                try:
                    cur.execute(sql)
                except psycopg2.Error as exc:
                    self._conn.rollback()
                    raise RuntimeError(
                        "Failed to create pgvector extension. "
                        "pgvector must be installed on the Postgres server. "
                        "On Supabase, enable it via "
                        "Dashboard → Database → Extensions → vector."
                    ) from exc
            # BP-47 Phase B — share tables MUST precede Tier1: the Tier1
            # ``groups`` table has an inline FK → k2g_share_group, so on a fresh
            # DB the referenced table has to exist first. (CREATE TABLE IF NOT
            # EXISTS makes this a no-op on an already-bootstrapped DB, so the
            # later, idempotent CREATE_SHARE_TABLES_SQL pass is harmless.)
            for sql in CREATE_SHARE_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_TIER1_TABLES_SQL(dim=self._embedding_dim):
                cur.execute(sql)
            for sql in CREATE_TIER1_EDGE_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_AUDIT_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_BUILD_AUDIT_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_FAILURE_MANIFEST_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_TRAIN_STATE_TABLES_SQL:
                cur.execute(sql)
            # BP-89 — Train run audit + community assignment
            for sql in CREATE_TRAIN_RUN_TABLES_SQL:
                cur.execute(sql)
            # BP-46 — Covenant metadata
            for sql in CREATE_COVENANT_META_TABLES_SQL:
                cur.execute(sql)
            # BP-47 Phase B — Share group + member + audit
            for sql in CREATE_SHARE_TABLES_SQL:
                cur.execute(sql)
            # BP-47 Phase C — cross-domain primitives
            for sql in CREATE_CROSS_DOMAIN_TABLES_SQL:
                cur.execute(sql)
            # Domain soft-registry (empty-domain support; idempotent)
            for sql in CREATE_DOMAIN_REGISTRY_SQL:
                cur.execute(sql)
            for sql in CREATE_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_AUDIT_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_BUILD_AUDIT_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_FAILURE_MANIFEST_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_TRAIN_STATE_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_TRAIN_RUN_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_COVENANT_META_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_SHARE_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_CROSS_DOMAIN_INDEXES_SQL:
                cur.execute(sql)
            # Ensure events.influence_score column exists (legacy migration).
            cur.execute(
                "ALTER TABLE events "
                "ADD COLUMN IF NOT EXISTS influence_score "
                "DOUBLE PRECISION NOT NULL DEFAULT 1.0",
            )
            # BP-39 — ner_method / ner_skip_reason
            cur.execute(
                "ALTER TABLE events "
                "ADD COLUMN IF NOT EXISTS ner_method VARCHAR(32)",
            )
            cur.execute(
                "ALTER TABLE events "
                "ADD COLUMN IF NOT EXISTS ner_skip_reason TEXT",
            )
            # BP-82 Phase 0 — entity_embedding_meta.coherence (mean resultant length)
            cur.execute(
                "ALTER TABLE entity_embedding_meta "
                "ADD COLUMN IF NOT EXISTS coherence DOUBLE PRECISION",
            )
            # Data Owner / Visibility: add 5 columns across 9 tables (idempotent).
            from k2g.security.data_owner import apply_data_owner_alters_postgres
            apply_data_owner_alters_postgres(cur)
            # Add groups.workspace_id — a cross-schema column not covered by
            # the Data Owner spec (which adds owner_id / visibility /
            # share_group_id / acl_json / org_id). Added here separately.
            # (plan_nodes.parent_id ALTER moved to setup_bp30_schema: plan_nodes
            #  is a Tier2b table created later, so altering it here broke a fresh
            #  DB with "relation plan_nodes does not exist".)
            cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS workspace_id TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_groups_workspace ON groups(workspace_id)",
            )
            # Source Lineage: ALTER events + create 3 new tables.
            from k2g.source.lineage_alter import apply_source_lineage_alters_postgres
            from k2g.db_store.postgres.source_lineage_schema import (
                setup_source_lineage_schema_postgres,
            )
            apply_source_lineage_alters_postgres(cur)
            setup_source_lineage_schema_postgres(cur)
            # BP-51 — Log System / Cost Reconciliation (Phase A + B + C)
            from k2g.db_store.postgres.schema import (
                CREATE_BP51_RUN_SUMMARY_TABLES_SQL,
                CREATE_BP51_RUN_SUMMARY_INDEXES_SQL,
                CREATE_BP51_STORAGE_USAGE_TABLES_SQL,
                CREATE_BP51_STORAGE_USAGE_INDEXES_SQL,
                CREATE_BP51_BALANCE_TABLES_SQL,
                CREATE_BP51_BALANCE_INDEXES_SQL,
                CREATE_BP51_CALIBRATION_TABLES_SQL,
                CREATE_BP51_CALIBRATION_INDEXES_SQL,
                CREATE_BP51_SQL_AUDIT_TABLES_SQL,
                CREATE_BP51_SQL_AUDIT_INDEXES_SQL,
                CREATE_BP51_VIEWS_SQL,
            )
            for sql in CREATE_BP51_RUN_SUMMARY_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_RUN_SUMMARY_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_STORAGE_USAGE_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_STORAGE_USAGE_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_BALANCE_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_BALANCE_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_CALIBRATION_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_CALIBRATION_INDEXES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_SQL_AUDIT_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_BP51_SQL_AUDIT_INDEXES_SQL:
                cur.execute(sql)
            # BP-51 Phase B — build_audit ALTER ADD COLUMN owner_id (idempotent)
            cur.execute(
                "ALTER TABLE build_audit "
                "ADD COLUMN IF NOT EXISTS owner_id VARCHAR(128)",
            )
            for sql in CREATE_BP51_VIEWS_SQL:
                cur.execute(sql)
            self._mark_schema_version(cur, "tier1", _TIER1_SCHEMA_VERSION)
        self._conn.commit()
        logger.info("PostgresGraphStore Tier1 schema initialized")

    def setup_training_schema(self) -> None:
        """Tier 2a ContextGroup (BP-28)."""
        if self._schema_up_to_date("tier2a", _TIER2A_SCHEMA_VERSION, "public.context_groups"):
            logger.debug("Tier2a schema already at v%d — skip DDL", _TIER2A_SCHEMA_VERSION)
            return
        with self._conn.cursor() as cur:
            for sql in CREATE_TIER2A_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_TIER2A_INDEXES_SQL:
                cur.execute(sql)
            # Add owner columns to context_groups.
            from k2g.security.data_owner import apply_data_owner_alters_postgres
            apply_data_owner_alters_postgres(cur)
            self._mark_schema_version(cur, "tier2a", _TIER2A_SCHEMA_VERSION)
        self._conn.commit()
        logger.info("PostgresGraphStore Tier2a schema initialized")

    def setup_bp30_schema(self) -> None:
        """Tier 2b ETG / PlanNode / PlanDirectionNode (BP-30)."""
        if self._schema_up_to_date("tier2b", _TIER2B_SCHEMA_VERSION, "public.plan_nodes"):
            logger.debug("Tier2b schema already at v%d — skip DDL", _TIER2B_SCHEMA_VERSION)
            return
        with self._conn.cursor() as cur:
            for sql in CREATE_TIER2B_TABLES_SQL:
                cur.execute(sql)
            for sql in CREATE_TIER2B_INDEXES_SQL:
                cur.execute(sql)
            # BP-80a — plan_nodes.parent_id (chain → tree). The base CREATE above
            # already includes the column on fresh DBs; this idempotent ALTER +
            # index backfills older DBs. Lives here (not in setup_schema) because
            # plan_nodes is a Tier2b table — altering it in Tier1 broke a fresh DB.
            cur.execute(
                "ALTER TABLE plan_nodes "
                "ADD COLUMN IF NOT EXISTS parent_id VARCHAR(64) "
                "REFERENCES plan_nodes(id) ON DELETE SET NULL",
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_nodes_parent "
                "ON plan_nodes(parent_id) WHERE parent_id IS NOT NULL",
            )
            # Add owner columns to Tier 2b tables.
            from k2g.security.data_owner import apply_data_owner_alters_postgres
            apply_data_owner_alters_postgres(cur)
            self._mark_schema_version(cur, "tier2b", _TIER2B_SCHEMA_VERSION)
        self._conn.commit()
        logger.info("PostgresGraphStore Tier2b schema initialized")

    # ------------------------------------------------------------------
    # (b) Tier 1 writes
    # ------------------------------------------------------------------

    def ensure_group_tree(self, domain_config: DomainConfig) -> dict[str, str]:
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
        """Atomic upsert keyed on ``name`` alone (no domain condition).

        Multiple concurrent clients inserting the same name are safe —
        the INSERT ON CONFLICT DO NOTHING pattern avoids UniqueViolation
        errors.  The old SELECT-then-INSERT approach was a check-then-act
        race that caused 23505 failures during concurrent ingest of tags
        and groups.  Same pattern as ``link_or_create_entity``.
        """
        from k2g.core.models import new_group_id

        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO groups "
                "(id, name, level, domain, parent_id, discriminator, "
                " original_name, source, summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (
                    new_group_id(), name, level, domain, parent_id,
                    discriminator or None,
                    original_name or None,
                    source or None,
                    summary or None,
                ),
            )
            # Backfill parent_id (new and existing rows, only when empty).
            if parent_id:
                cur.execute(
                    "UPDATE groups SET parent_id = %s "
                    "WHERE name = %s AND (parent_id IS NULL OR parent_id = '')",
                    (parent_id, name),
                )
            cur.execute("SELECT id FROM groups WHERE name = %s", (name,))
            row = cur.fetchone()
        self._conn.commit()
        return row["id"] if isinstance(row, dict) else row[0]

    def set_group_source(self, group_id: str, source: str) -> None:
        """Set a group's provenance ``source`` (axis label).

        Used to promote a Manager-added tag to the Discovery axis even when
        ``link_or_create_group``'s ON CONFLICT left a pre-existing row's source
        untouched.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET source = %s WHERE id = %s", (source, group_id),
            )
        self._conn.commit()

    def register_path_group_with_ancestors(
        self,
        leaf_canonical: str,
        *,
        domain: str,
        summary: str = "",
    ) -> str:
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
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM groups WHERE name = %s AND domain = %s",
                (group_name, domain),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return row["id"] if isinstance(row, dict) else row[0]

    def link_or_create_entity(
        self, name: str, domain: str, type: str | None = None,
    ) -> str:
        """Atomic upsert on (name, domain) — safe for concurrent clients.

        Multiple clients inserting the same (name, domain) concurrently
        will not raise UniqueViolation.  The old SELECT-then-INSERT was a
        check-then-act race that caused 23505 errors during concurrent
        ingest.  Same ON CONFLICT pattern as add_entities_bulk.  On
        conflict the INSERT waits for the competing transaction to commit
        then does nothing, and the following SELECT returns the
        authoritative id.
        """
        from k2g.core.models import new_entity_id

        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO entities (id, name, domain, type) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (name, domain) DO NOTHING",
                (new_entity_id(), name, domain, type or ""),
            )
            cur.execute(
                "SELECT id FROM entities WHERE name = %s AND domain = %s",
                (name, domain),
            )
            row = cur.fetchone()
        self._conn.commit()
        return row["id"] if isinstance(row, dict) else row[0]

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
        # NER metadata propagation — activates previously unused columns.
        ner_method: str | None = None,
        ner_skip_reason: str | None = None,
    ) -> str:
        """Create an event row with full ownership and NER metadata.

        Adds 5 owner columns (owner_id / org_id / visibility / acl_json /
        share_group_id, all additive with default 'public').
        ``ner_method`` / ``ner_skip_reason`` were schema-defined but
        missing from the INSERT path (always NULL); this fix activates them.
        """
        from k2g.core.models import new_event_id

        event_id = new_event_id()
        ts = _normalize_timestamp(timestamp)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (id, vector_id, domain, timestamp, order_index,
                                    summary, owner_id, org_id, visibility,
                                    acl_json, share_group_id,
                                    ner_method, ner_skip_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s, %s)
                """,
                (event_id, vector_id, domain, ts, order_index, summary,
                 owner_id, org_id, visibility, acl_json, share_group_id,
                 ner_method, ner_skip_reason),
            )
        self._conn.commit()
        return event_id

    def upsert_entity_connection(
        self, entity_id_a: str, entity_id_b: str, event_id: str,
    ) -> None:
        a, b = (entity_id_a, entity_id_b) if entity_id_a < entity_id_b else (entity_id_b, entity_id_a)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_connection (a_id, b_id, event_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (a_id, b_id)
                DO UPDATE SET event_count = entity_connection.event_count + 1
                """,
                (a, b),
            )
        self._conn.commit()

    def link_participated_in(self, entity_id: str, event_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO participated_in (entity_id, event_id)
                VALUES (%s, %s)
                ON CONFLICT (entity_id, event_id) DO NOTHING
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
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_member_of (event_id, group_id, kind)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id, group_id) DO NOTHING
                """,
                (event_id, group_id, kind),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Bulk insert APIs (psycopg2.extras.execute_values based)
    # ------------------------------------------------------------------
    # These methods do not commit — callers must commit inside db.session().

    def add_events_bulk(self, rows: list[dict[str, Any]]) -> list[str]:
        """Bulk INSERT into events; auto-generates id when not provided.

        Extracts ownership columns (owner_id / org_id / visibility /
        acl_json / share_group_id) from each row dict; missing values
        default to NULL / 'public'.
        ``ner_method`` / ``ner_skip_reason`` are also extracted; missing
        values default to NULL (no impact on existing callers).
        """
        from k2g.core.models import new_event_id
        out_ids: list[str] = []
        params: list[tuple[Any, ...]] = []
        for row in rows:
            ev_id = row.get("id") or new_event_id()
            out_ids.append(ev_id)
            # Normalise timestamp the same way as single-row create_event.
            # Non-standard LLM output like "2026-04-13~14" would cause
            # Postgres to reject the entire batch; _normalize_timestamp
            # attempts ISO-8601 conversion and falls back to None.
            ts = _normalize_timestamp(row.get("timestamp"))
            params.append((
                ev_id,
                row.get("vector_id", ""),
                row.get("domain", ""),
                ts,
                row.get("order_index", 0),
                row.get("summary", ""),
                row.get("owner_id"),
                row.get("org_id"),
                row.get("visibility") or "public",
                row.get("acl_json"),
                row.get("share_group_id"),
                row.get("ner_method"),
                row.get("ner_skip_reason"),
            ))
        if not params:
            return out_ids
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            # ON CONFLICT(id) DO NOTHING: deterministic pre-assigned ids
            # (manifest dedup) skip duplicate INSERTs on re-runs.
            # UUID ids from the standard producer never conflict.
            execute_values(
                cur,
                """
                INSERT INTO events (id, vector_id, domain, timestamp, order_index,
                                    summary, owner_id, org_id, visibility,
                                    acl_json, share_group_id,
                                    ner_method, ner_skip_reason)
                VALUES %s
                ON CONFLICT (id) DO NOTHING
                """,
                params,
            )
        return out_ids

    def add_entities_bulk(self, rows: list[dict[str, Any]]) -> list[str]:
        """Bulk INSERT into entities. ON CONFLICT (name, domain) DO NOTHING."""
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
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO entities (id, name, domain, type)
                VALUES %s
                ON CONFLICT (name, domain) DO NOTHING
                """,
                params,
            )
        return out_ids

    def link_event_member_of_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk INSERT into event_member_of."""
        params = [
            (r["event_id"], r["group_id"], r.get("kind", "contains"))
            for r in rows
        ]
        if not params:
            return 0
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO event_member_of (event_id, group_id, kind)
                VALUES %s
                ON CONFLICT (event_id, group_id) DO NOTHING
                """,
                params,
            )
        return len(params)

    def link_or_create_entities_bulk(
        self, rows: list[dict[str, Any]],
    ) -> dict[tuple[str, str], str]:
        """Bulk MERGE entities and return a ``{(name, domain): id}`` mapping.

        Callers use the returned mapping to drive link_participated_in_bulk
        and upsert_entity_connections_bulk.

        ``rows = [{"name", "domain", "type"}]``. Duplicate (name, domain)
        pairs are collapsed to one row (first-seen type wins).

        Implementation: INSERT ON CONFLICT DO NOTHING followed by a
        separate SELECT.  The DO UPDATE ... RETURNING pattern is avoided
        because psycopg2.extras.execute_values splits into pages, which
        can silently drop RETURNING rows.  The SELECT always returns the
        authoritative row for every (name, domain) pair.
        """
        from k2g.core.models import new_entity_id
        if not rows:
            return {}
        # Deduplicate (name, domain) — keep the first-seen type.
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
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            insert_result = execute_values(
                cur,
                """
                INSERT INTO entities (id, name, domain, type)
                VALUES %s
                ON CONFLICT (name, domain) DO NOTHING
                """,
                params,
                page_size=1000,
            )
            insert_rowcount = cur.rowcount
            # SELECT the authoritative id (existing on conflict, newly
            # assigned otherwise).  Uses a row-constructor IN pattern
            # with psycopg2 placeholder expansion — equivalent semantics
            # to SQLite.  The unnest-array pattern was avoided due to
            # serialisation issues with certain string values.
            keys = list(unique.keys())
            placeholders = ",".join(["(%s, %s)"] * len(keys))
            flat: list[Any] = []
            for name, domain in keys:
                flat.extend([name, domain])
            cur.execute(
                f"""
                SELECT name, domain, id FROM entities
                WHERE (name, domain) IN ({placeholders})
                """,
                flat,
            )
            select_rows = cur.fetchall()
            # Handle both RealDictCursor (dict rows, k2g default) and
            # a plain cursor (tuple rows).
            def _row_get(row, key, idx):
                if isinstance(row, dict):
                    return row[key]
                return row[idx]
            mapping = {
                (_row_get(r, "name", 0), _row_get(r, "domain", 1)): _row_get(r, "id", 2)
                for r in select_rows
            }
            return mapping

    def link_participated_in_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk INSERT into participated_in.

        ``rows = [{"entity_id", "event_id"}]``.
        """
        params = [(r["entity_id"], r["event_id"]) for r in rows]
        if not params:
            return 0
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO participated_in (entity_id, event_id)
                VALUES %s
                ON CONFLICT (entity_id, event_id) DO NOTHING
                """,
                params,
            )
        return len(params)

    def upsert_entity_connections_bulk(
        self, rows: list[dict[str, Any]],
    ) -> int:
        """Bulk UPSERT into entity_connection (accumulates event_count).

        ``rows = [{"a_id", "b_id", "count"}]``.  Callers must normalise
        a < b ordering and pre-aggregate counts for the same (a, b) pair
        because ON CONFLICT DO UPDATE does not allow duplicate keys within
        the same statement.
        """
        params = [
            (r["a_id"], r["b_id"], int(r.get("count", 1)))
            for r in rows
        ]
        if not params:
            return 0
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO entity_connection (a_id, b_id, event_count)
                VALUES %s
                ON CONFLICT (a_id, b_id)
                DO UPDATE SET
                    event_count = entity_connection.event_count + EXCLUDED.event_count
                """,
                params,
            )
        return len(params)

    def link_sequential_next_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk INSERT into event_sequential_next.

        ``rows = [{"prev_id", "next_id", "source"}]``. source defaults
        to ``"chunk_order"``.
        """
        params = [
            (r["prev_id"], r["next_id"], r.get("source", "chunk_order"))
            for r in rows
        ]
        if not params:
            return 0
        from psycopg2.extras import execute_values
        with self._conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO event_sequential_next (prev_id, next_id, source)
                VALUES %s
                ON CONFLICT (prev_id, next_id, source) DO NOTHING
                """,
                params,
            )
        return len(params)

    def reparent_group(self, group_id: str, new_parent_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET parent_id = %s WHERE id = %s",
                (new_parent_id, group_id),
            )
        self._conn.commit()

    def link_sequential_next(
        self,
        prev_event_id: str,
        event_id: str,
        source: SequentialSource = "chunk_order",
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_sequential_next (prev_id, next_id, source)
                VALUES (%s, %s, %s)
                ON CONFLICT (prev_id, next_id, source) DO NOTHING
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
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET user_tag = %s WHERE id = %s",
                (user_tag, node_id),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # (c) Tier 1 reads / statistics
    # ------------------------------------------------------------------

    def get_last_event_id(self, domain: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM events WHERE domain = %s AND NOT deprecated "
                "ORDER BY order_index DESC LIMIT 1",
                (domain,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return row["id"] if isinstance(row, dict) else row[0]

    def get_order_index(self, domain: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(order_index), -1) + 1 AS next_idx "
                "FROM events WHERE domain = %s AND NOT deprecated",
                (domain,),
            )
            row = cur.fetchone()
        if row is None:
            return 0
        val = row["next_idx"] if isinstance(row, dict) else row[0]
        return int(val) if val is not None else 0

    def get_entity_by_id(self, entity_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM entities WHERE id = %s", (entity_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_entity_event_counts(
        self, entity_ids: list[str],
    ) -> dict[str, int]:
        if not entity_ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, COUNT(*)
                FROM participated_in
                WHERE entity_id = ANY(%s)
                GROUP BY entity_id
                """,
                (list(entity_ids),),
            )
            rows = cur.fetchall()
        out: dict[str, int] = {eid: 0 for eid in entity_ids}
        for row in rows:
            row_eid = row["entity_id"] if isinstance(row, dict) else row[0]
            row_cnt = row["count"] if isinstance(row, dict) else row[1]
            out[str(row_eid)] = int(row_cnt or 0)
        return out

    def replace_entity_in_links(
        self, alias_id: str, canonical_id: str,
    ) -> dict[str, int]:
        """BP-87 entity merge helper — Postgres parity of sqlite version.

        Strategy mirrors `SqliteGraphStore.replace_entity_in_links`:
          1. participated_in: dedup canonical-already-has rows, UPDATE the rest.
          2. entity_connection: re-insert alias-touching pairs with the
             canonical id (proper ordering for CHECK a_id < b_id), merge
             event_count on conflict, then delete leftover alias rows.

        Caller wraps in a transaction (e.g. ``db.session()``).
        """
        if alias_id == canonical_id:
            raise ValueError("alias and canonical must differ")

        with self._conn.cursor() as cur:
            # --- 1. participated_in ---
            cur.execute(
                """
                DELETE FROM participated_in
                WHERE entity_id = %s
                  AND event_id IN (
                      SELECT event_id FROM participated_in
                      WHERE entity_id = %s
                  )
                """,
                (alias_id, canonical_id),
            )
            pi_dedup = cur.rowcount or 0
            cur.execute(
                "UPDATE participated_in SET entity_id = %s WHERE entity_id = %s",
                (canonical_id, alias_id),
            )
            pi_moved = cur.rowcount or 0

            # --- 2. entity_connection ---
            cur.execute(
                """
                INSERT INTO entity_connection (a_id, b_id, event_count, created_at)
                SELECT
                    CASE
                        WHEN %s < (CASE WHEN a_id = %s THEN b_id ELSE a_id END)
                            THEN %s
                        ELSE (CASE WHEN a_id = %s THEN b_id ELSE a_id END)
                    END AS new_a,
                    CASE
                        WHEN %s < (CASE WHEN a_id = %s THEN b_id ELSE a_id END)
                            THEN (CASE WHEN a_id = %s THEN b_id ELSE a_id END)
                        ELSE %s
                    END AS new_b,
                    event_count,
                    created_at
                FROM entity_connection
                WHERE (a_id = %s OR b_id = %s)
                  AND NOT (a_id = %s AND b_id = %s)
                  AND NOT (a_id = %s AND b_id = %s)
                ON CONFLICT (a_id, b_id) DO UPDATE
                    SET event_count = entity_connection.event_count + EXCLUDED.event_count
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
            cur.execute(
                "DELETE FROM entity_connection WHERE a_id = %s OR b_id = %s",
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
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO train_run (
                    id, kind, domain, status,
                    event_count, entity_count, edge_count,
                    params_json, triggered_by
                ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s::jsonb, %s)
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
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE train_run
                SET status = 'completed',
                    finished_at = NOW(),
                    metrics_json = %s::jsonb,
                    rows_written = %s
                WHERE id = %s AND status = 'running'
                """,
                (
                    json.dumps(metrics) if metrics is not None else None,
                    rows_written,
                    run_id,
                ),
            )
        self._conn.commit()

    def train_run_fail(self, run_id: str, error_text: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE train_run
                SET status = 'failed',
                    finished_at = NOW(),
                    error_text = %s
                WHERE id = %s AND status = 'running'
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
        with self._conn.cursor() as cur:
            if domain is None:
                cur.execute(
                    """
                    SELECT * FROM train_run
                    WHERE kind = %s AND domain IS NULL AND status = 'completed'
                    ORDER BY finished_at DESC LIMIT 1
                    """,
                    (kind,),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM train_run
                    WHERE kind = %s AND domain = %s AND status = 'completed'
                    ORDER BY finished_at DESC LIMIT 1
                    """,
                    (kind, domain),
                )
            row = cur.fetchone()
        return dict(row) if row else None

    def upsert_entity_community_assignments(
        self,
        run_id: str,
        assignments: list[tuple[str, int]],
    ) -> int:
        if not assignments:
            return 0
        rows = [(run_id, eid, int(cid)) for eid, cid in assignments]
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO entity_community_assignment (run_id, entity_id, community_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id, entity_id) DO UPDATE
                    SET community_id = EXCLUDED.community_id
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
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, community_id
                FROM entity_community_assignment
                WHERE run_id = %s AND entity_id = ANY(%s)
                """,
                (run_id, list(entity_ids)),
            )
            rows = cur.fetchall()
        out: dict[str, int] = {}
        for row in rows:
            row_eid = row["entity_id"] if isinstance(row, dict) else row[0]
            row_cid = row["community_id"] if isinstance(row, dict) else row[1]
            out[str(row_eid)] = int(row_cid)
        return out

    def upsert_event_community_assignments(
        self,
        run_id: str,
        assignments: list[tuple[str, int]],
    ) -> int:
        if not assignments:
            return 0
        rows = [(run_id, eid, int(cid)) for eid, cid in assignments]
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO event_community_assignment (run_id, event_id, community_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id, event_id) DO UPDATE
                    SET community_id = EXCLUDED.community_id
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
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, community_id
                FROM event_community_assignment
                WHERE run_id = %s AND event_id = ANY(%s)
                """,
                (run_id, list(event_ids)),
            )
            rows = cur.fetchall()
        out: dict[str, int] = {}
        for row in rows:
            row_eid = row["event_id"] if isinstance(row, dict) else row[0]
            row_cid = row["community_id"] if isinstance(row, dict) else row[1]
            out[str(row_eid)] = int(row_cid)
        return out

    def list_community_members(
        self,
        run_id: str,
        node_kind: str,
    ) -> list[dict[str, Any]]:
        # Compute weight via a single GROUP BY aggregate + LEFT JOIN
        # instead of N correlated sub-queries per member row.
        if node_kind == "entity":
            sql = """
                SELECT a.entity_id AS node_id, a.community_id, e.name,
                  COALESCE(w.c, 0) AS weight
                FROM entity_community_assignment a
                JOIN entities e ON e.id = a.entity_id
                LEFT JOIN (
                    SELECT entity_id, COUNT(*) AS c FROM participated_in
                    GROUP BY entity_id
                ) w ON w.entity_id = a.entity_id
                WHERE a.run_id = %s
            """
        elif node_kind == "event":
            sql = """
                SELECT a.event_id AS node_id, a.community_id, ev.summary AS name,
                  COALESCE(w.c, 0) AS weight
                FROM event_community_assignment a
                JOIN events ev ON ev.id = a.event_id
                LEFT JOIN (
                    SELECT event_id, COUNT(*) AS c FROM participated_in
                    GROUP BY event_id
                ) w ON w.event_id = a.event_id
                WHERE a.run_id = %s
            """
        else:
            raise ValueError(f"list_community_members: bad node_kind={node_kind!r}")
        with self._conn.cursor() as cur:
            cur.execute(sql, (run_id,))
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append({
                    "node_id": str(row["node_id"]),
                    "community_id": int(row["community_id"]),
                    "name": row.get("name") or "",
                    "weight": int(row.get("weight") or 0),
                })
            else:
                out.append({
                    "node_id": str(row[0]),
                    "community_id": int(row[1]),
                    "name": row[2] or "",
                    "weight": int(row[3] or 0),
                })
        return out

    def get_event_by_id(self, event_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM events WHERE id = %s", (event_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_connected_entities(
        self, entity_id: str, max_hops: int = 2,
    ) -> list[dict[str, Any]]:
        """BFS up to max_hops hops over entity_connection (a_id < b_id)."""
        hops = max(1, min(int(max_hops), 5))
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE reach(id, depth) AS (
                    SELECT %s::varchar AS id, 0 AS depth
                    UNION
                    SELECT CASE WHEN ec.a_id = reach.id THEN ec.b_id ELSE ec.a_id END,
                           reach.depth + 1
                    FROM entity_connection ec
                    JOIN reach ON (ec.a_id = reach.id OR ec.b_id = reach.id)
                    WHERE reach.depth < %s
                )
                SELECT e.* FROM entities e
                JOIN reach ON e.id = reach.id
                WHERE reach.id != %s
                """,
                (entity_id, hops, entity_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_entity_connections(self, domain: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT ec.a_id, ec.b_id, ec.event_count
                  FROM entity_connection ec
                  JOIN entities ea ON ea.id = ec.a_id
                  JOIN entities eb ON eb.id = ec.b_id
                 WHERE ea.domain = %s AND eb.domain = %s
                   AND NOT COALESCE(ea.deprecated, FALSE)
                   AND NOT COALESCE(eb.deprecated, FALSE)
                 ORDER BY ec.event_count DESC
                """,
                (domain, domain),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_entity_connection_events(
        self, a_id: str, b_id: str, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.*
                  FROM events e
                  JOIN participated_in p1 ON p1.event_id = e.id AND p1.entity_id = %s
                  JOIN participated_in p2 ON p2.event_id = e.id AND p2.entity_id = %s
                 WHERE NOT COALESCE(e.deprecated, FALSE)
                 ORDER BY e.timestamp DESC NULLS LAST, e.order_index DESC
                 LIMIT %s
                """,
                (a_id, b_id, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

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
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM events "
                "WHERE domain = %s AND NOT COALESCE(deprecated, FALSE)",
                (domain,),
            )
            row = cur.fetchone()
            total = int((row["c"] if isinstance(row, dict) else (row[0] if row else 0)) or 0)
            offset = max(0, (int(page) - 1) * int(size))
            cur.execute(
                f"SELECT * FROM events WHERE domain = %s "
                f"AND NOT COALESCE(deprecated, FALSE) "
                f"ORDER BY {col} DESC NULLS LAST LIMIT %s OFFSET %s",
                (domain, int(size), offset),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows], total

    def get_statistics(self, domain: str | None = None) -> dict[str, int]:
        stats: dict[str, int] = {}
        with self._conn.cursor() as cur:
            for table in ("entities", "events", "groups", "context_groups"):
                try:
                    if domain:
                        cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE domain = %s", (domain,))
                    else:
                        cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
                    row = cur.fetchone()
                    stats[table] = int((row["c"] if row else 0) or 0)
                except psycopg2.Error:
                    self._conn.rollback()
                    stats[table] = 0
        return stats

    def get_all_entity_names(self, limit: int = 10000) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM entities LIMIT %s", (int(limit),))
            return [row["name"] for row in cur.fetchall()]

    def list_entities(
        self,
        domain: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        conds: list[str] = []
        params: list[Any] = []
        if domain:
            conds.append("domain = %s")
            params.append(domain)
        if not include_deleted:
            conds.append("(deprecated IS NULL OR deprecated = FALSE)")
        where = f"WHERE {' AND '.join(conds)}" if conds else ""

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM entities {where}", params)
            row = cur.fetchone()
            total = int((row["c"] if row else 0) or 0)

            offset = max(0, (int(page) - 1) * int(size))
            cur.execute(
                f"SELECT * FROM entities {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (*params, int(size), offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows, total

    def search_entities_by_name(
        self, name: str, domain: str | list[str] | None = None, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search. list[str] domain uses IN, str uses =."""
        conds = ["LOWER(name) LIKE %s"]
        params: list[Any] = [f"%{name.lower()}%"]
        if domain:
            if isinstance(domain, list):
                placeholders = ", ".join(["%s"] * len(domain))
                conds.append(f"domain IN ({placeholders})")
                params.extend(domain)
            else:
                conds.append("domain = %s")
                params.append(domain)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM entities WHERE {' AND '.join(conds)} LIMIT %s",
                (*params, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_groups(self, domain: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM groups WHERE domain = %s ORDER BY parent_id NULLS FIRST, id",
                (domain,),
            )
            return [dict(r) for r in cur.fetchall()]

    def search_groups_by_name(
        self,
        name: str,
        domain: str | list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search over groups (tags)."""
        conds = ["LOWER(name) LIKE %s", "(deprecated = FALSE OR deprecated IS NULL)"]
        params: list[Any] = [f"%{name.lower()}%"]
        if domain:
            if isinstance(domain, list):
                placeholders = ", ".join(["%s"] * len(domain))
                conds.append(f"domain IN ({placeholders})")
                params.extend(domain)
            else:
                conds.append("domain = %s")
                params.append(domain)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM groups WHERE {' AND '.join(conds)} "
                f"ORDER BY LENGTH(name) ASC, name ASC LIMIT %s",
                (*params, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def merge_groups(self, alias_id: str, canonical_id: str) -> dict[str, Any]:
        if alias_id == canonical_id:
            raise ValueError("alias and canonical must differ")
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT deprecated FROM groups WHERE id = %s", (alias_id,))
                a = cur.fetchone()
                cur.execute("SELECT deprecated FROM groups WHERE id = %s", (canonical_id,))
                c = cur.fetchone()
                if a is None:
                    raise ValueError(f"alias group not found: {alias_id}")
                if c is None:
                    raise ValueError(f"canonical group not found: {canonical_id}")
                # psycopg2 may return a dict-row (RealDictCursor) or tuple
                c_dep = c["deprecated"] if isinstance(c, dict) else c[0]
                if c_dep:
                    raise ValueError(f"canonical group is deprecated: {canonical_id}")
                cur.execute(
                    "DELETE FROM event_member_of WHERE group_id = %s "
                    "AND event_id IN (SELECT event_id FROM event_member_of WHERE group_id = %s)",
                    (alias_id, canonical_id),
                )
                dedup = cur.rowcount or 0
                cur.execute(
                    "UPDATE event_member_of SET group_id = %s WHERE group_id = %s",
                    (canonical_id, alias_id),
                )
                moved = cur.rowcount or 0
                cur.execute(
                    "UPDATE groups SET parent_id = %s WHERE parent_id = %s",
                    (canonical_id, alias_id),
                )
                reparented = cur.rowcount or 0
                cur.execute("UPDATE groups SET deprecated = TRUE WHERE id = %s", (alias_id,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {
            "alias_id": alias_id, "canonical_id": canonical_id,
            "memberships_moved": int(moved), "memberships_dedup": int(dedup),
            "children_reparented": int(reparented),
        }

    def list_event_ids_by_group(
        self, group_id_or_name: "str | list[str]",
    ) -> list[str]:
        """Return deduplicated event IDs for a group and all its descendants (recursive CTE).

        Matches on groups.id or groups.name.  When a list is given,
        returns the union of sub-trees for each group.
        """
        if isinstance(group_id_or_name, str):
            seeds = [group_id_or_name]
        else:
            seeds = list(group_id_or_name)
        if not seeds:
            return []
        with self._conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(seeds))
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
            return [r["event_id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]

    def get_entity_events(
        self,
        entity_id: str,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        deleted_filter = "" if include_deleted else " AND (e.deprecated IS NULL OR e.deprecated = FALSE)"
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS c FROM participated_in pi "
                f"JOIN events e ON e.id = pi.event_id "
                f"WHERE pi.entity_id = %s{deleted_filter}",
                (entity_id,),
            )
            row = cur.fetchone()
            total = int((row["c"] if row else 0) or 0)

            offset = max(0, (int(page) - 1) * int(size))
            cur.execute(
                f"SELECT e.* FROM participated_in pi "
                f"JOIN events e ON e.id = pi.event_id "
                f"WHERE pi.entity_id = %s{deleted_filter} "
                f"ORDER BY e.order_index DESC NULLS LAST LIMIT %s OFFSET %s",
                (entity_id, int(size), offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows, total

    def get_group_events(
        self,
        group_id: str,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        deleted_filter = "" if include_deleted else " AND (e.deprecated IS NULL OR e.deprecated = FALSE)"
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS c FROM event_member_of m "
                f"JOIN events e ON e.id = m.event_id "
                f"WHERE m.group_id = %s{deleted_filter}",
                (group_id,),
            )
            row = cur.fetchone()
            total = int((row["c"] if row else 0) or 0)

            offset = max(0, (int(page) - 1) * int(size))
            cur.execute(
                f"SELECT e.* FROM event_member_of m "
                f"JOIN events e ON e.id = m.event_id "
                f"WHERE m.group_id = %s{deleted_filter} "
                f"ORDER BY e.order_index DESC NULLS LAST LIMIT %s OFFSET %s",
                (group_id, int(size), offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows, total

    def get_adjacent_events(self, event_id: str) -> dict[str, Any]:
        # INNER JOIN events enforces RLS: events blocked by a policy are
        # excluded from prev_id/next_id.  Without the join, raw ids from
        # event_sequential_next would leak blocked event ids (fail-open)
        # and produce dangling references.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT sn.prev_id FROM event_sequential_next sn "
                "JOIN events e ON e.id = sn.prev_id "
                "WHERE sn.next_id = %s LIMIT 1",
                (event_id,),
            )
            prev = cur.fetchone()
            cur.execute(
                "SELECT sn.next_id FROM event_sequential_next sn "
                "JOIN events e ON e.id = sn.next_id "
                "WHERE sn.prev_id = %s LIMIT 1",
                (event_id,),
            )
            nxt = cur.fetchone()
        return {
            "prev_id": prev["prev_id"] if prev else None,
            "next_id": nxt["next_id"] if nxt else None,
        }

    def get_event_context(self, event_id: str) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT e.id, e.name FROM participated_in pi "
                "JOIN entities e ON e.id = pi.entity_id WHERE pi.event_id = %s",
                (event_id,),
            )
            entities = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]
            cur.execute(
                "SELECT g.id, g.name FROM event_member_of m "
                "JOIN groups g ON g.id = m.group_id WHERE m.event_id = %s",
                (event_id,),
            )
            groups = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]
        return {"entities": entities, "groups": groups}

    def list_domains(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT DISTINCT domain FROM events ORDER BY domain")
            return [row["domain"] for row in cur.fetchall() if row["domain"]]

    # --- Domain soft-registry (empty-domain support) ------------------

    def register_domain(self, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO domain_registry (name) VALUES (%s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (name,),
                )
                added = cur.rowcount > 0
            self._conn.commit()
            return added
        except psycopg2.Error:
            self._conn.rollback()  # don't leave the shared connection poisoned
            raise

    def list_registered_domains(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT name FROM domain_registry ORDER BY name")
            return [row["name"] for row in cur.fetchall() if row["name"]]

    def list_managed_domains(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM domain_registry "
                "UNION SELECT domain FROM events "
                "UNION SELECT domain FROM entities "
                "UNION SELECT domain FROM groups",
            )
            return sorted({row["name"] for row in cur.fetchall() if row["name"]})

    def domain_data_counts(self, name: str) -> dict[str, int]:
        with self._conn.cursor() as cur:
            def _count(table: str) -> int:
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE domain = %s", (name,),
                )
                return int(cur.fetchone()["n"])

            return {
                "events": _count("events"),
                "entities": _count("entities"),
                "groups": _count("groups"),
            }

    def delete_domain(self, name: str) -> dict[str, Any]:
        counts = self.domain_data_counts(name)
        if counts["events"] > 0 or counts["entities"] > 0:
            return {"ok": False, "reason": "has_data", **counts}
        try:
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM groups WHERE domain = %s", (name,))
                cur.execute("DELETE FROM domain_registry WHERE name = %s", (name,))
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
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND column_name IN ('domain', 'source_domain')",
                )
                targets = [(r["table_name"], r["column_name"]) for r in cur.fetchall()]
                updated: set[str] = set()
                for table, col in targets:
                    cur.execute(
                        f'UPDATE "{table}" SET {col} = %s WHERE {col} = %s',
                        (new, old),
                    )
                    if cur.rowcount > 0:
                        updated.add(table)
                cur.execute(
                    "UPDATE domain_registry SET name = %s WHERE name = %s "
                    "AND NOT EXISTS (SELECT 1 FROM domain_registry WHERE name = %s)",
                    (new, old, new),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return {"ok": True, "tables": sorted(updated)}

    def check_entities_exist(self, names: list[str]) -> list[dict[str, Any]]:
        if not names:
            return []
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT name, id, user_tag FROM entities WHERE name = ANY(%s)",
                (list(names),),
            )
            return [
                {"name": r["name"], "id": r["id"], "user_tag": r["user_tag"]}
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------
    # (d) Jaccard / chain reads
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
        """UPSERT into event_jaccard_connected (normalises a_id < b_id)."""
        a, b = (event_id, other_id) if event_id < other_id else (other_id, event_id)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_jaccard_connected
                    (a_id, b_id, entity_jaccard, group_jaccard,
                     entity_intersection, group_intersection)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (a_id, b_id) DO UPDATE SET
                    entity_jaccard      = EXCLUDED.entity_jaccard,
                    group_jaccard       = EXCLUDED.group_jaccard,
                    entity_intersection = EXCLUDED.entity_intersection,
                    group_intersection  = EXCLUDED.group_intersection
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
        work_mem: str = "128MB",
        exclude_stopwords: bool = True,
    ) -> int:
        """SQL-native Jaccard: extract candidate pairs, score, filter, and
        INSERT in a single CTE with zero Python-side per-pair round-trips.

        The legacy O(N²) approach took hours at 28M+ pair scale; the
        Postgres hash-join + index scan implementation completes in minutes.

        ``max_entity_degree`` / ``max_group_degree``: entities/groups
        appearing in more than K events are excluded from candidates,
        preventing hash-join explosion.  In practice (10 075 events,
        29 410 entities) a cap of 200 excluded just 13 noisy entities
        but reduced candidates from 28.9 M to 674 k (43x reduction).
        Pass ``None`` to disable the cap.

        ``work_mem`` is applied via ``SET LOCAL`` inside the transaction
        so that large hashes fit in RAM even when the server-wide setting
        is small (e.g. 2 MB).  Reverts automatically on commit/rollback.

        ``exclude_stopwords`` (default True): entities tagged
        'stopword' are also excluded from candidates, removing
        user-curated noise in the mid-degree range that degree-cap alone
        does not catch.  The stopword_ent CTE returns 0 rows when
        disabled.

        Semantics match the legacy implementation: an edge is created
        when either the entity or group Jaccard threshold is satisfied;
        both Jaccard and intersection values are identical.
        """
        del scope
        stop_list: list[str] = list(stop_groups) if stop_groups else []
        ent_cap = max_entity_degree if max_entity_degree is not None else 2_000_000_000
        grp_cap = max_group_degree if max_group_degree is not None else 2_000_000_000

        sql = """
        WITH
        high_deg_ent AS (
            SELECT pi.entity_id
              FROM participated_in pi
              JOIN events e ON e.id = pi.event_id
             WHERE e.domain = %s AND NOT e.deprecated
             GROUP BY pi.entity_id
            HAVING COUNT(*) > %s
        ),
        high_deg_grp AS (
            SELECT em.group_id
              FROM event_member_of em
              JOIN events e ON e.id = em.event_id
             WHERE e.domain = %s AND NOT e.deprecated
             GROUP BY em.group_id
            HAVING COUNT(*) > %s
        ),
        stopword_ent AS (
            SELECT id AS entity_id FROM entities
             WHERE %s::boolean
               AND user_tag = 'stopword' AND domain = %s
        ),
        pi_dom AS (
            SELECT pi.event_id, pi.entity_id
              FROM participated_in pi
              JOIN events e ON e.id = pi.event_id
             WHERE e.domain = %s AND NOT e.deprecated
               AND pi.entity_id NOT IN (SELECT entity_id FROM high_deg_ent)
               AND pi.entity_id NOT IN (SELECT entity_id FROM stopword_ent)
        ),
        em_dom AS (
            SELECT em.event_id, em.group_id
              FROM event_member_of em
              JOIN events e ON e.id = em.event_id
             WHERE e.domain = %s AND NOT e.deprecated
               AND em.group_id <> ALL(%s::text[])
               AND em.group_id NOT IN (SELECT group_id FROM high_deg_grp)
        ),
        ent_size AS (
            SELECT event_id, COUNT(*)::int AS sz
              FROM pi_dom GROUP BY event_id
        ),
        grp_size AS (
            SELECT event_id, COUNT(*)::int AS sz
              FROM em_dom GROUP BY event_id
        ),
        ent_pair AS (
            SELECT p1.event_id AS a_id, p2.event_id AS b_id,
                   COUNT(*)::int AS isect
              FROM pi_dom p1
              JOIN pi_dom p2
                ON p1.entity_id = p2.entity_id
               AND p1.event_id < p2.event_id
             GROUP BY p1.event_id, p2.event_id
        ),
        grp_pair AS (
            SELECT g1.event_id AS a_id, g2.event_id AS b_id,
                   COUNT(*)::int AS isect
              FROM em_dom g1
              JOIN em_dom g2
                ON g1.group_id = g2.group_id
               AND g1.event_id < g2.event_id
             GROUP BY g1.event_id, g2.event_id
        ),
        candidates AS (
            SELECT
                COALESCE(ep.a_id, gp.a_id) AS a_id,
                COALESCE(ep.b_id, gp.b_id) AS b_id,
                COALESCE(ep.isect, 0)      AS ent_i,
                COALESCE(gp.isect, 0)      AS grp_i
              FROM ent_pair ep
              FULL OUTER JOIN grp_pair gp
                ON ep.a_id = gp.a_id AND ep.b_id = gp.b_id
        ),
        scored AS (
            SELECT
                c.a_id, c.b_id, c.ent_i, c.grp_i,
                CASE WHEN COALESCE(esa.sz, 0) + COALESCE(esb.sz, 0)
                          - c.ent_i > 0
                    THEN c.ent_i::float8
                         / (COALESCE(esa.sz, 0) + COALESCE(esb.sz, 0)
                            - c.ent_i)
                    ELSE 0.0 END AS ent_j,
                CASE WHEN COALESCE(gsa.sz, 0) + COALESCE(gsb.sz, 0)
                          - c.grp_i > 0
                    THEN c.grp_i::float8
                         / (COALESCE(gsa.sz, 0) + COALESCE(gsb.sz, 0)
                            - c.grp_i)
                    ELSE 0.0 END AS grp_j
              FROM candidates c
              LEFT JOIN ent_size esa ON esa.event_id = c.a_id
              LEFT JOIN ent_size esb ON esb.event_id = c.b_id
              LEFT JOIN grp_size gsa ON gsa.event_id = c.a_id
              LEFT JOIN grp_size gsb ON gsb.event_id = c.b_id
        )
        INSERT INTO event_jaccard_connected
            (a_id, b_id, entity_jaccard, group_jaccard,
             entity_intersection, group_intersection)
        SELECT a_id, b_id, ent_j, grp_j, ent_i, grp_i
          FROM scored
         WHERE ent_j > 0
            OR (grp_j >= %s AND grp_i >= %s)
        ON CONFLICT (a_id, b_id) DO UPDATE SET
            entity_jaccard      = EXCLUDED.entity_jaccard,
            group_jaccard       = EXCLUDED.group_jaccard,
            entity_intersection = EXCLUDED.entity_intersection,
            group_intersection  = EXCLUDED.group_intersection
        """

        with self._conn.cursor() as cur:
            # Raise work_mem only for this transaction; reverts on commit/rollback.
            cur.execute("SET LOCAL work_mem = %s", (work_mem,))
            cur.execute(sql, (
                domain, ent_cap,        # high_deg_ent
                domain, grp_cap,        # high_deg_grp
                bool(exclude_stopwords), domain,  # stopword_ent
                domain,                 # pi_dom
                domain, stop_list,      # em_dom
                # entity_jaccard write gate removed; theta_e filtering is read-time.
                theta_g, min_group_intersection,  # grp threshold
            ))
            created = cur.rowcount
        self._conn.commit()

        logger.info(
            "Jaccard Connected (SQL-native): domain=%s store-all(theta_e read-time) "
            "theta_g=%.2f stop=%d ent_cap=%s grp_cap=%s created=%d",
            domain, theta_g, len(stop_list),
            max_entity_degree, max_group_degree, created,
        )
        return created

    def mark_deprecated(self, node_type: str, node_id: str) -> bool:
        table = {
            "event": "events", "events": "events",
            "entity": "entities", "entities": "entities",
            "group": "groups", "groups": "groups",
            "context_group": "context_groups", "context_groups": "context_groups",
        }.get(node_type)
        if table is None:
            raise ValueError(f"mark_deprecated: unsupported node_type={node_type!r}")
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET deprecated = TRUE "
                f"WHERE id = %s AND deprecated = FALSE",
                (node_id,),
            )
            changed = cur.rowcount
        self._conn.commit()
        return changed > 0

    def mark_events_deprecated_by_group(self, group_id: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                   SET deprecated = TRUE
                 WHERE NOT deprecated
                   AND id IN (SELECT event_id FROM event_member_of WHERE group_id = %s)
                """,
                (group_id,),
            )
            changed = cur.rowcount
        self._conn.commit()
        return changed

    def find_group_by_path(self, relative_path: str, domain: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM groups "
                "WHERE domain = %s AND NOT deprecated AND name LIKE %s "
                "LIMIT 1",
                (domain, f"%{relative_path}"),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return row["id"] if isinstance(row, dict) else row[0]

    def list_event_ids(self, domain: str) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM events "
                "WHERE domain = %s AND NOT deprecated "
                "ORDER BY order_index",
                (domain,),
            )
            rows = cur.fetchall()
        if rows and isinstance(rows[0], dict):
            return [r["id"] for r in rows]
        return [r[0] for r in rows]

    def iter_sequential_pairs(self, domain: str) -> list[tuple[str, str]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT sn.prev_id, sn.next_id
                  FROM event_sequential_next sn
                  JOIN events e ON e.id = sn.prev_id
                 WHERE e.domain = %s AND NOT e.deprecated
                """,
                (domain,),
            )
            rows = cur.fetchall()
        if rows and isinstance(rows[0], dict):
            return [(r["prev_id"], r["next_id"]) for r in rows]
        return [(r[0], r[1]) for r in rows]

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
        """Incremental per-event Jaccard computation for Postgres.

        Same semantics as sqlite ``compute_jaccard_for_event``, including
        degree cap: entities/groups appearing in more than cap events are
        treated as hubs and excluded (bounds per-event cost).  Hub
        detection uses a GROUP BY limited to the event's own entities,
        avoiding a full domain scan.  Idempotent — existing rows for
        event_id are deleted before recomputation.

        ``exclude_stopwords`` (default True): entities tagged 'stopword'
        are added to hub_ents via an IN-clause restricted to the event's
        entities, again avoiding a domain scan.
        """
        def _c(row: Any, idx: int, key: str) -> Any:
            return row[key] if isinstance(row, dict) else row[idx]

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT entity_id FROM participated_in WHERE event_id = %s",
                (event_id,),
            )
            raw_ents = {_c(r, 0, "entity_id") for r in cur.fetchall()}
            cur.execute(
                "SELECT group_id FROM event_member_of WHERE event_id = %s",
                (event_id,),
            )
            raw_grps = {_c(r, 0, "group_id") for r in cur.fetchall()}

            # Detect hubs (>cap events) limited to E's own entities/groups.
            # The IN-clause GROUP BY avoids a full domain scan.
            hub_ents: set[str] = set()
            if max_entity_degree is not None and raw_ents:
                ph = ",".join("%s" for _ in raw_ents)
                cur.execute(
                    f"SELECT entity_id FROM participated_in "
                    f"WHERE entity_id IN ({ph}) "
                    f"GROUP BY entity_id HAVING COUNT(*) > %s",
                    (*raw_ents, max_entity_degree),
                )
                hub_ents = {_c(r, 0, "entity_id") for r in cur.fetchall()}
            # Union in stopword entities — IN-clause limited to E's entities.
            if exclude_stopwords and raw_ents:
                ph = ",".join("%s" for _ in raw_ents)
                cur.execute(
                    f"SELECT id FROM entities "
                    f"WHERE id IN ({ph}) AND user_tag = 'stopword'",
                    tuple(raw_ents),
                )
                hub_ents.update(_c(r, 0, "id") for r in cur.fetchall())
            hub_grps: set[str] = set()
            if max_group_degree is not None and raw_grps:
                ph = ",".join("%s" for _ in raw_grps)
                cur.execute(
                    f"SELECT group_id FROM event_member_of "
                    f"WHERE group_id IN ({ph}) "
                    f"GROUP BY group_id HAVING COUNT(*) > %s",
                    (*raw_grps, max_group_degree),
                )
                hub_grps = {_c(r, 0, "group_id") for r in cur.fetchall()}
            e_ents = raw_ents - hub_ents
            e_grps = raw_grps - hub_grps

            cur.execute(
                "DELETE FROM event_jaccard_connected "
                "WHERE a_id = %s OR b_id = %s",
                (event_id, event_id),
            )
            if not e_ents and not e_grps:
                self._conn.commit()
                return 0

            cands: set[str] = set()
            if e_ents:
                ph = ",".join("%s" for _ in e_ents)
                cur.execute(
                    f"SELECT DISTINCT pi.event_id FROM participated_in pi "
                    f"JOIN events e ON e.id = pi.event_id "
                    f"WHERE pi.entity_id IN ({ph}) AND pi.event_id <> %s "
                    f"  AND e.domain = %s AND NOT e.deprecated",
                    (*e_ents, event_id, domain),
                )
                cands.update(_c(r, 0, "event_id") for r in cur.fetchall())
            if e_grps:
                ph = ",".join("%s" for _ in e_grps)
                cur.execute(
                    f"SELECT DISTINCT em.event_id FROM event_member_of em "
                    f"JOIN events e ON e.id = em.event_id "
                    f"WHERE em.group_id IN ({ph}) AND em.event_id <> %s "
                    f"  AND e.domain = %s AND NOT e.deprecated",
                    (*e_grps, event_id, domain),
                )
                cands.update(_c(r, 0, "event_id") for r in cur.fetchall())
            if not cands:
                self._conn.commit()
                return 0

            cph = ",".join("%s" for _ in cands)
            f_ents: dict[str, set[str]] = {}
            cur.execute(
                f"SELECT event_id, entity_id FROM participated_in "
                f"WHERE event_id IN ({cph})",
                tuple(cands),
            )
            for r in cur.fetchall():
                ent = _c(r, 1, "entity_id")
                if ent not in hub_ents:
                    f_ents.setdefault(_c(r, 0, "event_id"), set()).add(ent)
            f_grps: dict[str, set[str]] = {}
            cur.execute(
                f"SELECT event_id, group_id FROM event_member_of "
                f"WHERE event_id IN ({cph})",
                tuple(cands),
            )
            for r in cur.fetchall():
                grp = _c(r, 1, "group_id")
                if grp not in hub_grps:
                    f_grps.setdefault(_c(r, 0, "event_id"), set()).add(grp)

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
            # Store when entity_jaccard > 0; theta_e is applied at read time.
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
        """Return Jaccard neighbours for an event (Postgres implementation)."""
        def _c(row: Any, idx: int, key: str) -> Any:
            return row[key] if isinstance(row, dict) else row[idx]

        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT CASE WHEN a_id = %s THEN b_id ELSE a_id END AS nb,
                       GREATEST(COALESCE(entity_jaccard, 0),
                                COALESCE(group_jaccard, 0)) AS score
                  FROM event_jaccard_connected
                 WHERE a_id = %s OR b_id = %s
                 ORDER BY score DESC
                 LIMIT %s
                """,
                (event_id, event_id, event_id, int(limit)),
            )
            rows = cur.fetchall()
        return [
            {"event_id": _c(r, 0, "nb"), "score": float(_c(r, 1, "score") or 0.0)}
            for r in rows
        ]

    def iter_jaccard_pairs(self, domain: str) -> list[tuple[str, str, float]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.a_id, j.b_id,
                       GREATEST(COALESCE(j.entity_jaccard, 0),
                                COALESCE(j.group_jaccard, 0)) AS score
                  FROM event_jaccard_connected j
                  JOIN events ea ON ea.id = j.a_id
                  JOIN events eb ON eb.id = j.b_id
                 WHERE ea.domain = %s AND eb.domain = %s
                   AND NOT ea.deprecated AND NOT eb.deprecated
                """,
                (domain, domain),
            )
            rows = cur.fetchall()
        if rows and isinstance(rows[0], dict):
            return [(r["a_id"], r["b_id"], float(r["score"] or 0.0)) for r in rows]
        return [(r[0], r[1], float(r[2] or 0.0)) for r in rows]

    # ------------------------------------------------------------------
    # (e) ContextGroup Training
    # ------------------------------------------------------------------
    #
    # JSONB columns (expected_entities / transition_pattern / source_cg_ids):
    #   - dict/list values are JSON-serialised and cast with %s::jsonb.
    #   - Already-serialised JSON strings are passed through as-is.

    _CG_COLUMNS: tuple[str, ...] = (
        "id", "name", "stage", "cluster_source", "training_method", "confidence",
        "member_count_own", "member_count_total",
        "depth", "version", "narrative_summary", "order_index", "domain",
        "parent_id", "plan_stage", "plan_id", "expected_entities",
        "abandon_reason", "template_id", "transition_pattern",
        "source_cg_ids", "instance_count",
    )
    _CG_JSONB_COLUMNS: frozenset[str] = frozenset(
        ("expected_entities", "transition_pattern", "source_cg_ids")
    )

    def create_context_group(self, cg: dict[str, Any]) -> str:
        sql, params = self._build_insert_with_jsonb(
            "context_groups", self._CG_COLUMNS, self._CG_JSONB_COLUMNS, cg,
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()
        return str(cg["id"])

    def get_context_group(self, cg_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM context_groups WHERE id = %s", (cg_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def delete_context_group(self, cg_id: str) -> None:
        # Explicitly remove polymorphic edges pointing to this CG before
        # deleting it (event_belongs_to_context / realized_as /
        # plan_from / plan_next).
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM event_belongs_to_context "
                "WHERE target_id = %s AND target_kind = 'CG '",
                (cg_id,),
            )
            cur.execute(
                "DELETE FROM realized_as WHERE from_id = %s AND from_kind = 'CG '",
                (cg_id,),
            )
            cur.execute(
                "DELETE FROM plan_from WHERE target_id = %s AND target_kind = 'CG '",
                (cg_id,),
            )
            cur.execute(
                "DELETE FROM plan_next "
                "WHERE (from_id = %s AND from_kind = 'CG ') "
                "   OR (to_id   = %s AND to_kind   = 'CG ')",
                (cg_id, cg_id),
            )
            cur.execute("DELETE FROM context_groups WHERE id = %s", (cg_id,))
        self._conn.commit()

    def update_cg_stage(self, cg_id: str, stage: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET stage = %s WHERE id = %s",
                (stage, cg_id),
            )
        self._conn.commit()

    def update_cg_name(self, cg_id: str, name: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET name = %s WHERE id = %s",
                (name, cg_id),
            )
        self._conn.commit()

    def update_cg_member_counts(self, cg_id: str, own: int, total: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET member_count_own = %s, "
                "member_count_total = %s WHERE id = %s",
                (int(own), int(total), cg_id),
            )
        self._conn.commit()

    def update_cg_narrative(self, cg_id: str, summary: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET narrative_summary = %s WHERE id = %s",
                (summary, cg_id),
            )
        self._conn.commit()

    def update_cg_order(self, cg_id: str, order_index: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET order_index = %s WHERE id = %s",
                (int(order_index), cg_id),
            )
        self._conn.commit()

    def link_event_belongs_to_context(
        self, event_id: str, cg_id: str, kind: str = "",
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO event_belongs_to_context "
                "(event_id, target_id, target_kind, kind) "
                "VALUES (%s, %s, 'CG ', %s) "
                "ON CONFLICT (event_id, target_id, target_kind) "
                "DO UPDATE SET kind = EXCLUDED.kind",
                (event_id, cg_id, kind),
            )
        self._conn.commit()

    def link_cg_child_of(
        self, child_cg_id: str, parent_cg_id: str, depth: int = 1,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET parent_id = %s, depth = %s WHERE id = %s",
                (parent_cg_id, int(depth), child_cg_id),
            )
        self._conn.commit()

    def link_cg_realized_as(self, cg_id: str, event_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO realized_as (from_id, from_kind, event_id) "
                "VALUES (%s, 'CG ', %s) ON CONFLICT DO NOTHING",
                (cg_id, event_id),
            )
        self._conn.commit()

    def link_plan_from(self, event_id: str, cg_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_from (event_id, target_id, target_kind) "
                "VALUES (%s, %s, 'CG ') ON CONFLICT DO NOTHING",
                (event_id, cg_id),
            )
        self._conn.commit()

    def link_plan_next(
        self, from_cg_id: str, to_cg_id: str, order: int = 0,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_next "
                "(from_id, from_kind, to_id, to_kind, order_idx) "
                "VALUES (%s, 'CG ', %s, 'CG ', %s) "
                "ON CONFLICT (from_id, from_kind, to_id, to_kind) "
                "DO UPDATE SET order_idx = EXCLUDED.order_idx",
                (from_cg_id, to_cg_id, int(order)),
            )
        self._conn.commit()

    def get_cg_members(self, cg_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT event_id FROM event_belongs_to_context "
                "WHERE target_id = %s AND target_kind = 'CG ' LIMIT 1000",
                (cg_id,),
            )
            return [{"id": row["event_id"], "labels": ["Event"]} for row in cur.fetchall()]

    def get_cg_events(self, cg_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT e.* FROM events e "
                "JOIN event_belongs_to_context m ON m.event_id = e.id "
                "WHERE m.target_id = %s AND m.target_kind = 'CG ' "
                "ORDER BY e.order_index ASC NULLS LAST",
                (cg_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_contexts_of_event(self, event_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT cg.* FROM context_groups cg "
                "JOIN event_belongs_to_context m ON m.target_id = cg.id "
                "WHERE m.event_id = %s AND m.target_kind = 'CG '",
                (event_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_cg_children(self, cg_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM context_groups WHERE parent_id = %s "
                "ORDER BY order_index ASC NULLS LAST",
                (cg_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_cg_hierarchy(self, cg_id: str) -> dict[str, Any]:
        children = self.get_cg_children(cg_id)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE chain(id, depth) AS (
                    SELECT id, 0 FROM context_groups WHERE id = %s
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
            ancestors = [dict(r) for r in cur.fetchall()]
        return {"children": children, "ancestors": ancestors}

    def list_cg_by_stage(self, stage: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM context_groups WHERE stage = %s "
                "ORDER BY member_count_total DESC NULLS LAST",
                (stage,),
            )
            return [dict(r) for r in cur.fetchall()]

    def count_cg_by_stage(self) -> dict[str, int]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT stage, COUNT(*) AS cnt FROM context_groups GROUP BY stage"
            )
            return {row["stage"]: int(row["cnt"]) for row in cur.fetchall()}

    def list_control_nodes(
        self, domain: str, include_seed: bool = False,
    ) -> list[dict[str, Any]]:
        stage_filter = "" if include_seed else " AND stage != 'seed'"
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM context_groups "
                f"WHERE training_method = 'structural_summary' AND domain = %s"
                f"{stage_filter} "
                f"ORDER BY order_index ASC NULLS LAST, created_at ASC",
                (domain,),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_cg_plan_attrs(
        self,
        cg_id: str,
        plan_stage: str,
        plan_id: str,
        expected_entities_json: str,
        abandon_reason: str = "",
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET plan_stage = %s, plan_id = %s, "
                "expected_entities = %s::jsonb, abandon_reason = %s WHERE id = %s",
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
        with self._conn.cursor() as cur:
            if abandon_reason is not None:
                cur.execute(
                    "UPDATE context_groups SET plan_stage = %s, abandon_reason = %s "
                    "WHERE id = %s",
                    (plan_stage, abandon_reason, cg_id),
                )
            else:
                cur.execute(
                    "UPDATE context_groups SET plan_stage = %s WHERE id = %s",
                    (plan_stage, cg_id),
                )
            if realized_event_id:
                cur.execute(
                    "INSERT INTO realized_as (from_id, from_kind, event_id) "
                    "VALUES (%s, 'CG ', %s) ON CONFLICT DO NOTHING",
                    (cg_id, realized_event_id),
                )
        self._conn.commit()

    def list_plan_seed_cgs(self, domain: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM context_groups "
                "WHERE cluster_source = 'plan' AND order_index = 0 AND domain = %s "
                "ORDER BY created_at ASC",
                (domain,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_plan_chain(self, start_cg_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE chain(id, depth) AS (
                    SELECT %s::varchar AS id, 0 AS depth
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
            return [dict(r) for r in cur.fetchall()]

    def list_abandoned_plans(
        self, domain: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM context_groups "
                "WHERE cluster_source = 'plan' AND plan_stage = 'abandoned' "
                "  AND domain = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (domain, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_cg_template_attrs(
        self,
        cg_id: str,
        template_id: str,
        transition_pattern_json: str,
        source_cg_ids_json: str,
        instance_count: int,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE context_groups SET template_id = %s, "
                "transition_pattern = %s::jsonb, source_cg_ids = %s::jsonb, "
                "instance_count = %s WHERE id = %s",
                (
                    template_id, transition_pattern_json, source_cg_ids_json,
                    int(instance_count), cg_id,
                ),
            )
        self._conn.commit()

    def list_cg_templates(self, domain: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM context_groups "
                "WHERE cluster_source = 'template' AND domain = %s "
                "ORDER BY instance_count DESC NULLS LAST",
                (domain,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # (f) Tier 2b — ETG / PlanNode / PlanDirectionNode
    # ------------------------------------------------------------------

    _ETG_COLUMNS: tuple[str, ...] = (
        "id", "name", "domain", "transition_pattern", "structure_description",
        "entity_list", "confidence", "instance_count",
    )
    _ETG_JSONB_COLUMNS: frozenset[str] = frozenset(("transition_pattern", "entity_list"))

    _PLAN_NODE_COLUMNS: tuple[str, ...] = (
        "id", "plan_id", "etg_id", "domain", "name", "stage",
        "objective_summary", "structure_description", "entity_ids",
        "entity_summary", "order_index", "realized_event_id",
        "parent_id",  # plan tree: NULL = root
        "abandon_reason",
    )
    _PLAN_NODE_JSONB_COLUMNS: frozenset[str] = frozenset(("entity_ids",))

    _DIR_NODE_COLUMNS: tuple[str, ...] = (
        "id", "plan_id", "domain", "goal_text", "stage", "entity_ids",
        "entity_summary", "order_index", "realized_event_id", "abandon_reason",
    )
    _DIR_NODE_JSONB_COLUMNS: frozenset[str] = frozenset(("entity_ids",))

    @staticmethod
    def _build_insert_with_jsonb(
        table: str,
        columns: tuple[str, ...],
        jsonb_cols: frozenset[str],
        values_dict: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        """Build a dynamic INSERT SQL string and params list for mixed JSONB columns."""
        cols = [c for c in columns if c in values_dict]
        placeholders: list[str] = []
        params: list[Any] = []
        for c in cols:
            v = values_dict[c]
            if c in jsonb_cols:
                placeholders.append("%s::jsonb")
                if isinstance(v, (dict, list)):
                    params.append(__import__("json").dumps(v))
                else:
                    params.append(v)  # assumed to be a JSON string already
            else:
                placeholders.append("%s")
                params.append(v)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)})"
        )
        return sql, params

    def create_etg(self, etg: dict[str, Any]) -> str:
        sql, params = self._build_insert_with_jsonb(
            "event_template_groups", self._ETG_COLUMNS, self._ETG_JSONB_COLUMNS, etg,
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()
        return str(etg["id"])

    def get_etg(self, etg_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM event_template_groups WHERE id = %s", (etg_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def list_etgs(self, domain: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM event_template_groups WHERE domain = %s "
                "ORDER BY confidence DESC NULLS LAST, created_at DESC",
                (domain,),
            )
            return [dict(r) for r in cur.fetchall()]

    def link_event_belongs_to_etg(
        self, event_id: str, etg_id: str, order: int = 0,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO event_belongs_to_context "
                "(event_id, target_id, target_kind, order_index) "
                "VALUES (%s, %s, 'ETG', %s) "
                "ON CONFLICT (event_id, target_id, target_kind) "
                "DO UPDATE SET order_index = EXCLUDED.order_index",
                (event_id, etg_id, int(order)),
            )
        self._conn.commit()

    def get_etg_events(self, etg_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT e.* FROM events e "
                "JOIN event_belongs_to_context m ON m.event_id = e.id "
                "WHERE m.target_id = %s AND m.target_kind = 'ETG' "
                "ORDER BY m.order_index ASC NULLS LAST, "
                "         e.order_index ASC NULLS LAST",
                (etg_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def create_plan_node(self, pn: dict[str, Any]) -> str:
        sql, params = self._build_insert_with_jsonb(
            "plan_nodes", self._PLAN_NODE_COLUMNS, self._PLAN_NODE_JSONB_COLUMNS, pn,
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()
        return str(pn["id"])

    def get_plan_node(self, plan_node_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM plan_nodes WHERE id = %s", (plan_node_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_plan_chain_v2(self, plan_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM plan_nodes WHERE plan_id = %s "
                "ORDER BY order_index ASC NULLS LAST",
                (plan_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def update_plan_node_stage(
        self,
        plan_node_id: str,
        stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        sets = ["stage = %s"]
        params: list[Any] = [stage]
        if realized_event_id is not None:
            sets.append("realized_event_id = %s")
            params.append(realized_event_id)
        if abandon_reason is not None:
            sets.append("abandon_reason = %s")
            params.append(abandon_reason)
        params.append(plan_node_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE plan_nodes SET {', '.join(sets)} WHERE id = %s",
                params,
            )
            if realized_event_id:
                cur.execute(
                    "INSERT INTO realized_as (from_id, from_kind, event_id) "
                    "VALUES (%s, 'PLN', %s) ON CONFLICT DO NOTHING",
                    (plan_node_id, realized_event_id),
                )
        self._conn.commit()

    def link_plan_from_etg(self, event_id: str, plan_node_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_from "
                "(event_id, target_id, target_kind, source_type) "
                "VALUES (%s, %s, 'PLN', 'etg_event') "
                "ON CONFLICT (event_id, target_id, target_kind) "
                "DO UPDATE SET source_type = EXCLUDED.source_type",
                (event_id, plan_node_id),
            )
        self._conn.commit()

    def link_plan_next_plan(
        self, from_plan_id: str, to_plan_id: str, order: int = 0,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_next "
                "(from_id, from_kind, to_id, to_kind, order_idx) "
                "VALUES (%s, 'PLN', %s, 'PLN', %s) "
                "ON CONFLICT (from_id, from_kind, to_id, to_kind) "
                "DO UPDATE SET order_idx = EXCLUDED.order_idx",
                (from_plan_id, to_plan_id, int(order)),
            )
        self._conn.commit()

    def link_realized_as_plan(self, plan_node_id: str, event_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO realized_as (from_id, from_kind, event_id) "
                "VALUES (%s, 'PLN', %s) ON CONFLICT DO NOTHING",
                (plan_node_id, event_id),
            )
        self._conn.commit()

    def list_active_plans(
        self, domain: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT plan_id, MIN(created_at) AS first_created, "
                "       COUNT(*) AS node_count "
                "  FROM plan_nodes "
                " WHERE domain = %s AND stage IN ('draft','planned','active') "
                " GROUP BY plan_id "
                " ORDER BY first_created DESC LIMIT %s",
                (domain, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def create_direction_node(self, dn: dict[str, Any]) -> str:
        sql, params = self._build_insert_with_jsonb(
            "plan_direction_nodes", self._DIR_NODE_COLUMNS,
            self._DIR_NODE_JSONB_COLUMNS, dn,
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()
        return str(dn["id"])

    def get_direction_node(self, dir_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM plan_direction_nodes WHERE id = %s", (dir_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_direction_chain(self, plan_id: str) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM plan_direction_nodes WHERE plan_id = %s "
                "ORDER BY order_index ASC NULLS LAST",
                (plan_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def update_direction_stage(
        self,
        dir_id: str,
        stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        sets = ["stage = %s"]
        params: list[Any] = [stage]
        if realized_event_id is not None:
            sets.append("realized_event_id = %s")
            params.append(realized_event_id)
        if abandon_reason is not None:
            sets.append("abandon_reason = %s")
            params.append(abandon_reason)
        params.append(dir_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE plan_direction_nodes SET {', '.join(sets)} WHERE id = %s",
                params,
            )
            if realized_event_id:
                cur.execute(
                    "INSERT INTO realized_as (from_id, from_kind, event_id) "
                    "VALUES (%s, 'DIR', %s) ON CONFLICT DO NOTHING",
                    (dir_id, realized_event_id),
                )
        self._conn.commit()

    def link_plan_next_direction(
        self, from_dir_id: str, to_dir_id: str, order: int = 0,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plan_next "
                "(from_id, from_kind, to_id, to_kind, order_idx) "
                "VALUES (%s, 'DIR', %s, 'DIR', %s) "
                "ON CONFLICT (from_id, from_kind, to_id, to_kind) "
                "DO UPDATE SET order_idx = EXCLUDED.order_idx",
                (from_dir_id, to_dir_id, int(order)),
            )
        self._conn.commit()

    def link_realized_as_direction(self, dir_id: str, event_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO realized_as (from_id, from_kind, event_id) "
                "VALUES (%s, 'DIR', %s) ON CONFLICT DO NOTHING",
                (dir_id, event_id),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # (g) Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except psycopg2.Error as exc:
            logger.exception("db_connection_lost", extra={
                "backend": "postgres", "operation": "ping",
                "err": str(exc)[:200],
            })
            return False

    def close(self) -> None:
        if hasattr(self, "_conn") and self._conn and not self._conn.closed:
            try:
                self._conn.close()
                logger.info("PostgresGraphStore connection closed")
            except psycopg2.Error as exc:
                logger.warning("PostgresGraphStore close error: %s", exc)
