"""DDL definitions for SqliteGraphStore + SqliteVectorStore.

DDL constants are kept in this file (separated from graph.py).
One schema.py per backend (sqlite / postgres) — table and column names
are identical across backends; only the SQL dialect differs.

SQL dialect mapping (Postgres → SQLite):
    VARCHAR(n)   → TEXT
    TIMESTAMPTZ  → TEXT (ISO8601, DEFAULT CURRENT_TIMESTAMP)
    vector(N)    → separate vec0 virtual table (events_vec, entities_vec)
    BOOLEAN      → INTEGER (0/1)
    JSONB        → TEXT (built-in JSON1)
    CHAR(3)      → TEXT
    NOW()        → CURRENT_TIMESTAMP
    BIGINT       → INTEGER
    HNSW index   → not needed (vec0 has built-in KNN)

When making changes, update ````,
````, and ``postgres/schema.py`` in sync.
"""
from __future__ import annotations

# ============================================================================
# DDL — schema mapping for SQLite.
# ============================================================================

# --- Tier 1 Nodes ---------------------------------------------------------

_CREATE_TIER1_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS entities (
        id          TEXT    PRIMARY KEY,
        name        TEXT    NOT NULL,
        domain      TEXT    NOT NULL,
        type        TEXT,
        user_tag    TEXT,
        embedding   BLOB,                 -- inline embedding (sqlite-vec serialize_float32). Replaces legacy entities_vec vec0.
        deprecated  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (name, domain)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id              TEXT    PRIMARY KEY,
        domain          TEXT    NOT NULL,
        summary         TEXT,
        vector_id       TEXT,
        embedding       BLOB,              -- inline embedding (sqlite-vec serialize_float32). Replaces legacy events_vec vec0.
        timestamp       TEXT,
        order_index     INTEGER,
        deprecated      INTEGER NOT NULL DEFAULT 0,
        influence_score REAL    NOT NULL DEFAULT 1.0,
        ner_method      TEXT,             -- 'regex_only' | 'llm_augmented' | 'split' | 'skipped'
        ner_skip_reason TEXT,              -- skip reason (e.g. log_len_below_threshold:N)
        created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        id             TEXT    PRIMARY KEY,
        name           TEXT    NOT NULL UNIQUE,
        level          INTEGER,
        domain         TEXT    NOT NULL,
        parent_id      TEXT    REFERENCES groups(id),
        discriminator  TEXT,
        original_name  TEXT,
        source         TEXT,
        user_tag       TEXT,
        summary        TEXT,
        deprecated     INTEGER NOT NULL DEFAULT 0,
        -- RLS parity: owner_id (UUID TEXT) and workspace_id cross schema
        -- boundaries so there is no FK in core — cloud_layer ensures nullable parity.
        owner_id       TEXT,
        workspace_id   TEXT,
        visibility     TEXT    NOT NULL DEFAULT 'public',
        share_group_id TEXT    REFERENCES k2g_share_group(id) ON DELETE SET NULL,
        acl_json       TEXT,
        created_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]

# --- Tier 1 Edges ---------------------------------------------------------

_CREATE_TIER1_EDGE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS participated_in (
        entity_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        event_id   TEXT NOT NULL REFERENCES events(id)   ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (entity_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_connection (
        a_id        TEXT    NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        b_id        TEXT    NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        event_count INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (a_id, b_id),
        CHECK (a_id < b_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_member_of (
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        kind     TEXT NOT NULL DEFAULT 'contains',
        PRIMARY KEY (event_id, group_id),
        CHECK (kind IN ('contains','refers'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_sequential_next (
        prev_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        next_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        source     TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (prev_id, next_id, source),
        CHECK (source IN ('chunk_order','file_name','folder_name',
                          'thread','topic_segment','user_manual','version up'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_jaccard_connected (
        a_id                TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        b_id                TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        entity_jaccard      REAL,
        group_jaccard       REAL,
        entity_intersection INTEGER,
        group_intersection  INTEGER,
        created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (a_id, b_id),
        CHECK (a_id < b_id)
    );
    """,
    # Entity embedding metadata — entities_vec (vec0) stores only id + embedding,
    # so computed_at / method / ref_event_count / domain are kept in this table.
    # Only this row is UPSERTed on recomputation; CASCADE handles entity deletion.
    # When new ingestion changes an entity's participated_in rows, centroid
    # recomputation is required — ProjectionEngine detects staleness by comparing
    # ref_event_count.
    """
    CREATE TABLE IF NOT EXISTS entity_embedding_meta (
        entity_id        TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
        computed_at      TEXT NOT NULL,
        method           TEXT NOT NULL,
        ref_event_count  INTEGER NOT NULL DEFAULT 0,
        domain           TEXT,
        entity_name      TEXT,
        updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        coherence        REAL                     -- mean resultant length R in [0,1]
    );
    """,
]

# --- Tier 2a: ContextGroup (BP-28) ---------------------------------------

_CREATE_TIER2A_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS context_groups (
        id                      TEXT    PRIMARY KEY,
        name                    TEXT    NOT NULL,
        stage                   TEXT    NOT NULL,
        cluster_source          TEXT    NOT NULL,
        training_method         TEXT,
        confidence              REAL,
        member_count_own        INTEGER DEFAULT 0,
        member_count_total      INTEGER DEFAULT 0,
        depth                   INTEGER DEFAULT 0,
        version                 INTEGER DEFAULT 1,
        narrative_summary       TEXT,
        order_index             INTEGER,
        domain                  TEXT    NOT NULL,
        parent_id               TEXT    REFERENCES context_groups(id),
        plan_stage              TEXT,
        plan_id                 TEXT,
        expected_entities       TEXT,
        abandon_reason          TEXT,
        template_id             TEXT,
        transition_pattern      TEXT,
        source_cg_ids           TEXT,
        instance_count          INTEGER,
        valid_from              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_belongs_to_context (
        event_id    TEXT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        target_id   TEXT    NOT NULL,
        target_kind TEXT    NOT NULL,
        kind        TEXT,
        order_index INTEGER,
        PRIMARY KEY (event_id, target_id, target_kind),
        CHECK (target_kind IN ('CG ','ETG'))
    );
    """,
]

# --- Tier 2b: ETG / PlanNode / PlanDirectionNode (BP-30) ------------------

_CREATE_TIER2B_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS event_template_groups (
        id                    TEXT    PRIMARY KEY,
        name                  TEXT    NOT NULL,
        domain                TEXT    NOT NULL,
        transition_pattern    TEXT,
        structure_description TEXT,
        entity_list           TEXT,
        confidence            REAL,
        instance_count        INTEGER,
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_nodes (
        id                    TEXT    PRIMARY KEY,
        plan_id               TEXT    NOT NULL,
        etg_id                TEXT    REFERENCES event_template_groups(id),
        domain                TEXT    NOT NULL,
        name                  TEXT,
        stage                 TEXT    NOT NULL,
        objective_summary     TEXT,
        structure_description TEXT,
        entity_ids            TEXT,
        entity_summary        TEXT,
        order_index           INTEGER,
        realized_event_id     TEXT    REFERENCES events(id),
        -- v2: major premise → minor premise tree. NULL = root.
        -- SQLite ON DELETE SET NULL is deferred when PRAGMA foreign_keys=ON.
        parent_id             TEXT    REFERENCES plan_nodes(id) ON DELETE SET NULL,
        abandon_reason        TEXT,
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_direction_nodes (
        id                TEXT    PRIMARY KEY,
        plan_id           TEXT    NOT NULL,
        domain            TEXT    NOT NULL,
        goal_text         TEXT,
        stage             TEXT    NOT NULL,
        entity_ids        TEXT,
        entity_summary    TEXT,
        order_index       INTEGER,
        realized_event_id TEXT    REFERENCES events(id),
        abandon_reason    TEXT,
        created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_from (
        event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        target_id   TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        source_type TEXT,
        PRIMARY KEY (event_id, target_id, target_kind),
        CHECK (target_kind IN ('CG ','PLN'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_next (
        from_id    TEXT    NOT NULL,
        from_kind  TEXT    NOT NULL,
        to_id      TEXT    NOT NULL,
        to_kind    TEXT    NOT NULL,
        order_idx  INTEGER DEFAULT 0,
        PRIMARY KEY (from_id, from_kind, to_id, to_kind),
        CHECK (from_kind IN ('CG ','PLN','DIR')),
        CHECK (to_kind   IN ('CG ','PLN','DIR'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS realized_as (
        from_id   TEXT NOT NULL,
        from_kind TEXT NOT NULL,
        event_id  TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        PRIMARY KEY (from_id, from_kind, event_id),
        CHECK (from_kind IN ('CG ','PLN','DIR'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_archive_ref (
        id         TEXT    PRIMARY KEY,
        event_id   TEXT    REFERENCES events(id),
        uri        TEXT    NOT NULL,
        sha256     TEXT,
        size_bytes INTEGER,
        created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]

# --- Audit ----------------------------------------------------------------
# events_audit: accumulates score_before / score_after / reason on each
# set_influence call. Only explicit user/LLM calls are recorded here;
# K2G does not write to this table automatically.

_CREATE_AUDIT_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS events_audit (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id      TEXT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        score_before  REAL    NOT NULL,
        score_after   REAL    NOT NULL,
        reason        TEXT,
        set_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]

# --- Build Audit ----------------------------------------------------------
# build_audit: records every LLM call from PoolManager (success / fallback /
# censor bypass / tier_exhausted) as one row per call. Source of evidence for
# the cost dashboard and retry tracking. Only the call fact + result are
# accumulated — K2G does not attach a verdict.

_CREATE_BUILD_AUDIT_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS build_audit (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        domain          TEXT    NOT NULL,
        persona         TEXT    NOT NULL,
        tier_provider   TEXT,
        tier_model      TEXT,
        event_id        TEXT,                       -- related event (NULL = file/cluster/global)
        target_kind     TEXT,                       -- "event" | "file" | "cluster" | "entity"
        target_id       TEXT,
        attempt         INTEGER NOT NULL DEFAULT 1, -- position in the fallback chain
        error_class     TEXT,                       -- NULL = success
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cost_usd        REAL,
        latency_ms      INTEGER,
        reason          TEXT,                       -- "ok" | "censor_fallback" | "rate_limit" | etc.
        build_id        TEXT,
        created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]

# --- Embeddings: inline BLOB columns (sqlite-vec) -------------------------
# events.embedding / entities.embedding stored as BLOB (sqlite-vec
# serialize_float32 bytes), unifying architecture with PG's vector(N) column.
# Search uses vec_distance_cosine(embedding, ?) scalar + inline WHERE domain
# filter (sqlite/vector.py). The legacy events_vec / entities_vec vec0 virtual
# tables are deprecated — migrate existing databases once with
# `python -m k2g.db_store.sqlite.migrate_inline_embeddings` then DROP them.
# HNSW indexes are Postgres-only; SQLite uses brute-force scan (acceptable
# at local scale).

# --- Indexes (B-tree only; HNSW is built into vec0) ----------------------

_CREATE_INDEXES_SQL = [
    # entities
    "CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain);",
    "CREATE INDEX IF NOT EXISTS idx_entities_name_lower ON entities(LOWER(name));",
    # events
    "CREATE INDEX IF NOT EXISTS idx_events_domain ON events(domain);",
    "CREATE INDEX IF NOT EXISTS idx_events_domain_order ON events(domain, order_index);",
    "CREATE INDEX IF NOT EXISTS idx_events_vector_id ON events(vector_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_influence ON events(domain, influence_score DESC);",
    # groups
    "CREATE INDEX IF NOT EXISTS idx_groups_domain_name ON groups(domain, name);",
    "CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_groups_discriminator ON groups(discriminator);",
    # event edges
    "CREATE INDEX IF NOT EXISTS idx_participated_in_event ON participated_in(event_id);",
    "CREATE INDEX IF NOT EXISTS idx_event_member_of_group ON event_member_of(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_event_sequential_next_next ON event_sequential_next(next_id);",
    # event_jaccard_connected can grow to ~1M unfiltered rows (store-all mode);
    # index entity_jaccard for read-time theta_e filtering and b_id for the
    # ego-UNION arm.
    "CREATE INDEX IF NOT EXISTS idx_ejc_entity_jaccard ON event_jaccard_connected(entity_jaccard);",
    "CREATE INDEX IF NOT EXISTS idx_ejc_b_id ON event_jaccard_connected(b_id);",
    # entity_embedding_meta — domain filter for search_similar_entities
    "CREATE INDEX IF NOT EXISTS idx_entity_embedding_meta_domain ON entity_embedding_meta(domain);",
]

_CREATE_TIER2A_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_cg_domain_stage ON context_groups(domain, stage);",
    "CREATE INDEX IF NOT EXISTS idx_cg_parent ON context_groups(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_cg_cluster_source ON context_groups(cluster_source);",
    "CREATE INDEX IF NOT EXISTS idx_cg_training_method ON context_groups(training_method);",
    "CREATE INDEX IF NOT EXISTS idx_ebtc_target ON event_belongs_to_context(target_id, target_kind);",
]

_CREATE_TIER2B_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_etg_domain ON event_template_groups(domain);",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_plan_id ON plan_nodes(plan_id);",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_stage ON plan_nodes(stage);",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_domain ON plan_nodes(domain);",
    "CREATE INDEX IF NOT EXISTS idx_direction_nodes_plan_id ON plan_direction_nodes(plan_id);",
    "CREATE INDEX IF NOT EXISTS idx_direction_nodes_stage ON plan_direction_nodes(stage);",
    "CREATE INDEX IF NOT EXISTS idx_plan_next_to ON plan_next(to_id, to_kind);",
    "CREATE INDEX IF NOT EXISTS idx_realized_as_event ON realized_as(event_id);",
]

_CREATE_AUDIT_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_events_audit_event_id ON events_audit(event_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_audit_set_at ON events_audit(set_at);",
]

_CREATE_BUILD_AUDIT_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_build_audit_domain_created "
    "ON build_audit(domain, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_build_audit_persona "
    "ON build_audit(persona, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_build_audit_event "
    "ON build_audit(event_id);",
    "CREATE INDEX IF NOT EXISTS idx_build_audit_build_id "
    "ON build_audit(build_id);",
    "CREATE INDEX IF NOT EXISTS idx_build_audit_error "
    "ON build_audit(error_class) WHERE error_class IS NOT NULL;",
]

# --- Build Failure Manifest --------------------------------------------
# build_failure_manifest: tracks per-unit failures in the TRAIN/LOAD/PRODUCE
# stages on a separate track. Distinct from events / state_manifest — records
# the fact that an ingest attempt failed. The retry CLI targets rows where
# status='pending'.

_CREATE_FAILURE_MANIFEST_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS build_failure_manifest (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        domain          TEXT    NOT NULL,
        stage           TEXT    NOT NULL,    -- "extract" | "produce" | "load" | "train"
        persona         TEXT,
        target_kind     TEXT    NOT NULL,    -- "file" | "segment" | "cluster" | "entity"
        target_id       TEXT    NOT NULL,
        file_path       TEXT,
        error_class     TEXT    NOT NULL,
        error_detail    TEXT,
        last_tier       TEXT,
        attempt_count   INTEGER NOT NULL DEFAULT 1,
        build_id        TEXT    NOT NULL,
        failed_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status          TEXT    NOT NULL DEFAULT 'pending',
        UNIQUE(domain, stage, target_kind, target_id, build_id)
    );
    """,
]

_CREATE_FAILURE_MANIFEST_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_failure_pending "
    "ON build_failure_manifest(domain, status) WHERE status = 'pending';",
    "CREATE INDEX IF NOT EXISTS idx_failure_class "
    "ON build_failure_manifest(error_class);",
    "CREATE INDEX IF NOT EXISTS idx_failure_target "
    "ON build_failure_manifest(domain, target_kind, target_id);",
]

# --- Train State Manifest + Cluster Narrative Cache --------------------
# train_state_manifest: tracks per-unit state across the 4 TRAIN phases
# (jaccard / hdbscan / controlnode / entity_vector) keyed by
# (domain, target_kind, target_id, phase) with states:
# trained / pending / stale / abandoned + signature.
# All mutations go through db.session() with CAS (version counter) protection.
#
# cluster_narrative_cache: separate track for ControlNode LLM output.
# Only the non-deterministic phase needs caching; the other 3 phases are
# deterministic so state_manifest alone is sufficient.

_CREATE_TRAIN_STATE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS train_state_manifest (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        domain            TEXT    NOT NULL,
        target_kind       TEXT    NOT NULL,    -- "event"|"entity"|"cluster"|"edge"
        target_id         TEXT    NOT NULL,
        phase             TEXT    NOT NULL,    -- "jaccard"|"hdbscan"|"controlnode"|"entity_vector"
        state             TEXT    NOT NULL,    -- "pending"|"in_progress"|"trained"|"stale"|"abandoned"
        version           INTEGER NOT NULL DEFAULT 1,
        claim_owner       TEXT,
        claim_expires_at  TEXT,
        signature         TEXT,
        last_input_hash   TEXT,
        last_trained_at   TEXT,
        last_phase_run    TEXT,
        upstream_deps     TEXT,
        error_count       INTEGER NOT NULL DEFAULT 0,
        last_error        TEXT,
        algorithm_version TEXT,
        created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(domain, target_kind, target_id, phase)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_narrative_cache (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        domain                   TEXT    NOT NULL,
        cluster_signature        TEXT    NOT NULL,
        narrative                TEXT    NOT NULL,
        member_count             INTEGER,
        model_version            TEXT    NOT NULL,
        narrative_format_version INTEGER NOT NULL DEFAULT 1,
        created_at               TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        used_count               INTEGER NOT NULL DEFAULT 1,
        UNIQUE(domain, cluster_signature, model_version, narrative_format_version)
    );
    """,
]

_CREATE_TRAIN_STATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_state_pending "
    "ON train_state_manifest(domain, phase, state) "
    "WHERE state IN ('pending', 'stale');",
    "CREATE INDEX IF NOT EXISTS idx_state_target "
    "ON train_state_manifest(domain, target_kind, target_id);",
    "CREATE INDEX IF NOT EXISTS idx_state_phase "
    "ON train_state_manifest(domain, phase, last_trained_at);",
    "CREATE INDEX IF NOT EXISTS idx_state_claim_expires "
    "ON train_state_manifest(claim_expires_at) "
    "WHERE state = 'in_progress';",
    "CREATE INDEX IF NOT EXISTS idx_cluster_narrative_sig "
    "ON cluster_narrative_cache(domain, cluster_signature);",
]

# --- Train Run Manifest (global batch audit) ----------------------------
# train_run: one row per global batch training run (Louvain, future KGE/ETG,
#   etc.). Sibling relationship with train_state_manifest's per-target
#   staleness — the two tables have separate concerns. Future global training
#   kinds are absorbed into the same schema (kind TEXT is free-form).
# entity_community_assignment: per-run entity → community_id results.
#   Historical records are preserved per run_id for community drift tracking.

_CREATE_TRAIN_RUN_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS train_run (
        id              TEXT    PRIMARY KEY,
        kind            TEXT    NOT NULL,
        domain          TEXT,
        started_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at     TEXT,
        status          TEXT    NOT NULL DEFAULT 'running',
        event_count     INTEGER,
        entity_count    INTEGER,
        edge_count      INTEGER,
        params_json     TEXT,
        metrics_json    TEXT,
        rows_written    INTEGER,
        error_text      TEXT,
        notes           TEXT,
        triggered_by    TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_community_assignment (
        run_id        TEXT NOT NULL REFERENCES train_run(id) ON DELETE CASCADE,
        entity_id     TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        community_id  INTEGER NOT NULL,
        PRIMARY KEY (run_id, entity_id)
    );
    """,
    # BP-90: event community (leidenalg on event_jaccard_connected). Mirror of
    # entity_community_assignment, run_id distinguishes entity vs event via kind.
    """
    CREATE TABLE IF NOT EXISTS event_community_assignment (
        run_id        TEXT NOT NULL REFERENCES train_run(id) ON DELETE CASCADE,
        event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        community_id  INTEGER NOT NULL,
        PRIMARY KEY (run_id, event_id)
    );
    """,
]

_CREATE_TRAIN_RUN_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_train_run_kind_finished "
    "ON train_run(kind, finished_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_eca_run_community "
    "ON entity_community_assignment(run_id, community_id);",
    "CREATE INDEX IF NOT EXISTS idx_eca_entity "
    "ON entity_community_assignment(entity_id);",
    "CREATE INDEX IF NOT EXISTS idx_evca_run_community "
    "ON event_community_assignment(run_id, community_id);",
    "CREATE INDEX IF NOT EXISTS idx_evca_event "
    "ON event_community_assignment(event_id);",
]

# --- Covenant Metadata (integrated into main DB) -----------------------
# k2g_covenant: domain × source registry (filesystem/vcs/database/email/rss).
#   config_json is free-form per source type, e.g.:
#   filesystem={'root','include','exclude'}, vcs={'repo','branch','since'},
#   email={'imap_url','folder'}, rss={'feed_url'}.
# k2g_covenant_history: chronological add/remove/enable/disable/modify audit.
# k2g_source_file: per-file/message/article ingestion metadata
#   (covenant_id FK + relative_path + content_hash + extractor_key +
#   unit_strategy + last_ingested).
#
# Same schema as the legacy covenant.db (separate SQLite file). After
# integration, scripts/migrate_covenant_db.py performs a one-time migration.

_CREATE_COVENANT_META_TABLES_SQL = [
    """
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
    """,
    """
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
    """,
    """
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
    """,
]

_CREATE_COVENANT_META_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_covenant_domain "
    "ON k2g_covenant (domain);",
    "CREATE INDEX IF NOT EXISTS idx_covenant_enabled "
    "ON k2g_covenant (enabled);",
    "CREATE INDEX IF NOT EXISTS idx_covenant_history_domain "
    "ON k2g_covenant_history (domain);",
    "CREATE INDEX IF NOT EXISTS idx_sf_covenant "
    "ON k2g_source_file (covenant_id);",
]

# --- Share Group + Member + Audit --------------------------------------
# k2g_share_group: unit of sharing within a domain (e.g. "Team Alpha").
# k2g_share_member: members of a share_group (user / org / public).
#   Adding/removing a member is 1 INSERT/DELETE that affects N data rows
#   without modifying those rows directly.
# k2g_share_audit: change audit (same pattern as covenant_history).

_CREATE_SHARE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_share_group (
        id           TEXT    PRIMARY KEY,
        domain       TEXT    NOT NULL,
        name         TEXT    NOT NULL,
        description  TEXT,
        owner_id     TEXT,
        created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_share_member (
        share_group_id TEXT NOT NULL REFERENCES k2g_share_group(id) ON DELETE CASCADE,
        member_kind    TEXT NOT NULL,
        member_id      TEXT NOT NULL,
        role           TEXT NOT NULL DEFAULT 'reader',
        added_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (share_group_id, member_kind, member_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_share_audit (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        action         TEXT    NOT NULL,
        share_group_id TEXT,
        member_kind    TEXT,
        member_id      TEXT,
        actor_id       TEXT,
        before_json    TEXT,
        after_json     TEXT,
        acted_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]

_CREATE_SHARE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_share_group_domain "
    "ON k2g_share_group (domain);",
    "CREATE INDEX IF NOT EXISTS idx_share_member_user "
    "ON k2g_share_member (member_kind, member_id) "
    "WHERE member_kind = 'user';",
    "CREATE INDEX IF NOT EXISTS idx_share_member_org "
    "ON k2g_share_member (member_kind, member_id) "
    "WHERE member_kind = 'org';",
    "CREATE INDEX IF NOT EXISTS idx_share_audit_group "
    "ON k2g_share_audit (share_group_id, acted_at);",
]

# --- Cross-domain primitives (alias / reference / domain_share) --------
# k2g_entity_alias: declares that an entity in domain A and an entity in
#   domain B refer to the same person/place (or aspect/translation).
#   Permission-agnostic — pure graph representation.
# k2g_event_reference: cross-domain event references
#   (same_event / derived_from / mentions / contradicts).
# k2g_domain_share: explicit permission grant giving a target user/org
#   access to part of a source domain. Checked in RLS policy branch (2).

_CREATE_CROSS_DOMAIN_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_entity_alias (
        entity_id_a   TEXT NOT NULL,
        entity_id_b   TEXT NOT NULL,
        relation      TEXT NOT NULL DEFAULT 'same',
        confidence    REAL DEFAULT 1.0,
        asserted_by   TEXT,
        asserted_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (entity_id_a, entity_id_b)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_event_reference (
        event_id_a    TEXT NOT NULL,
        event_id_b    TEXT NOT NULL,
        kind          TEXT NOT NULL,
        confidence    REAL DEFAULT 1.0,
        asserted_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id_a, event_id_b, kind)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_domain_share (
        source_domain  TEXT NOT NULL,
        target_kind    TEXT NOT NULL,
        target_id      TEXT NOT NULL,
        scope_filter   TEXT,
        role           TEXT NOT NULL DEFAULT 'reader',
        granted_by     TEXT,
        granted_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at     TEXT,
        PRIMARY KEY (source_domain, target_kind, target_id)
    );
    """,
]

_CREATE_CROSS_DOMAIN_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_entity_alias_b "
    "ON k2g_entity_alias (entity_id_b);",
    "CREATE INDEX IF NOT EXISTS idx_event_reference_b "
    "ON k2g_event_reference (event_id_b);",
    "CREATE INDEX IF NOT EXISTS idx_domain_share_target "
    "ON k2g_domain_share (target_kind, target_id, source_domain);",
]


# --- Domain soft-registry -------------------------------------------------
# Lets a domain *exist* independent of data (so an empty domain can be created,
# and a domain can be deleted only while empty). NOT a FK target: `domain` stays
# a free-text tag on every table; this is a side list reconciled by union with
# the data-derived domains. See db_store.graph_backend domain-admin methods.

_CREATE_DOMAIN_REGISTRY_SQL = [
    """
    CREATE TABLE IF NOT EXISTS domain_registry (
        name        TEXT    PRIMARY KEY,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]


# --- build_run_summary ---------------------------------------------------
# One row per build run: INSERT on start (running), UPDATE on end
# (success/failed/partial). Single aggregated row for data spread across
# build_audit and build_failure_manifest.

_CREATE_BP51_RUN_SUMMARY_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS build_run_summary (
        build_id           TEXT PRIMARY KEY,
        session_id         TEXT NOT NULL,
        domain             TEXT NOT NULL,
        owner_id           TEXT,                              -- populated in Phase B; 'sub:dev' sentinel before
        started_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at           TEXT,
        status             TEXT NOT NULL DEFAULT 'running',  -- running|success|partial|failed
        stages_run         TEXT,                              -- JSON array e.g. '["produce","load","train"]'
        events_produced    INTEGER,
        entities_loaded    INTEGER,
        llm_calls          INTEGER,
        llm_cost_usd       REAL,
        llm_failures       INTEGER,
        source_purge_count INTEGER DEFAULT 0,
        error_summary      TEXT,
        config_snapshot    TEXT                               -- JSON
    );
    """,
]

_CREATE_BP51_RUN_SUMMARY_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_build_run_summary_owner_time "
    "ON build_run_summary (owner_id, started_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_build_run_summary_domain_time "
    "ON build_run_summary (domain, started_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_build_run_summary_status "
    "ON build_run_summary (status, started_at DESC);",
]

# --- storage_usage_daily --------------------------------------------------
# Daily per-domain × per-owner storage usage snapshot. Inserted nightly by
# cron. (domain, snapshot_date, owner_id) is UNIQUE to prevent duplicates.

_CREATE_BP51_STORAGE_USAGE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS storage_usage_daily (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date       TEXT NOT NULL,                -- YYYY-MM-DD
        domain              TEXT NOT NULL,
        owner_id            TEXT,                          -- NULL = whole domain
        events_count        INTEGER,
        events_bytes        INTEGER,                       -- approximate sum of summary char counts
        segment_blob_bytes  INTEGER,                       -- raw archive (source lineage)
        vector_count        INTEGER,
        total_bytes         INTEGER,
        created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (snapshot_date, domain, owner_id)
    );
    """,
]

_CREATE_BP51_STORAGE_USAGE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_storage_usage_owner_date "
    "ON storage_usage_daily (owner_id, snapshot_date DESC);",
    "CREATE INDEX IF NOT EXISTS idx_storage_usage_domain_date "
    "ON storage_usage_daily (domain, snapshot_date DESC);",
]


# --- Cost Reconciliation (L2) --------------------------------------------
# provider_balance_snapshot: periodic polling results from external provider
# billing APIs. metric_kind = 'balance' (decreasing) | 'cumulative_cost'
# (increasing).

_CREATE_BP51_BALANCE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS provider_balance_snapshot (
        snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at    TEXT NOT NULL,
        provider       TEXT NOT NULL,                    -- openai | anthropic | xai | deepseek | gemini
        metric_kind    TEXT NOT NULL,                    -- 'balance' | 'cumulative_cost'
        value_usd      REAL NOT NULL,
        currency       TEXT DEFAULT 'USD',
        fx_rate        REAL,
        raw_response   TEXT,                             -- JSON
        poll_status    TEXT NOT NULL,                    -- success | init_skipped | poll_error | unknown_error
        poll_error     TEXT,
        UNIQUE (provider, snapshot_at)
    );
    """,
]

_CREATE_BP51_BALANCE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_balance_snapshot_provider_time "
    "ON provider_balance_snapshot (provider, snapshot_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_balance_snapshot_status "
    "ON provider_balance_snapshot (poll_status, snapshot_at DESC) "
    "WHERE poll_status != 'success';",
]

# --- Cost Calibration (L3 lazy) ------------------------------------------
# provider_calibration: derives per-token-kind learned unit prices by
# regressing (Ridge) windowed deltas from balance/cost APIs against the
# call history in build_audit. Inserted by the lazy CLI at call time; no
# back-fill of build_audit is needed — the VIEW provides lookup access.

_CREATE_BP51_CALIBRATION_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS provider_calibration (
        provider              TEXT NOT NULL,
        model                 TEXT NOT NULL,
        token_kind            TEXT NOT NULL,             -- 'input'|'output'|'cache_hit'|'cache_miss'
        unit_price_usd_per_1m REAL NOT NULL,
        fitted_at             TEXT NOT NULL,
        window_start          TEXT NOT NULL,
        window_end            TEXT NOT NULL,
        window_count          INTEGER NOT NULL,
        r_squared             REAL,
        drift_vs_yaml_pct     REAL,
        fitted_method         TEXT NOT NULL,             -- 'ridge' | 'huber'
        PRIMARY KEY (provider, model, token_kind, fitted_at)
    );
    """,
]

_CREATE_BP51_CALIBRATION_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_calibration_lookup "
    "ON provider_calibration (provider, model, token_kind, fitted_at DESC);",
]

# --- k2g_sql_audit -------------------------------------------------------
# Already reserved in DATA_OWNER_EXCLUDED (data_owner.py). The k2g_sql_query
# MCP tool inserts a row immediately after each call for operational audit
# and SQL usage pattern tracking.

_CREATE_BP51_SQL_AUDIT_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_sql_audit (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        invoked_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor_id        TEXT,                            -- session GUC k2g.user_id or sub:dev
        domain          TEXT,
        query_sql       TEXT NOT NULL,                   -- final form after safety validation
        row_count       INTEGER,
        duration_ms     INTEGER,
        status          TEXT NOT NULL,                   -- success|rejected|error
        error_class     TEXT,
        error_detail    TEXT
    );
    """,
]

_CREATE_BP51_SQL_AUDIT_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_sql_audit_actor_time "
    "ON k2g_sql_audit (actor_id, invoked_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_sql_audit_status "
    "ON k2g_sql_audit (status, invoked_at DESC) "
    "WHERE status != 'success';",
]

# --- Views (drift + calibrated cost) ------------------------------------
# SQLite has no LATERAL, so correlated scalar subqueries are used instead.
# NULL propagation provides automatic fallback to ba.cost_usd (estimated)
# when no fitting data is available. CREATE VIEW IF NOT EXISTS is supported
# since SQLite 3.9+ (2015). DROP-then-CREATE is idempotent-safe.

_CREATE_BP51_VIEWS_SQL = [
    # v_owner_usage_monthly — per-user monthly aggregation
    "DROP VIEW IF EXISTS v_owner_usage_monthly;",
    """
    CREATE VIEW v_owner_usage_monthly AS
    SELECT
      COALESCE(owner_id, 'system') AS owner_id,
      STRFTIME('%Y-%m', created_at) AS month,
      COUNT(*) FILTER (WHERE error_class IS NULL) AS llm_success,
      COUNT(*) FILTER (WHERE error_class IS NOT NULL) AS llm_fail,
      COALESCE(SUM(cost_usd), 0.0) AS cost_usd_estimated,
      COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens
    FROM build_audit
    GROUP BY COALESCE(owner_id, 'system'), STRFTIME('%Y-%m', created_at);
    """,
    # v_provider_drift_hourly — L2 estimated vs. billed
    "DROP VIEW IF EXISTS v_provider_drift_hourly;",
    """
    CREATE VIEW v_provider_drift_hourly AS
    WITH snapshots AS (
      SELECT provider, snapshot_at, value_usd, metric_kind,
             LAG(value_usd) OVER (PARTITION BY provider ORDER BY snapshot_at) AS prev_value
      FROM provider_balance_snapshot
      WHERE poll_status = 'success'
    ),
    deltas AS (
      SELECT provider, snapshot_at,
             CASE metric_kind
               WHEN 'balance'         THEN prev_value - value_usd
               WHEN 'cumulative_cost' THEN value_usd - prev_value
             END AS billed_delta
      FROM snapshots WHERE prev_value IS NOT NULL
    ),
    local_estimates AS (
      SELECT tier_provider AS provider,
             STRFTIME('%Y-%m-%d %H:00:00', created_at) AS hour,
             SUM(cost_usd) AS local_cost
      FROM build_audit
      WHERE cost_usd IS NOT NULL
      GROUP BY tier_provider, STRFTIME('%Y-%m-%d %H:00:00', created_at)
    )
    SELECT d.provider,
           d.snapshot_at,
           d.billed_delta,
           l.local_cost AS estimated_delta,
           (d.billed_delta - COALESCE(l.local_cost, 0.0)) AS drift_usd,
           CASE WHEN l.local_cost IS NOT NULL AND l.local_cost > 0
                THEN (d.billed_delta - l.local_cost) / l.local_cost * 100.0
                ELSE NULL END AS drift_pct
      FROM deltas d
      LEFT JOIN local_estimates l
             ON l.provider = d.provider
            AND l.hour = STRFTIME('%Y-%m-%d %H:00:00', d.snapshot_at);
    """,
    # v_build_audit_calibrated — L3 lazy lookup (correlated subquery variant)
    "DROP VIEW IF EXISTS v_build_audit_calibrated;",
    """
    CREATE VIEW v_build_audit_calibrated AS
    SELECT
      ba.*,
      COALESCE(
          (COALESCE(ba.input_tokens, 0) / 1000000.0) * (
            SELECT pc.unit_price_usd_per_1m FROM provider_calibration pc
             WHERE pc.provider = ba.tier_provider AND pc.model = ba.tier_model
               AND pc.token_kind = 'input' AND pc.fitted_at <= ba.created_at
             ORDER BY pc.fitted_at DESC LIMIT 1
          )
        + (COALESCE(ba.output_tokens, 0) / 1000000.0) * (
            SELECT pc.unit_price_usd_per_1m FROM provider_calibration pc
             WHERE pc.provider = ba.tier_provider AND pc.model = ba.tier_model
               AND pc.token_kind = 'output' AND pc.fitted_at <= ba.created_at
             ORDER BY pc.fitted_at DESC LIMIT 1
          ),
          ba.cost_usd
      ) AS cost_usd_calibrated,
      (
        SELECT MAX(pc.fitted_at) FROM provider_calibration pc
         WHERE pc.provider = ba.tier_provider AND pc.model = ba.tier_model
           AND pc.token_kind IN ('input', 'output')
           AND pc.fitted_at <= ba.created_at
      ) AS calibrated_at,
      CASE WHEN (
        SELECT COUNT(DISTINCT pc.token_kind) FROM provider_calibration pc
         WHERE pc.provider = ba.tier_provider AND pc.model = ba.tier_model
           AND pc.token_kind IN ('input', 'output')
           AND pc.fitted_at <= ba.created_at
      ) = 2 THEN 'calibrated' ELSE 'estimated' END AS cost_source
    FROM build_audit ba;
    """,
]


__all__ = [
    "_CREATE_TIER1_TABLES_SQL",
    "_CREATE_TIER1_EDGE_TABLES_SQL",
    "_CREATE_TIER2A_TABLES_SQL",
    "_CREATE_TIER2B_TABLES_SQL",
    "_CREATE_AUDIT_TABLES_SQL",
    "_CREATE_BUILD_AUDIT_TABLES_SQL",
    "_CREATE_FAILURE_MANIFEST_TABLES_SQL",
    "_CREATE_TRAIN_STATE_TABLES_SQL",
    "_CREATE_TRAIN_RUN_TABLES_SQL",
    "_CREATE_COVENANT_META_TABLES_SQL",
    "_CREATE_SHARE_TABLES_SQL",
    "_CREATE_CROSS_DOMAIN_TABLES_SQL",
    "_CREATE_INDEXES_SQL",
    "_CREATE_TIER2A_INDEXES_SQL",
    "_CREATE_TIER2B_INDEXES_SQL",
    "_CREATE_AUDIT_INDEXES_SQL",
    "_CREATE_BUILD_AUDIT_INDEXES_SQL",
    "_CREATE_FAILURE_MANIFEST_INDEXES_SQL",
    "_CREATE_TRAIN_STATE_INDEXES_SQL",
    "_CREATE_TRAIN_RUN_INDEXES_SQL",
    "_CREATE_COVENANT_META_INDEXES_SQL",
    "_CREATE_SHARE_INDEXES_SQL",
    "_CREATE_CROSS_DOMAIN_INDEXES_SQL",
    # BP-51 — Log System / Cost Reconciliation
    "_CREATE_BP51_RUN_SUMMARY_TABLES_SQL",
    "_CREATE_BP51_RUN_SUMMARY_INDEXES_SQL",
    "_CREATE_BP51_STORAGE_USAGE_TABLES_SQL",
    "_CREATE_BP51_STORAGE_USAGE_INDEXES_SQL",
    "_CREATE_BP51_BALANCE_TABLES_SQL",
    "_CREATE_BP51_BALANCE_INDEXES_SQL",
    "_CREATE_BP51_CALIBRATION_TABLES_SQL",
    "_CREATE_BP51_CALIBRATION_INDEXES_SQL",
    "_CREATE_BP51_SQL_AUDIT_TABLES_SQL",
    "_CREATE_BP51_SQL_AUDIT_INDEXES_SQL",
    "_CREATE_BP51_VIEWS_SQL",
]
