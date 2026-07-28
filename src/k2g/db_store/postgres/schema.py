"""DDL definitions for PostgresGraphStore + PostgresVectorStore.

DDL constants are separated from graph.py into per-backend schema.py
files (sqlite / postgres). Table and column names are identical across
backends; only SQL dialect differs.

Usage::

    from k2g.db_store.postgres.schema import (
        CREATE_EXTENSIONS_SQL,
        CREATE_TIER1_TABLES_SQL,  # callable: dim -> list[str]
        CREATE_TIER1_EDGE_TABLES_SQL,
        CREATE_TIER2A_TABLES_SQL,
        CREATE_TIER2B_TABLES_SQL,
        CREATE_INDEXES_SQL,
        CREATE_TIER2A_INDEXES_SQL,
        CREATE_TIER2B_INDEXES_SQL,
    )
    cur.execute(CREATE_TIER1_TABLES_SQL(dim=embedding_dim)[0])

Notes:
- ``CREATE_TIER1_TABLES_SQL`` is a factory function -- ``vector(N)``
  dimension depends on ``settings.embedding_dim`` at call time.
- ``entity_embedding_meta`` is bundled with Tier 1 edges (SQLite parity).
- HNSW indexes are in ``_CREATE_INDEXES_SQL`` -- pgvector required.

Keep sqlite/schema.py in sync when modifying.
"""
from __future__ import annotations

# ============================================================================
# Extensions
# ============================================================================

CREATE_EXTENSIONS_SQL = [
    "CREATE EXTENSION IF NOT EXISTS vector;",
]


# ============================================================================
# Tier 1 — Nodes
# ============================================================================

def CREATE_TIER1_TABLES_SQL(dim: int = 1024) -> list[str]:
    """Tier 1 node table DDL sized to vector(dim).

    Args:
        dim: pgvector dimension (must match settings.embedding_dim).
            Default 1024 for multilingual-e5-large.
    """
    return [
        f"""
        CREATE TABLE IF NOT EXISTS entities (
            id          VARCHAR(64)  PRIMARY KEY,
            name        VARCHAR(512) NOT NULL,
            domain      VARCHAR(128) NOT NULL,
            type        VARCHAR(64),
            user_tag    VARCHAR(128),
            embedding   vector({dim}),
            deprecated  BOOLEAN      NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (name, domain)
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS events (
            id              VARCHAR(64)  PRIMARY KEY,
            domain          VARCHAR(128) NOT NULL,
            summary         TEXT,
            vector_id       VARCHAR(64),
            embedding       vector({dim}),
            timestamp       TIMESTAMPTZ,
            order_index     BIGINT,
            deprecated      BOOLEAN          NOT NULL DEFAULT FALSE,
            influence_score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            ner_method      VARCHAR(32),     -- BP-39
            ner_skip_reason TEXT,             -- BP-39
            created_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW()
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS groups (
            id             VARCHAR(64)  PRIMARY KEY,
            name           VARCHAR(512) NOT NULL,
            level          INTEGER,
            domain         VARCHAR(128) NOT NULL,
            parent_id      VARCHAR(64)  REFERENCES groups(id),
            discriminator  VARCHAR(64),
            original_name  VARCHAR(512),
            source         VARCHAR(64),
            -- Authority axis (≠ source=provenance): 'user' (default) |
            -- 'system' (server-side principal-search mirror). Part of the
            -- uniqueness key so a system mirror can coexist with a same-named
            -- user tag. SQLite parity.
            type           VARCHAR(32)  NOT NULL DEFAULT 'user',
            UNIQUE (name, domain, type),
            user_tag       VARCHAR(128),
            summary        TEXT,
            deprecated     BOOLEAN      NOT NULL DEFAULT FALSE,
            -- RLS parity. owner_id is VARCHAR per spec (UUID type
            -- mismatch with events/entities/plan_nodes — FK deferred
            -- until multi-user ALTER TYPE unification). workspace_id
            -- is a cross-schema FK so no FK in core — cloud_layer
            -- adds the constraint via k2g_migration.py.
            owner_id       VARCHAR(128),
            workspace_id   TEXT,
            visibility     VARCHAR(32)  NOT NULL DEFAULT 'public',
            share_group_id VARCHAR(128) REFERENCES k2g_share_group(id) ON DELETE SET NULL,
            acl_json       JSONB,
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
        """,
    ]


# ============================================================================
# Tier 1 — Edges
# ============================================================================

CREATE_TIER1_EDGE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS participated_in (
        entity_id  VARCHAR(64) NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        event_id   VARCHAR(64) NOT NULL REFERENCES events(id)   ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (entity_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_connection (
        a_id        VARCHAR(64) NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        b_id        VARCHAR(64) NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        event_count INTEGER     NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (a_id, b_id),
        CHECK (a_id < b_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_member_of (
        event_id VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        group_id VARCHAR(64) NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        kind     VARCHAR(32) NOT NULL DEFAULT 'contains',
        PRIMARY KEY (event_id, group_id),
        CHECK (kind IN ('contains','refers'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_sequential_next (
        prev_id    VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        next_id    VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        source     VARCHAR(32) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (prev_id, next_id, source),
        CHECK (source IN ('chunk_order','file_name','folder_name',
                          'thread','topic_segment','user_manual','version up'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_jaccard_connected (
        a_id                VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        b_id                VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        entity_jaccard      REAL,
        group_jaccard       REAL,
        entity_intersection INTEGER,
        group_intersection  INTEGER,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (a_id, b_id),
        CHECK (a_id < b_id)
    );
    """,
    # Entity vector metadata — SQLite parity (BP-32 §A entity_embedding_meta).
    # entities.embedding is stored by pgvector; this table holds only
    # lifecycle metadata.  ProjectionEngine compares ref_event_count
    # to determine cache staleness.
    """
    CREATE TABLE IF NOT EXISTS entity_embedding_meta (
        entity_id        VARCHAR(64)  PRIMARY KEY
                         REFERENCES entities(id) ON DELETE CASCADE,
        computed_at      TIMESTAMPTZ  NOT NULL,
        method           VARCHAR(64)  NOT NULL,
        ref_event_count  INTEGER      NOT NULL DEFAULT 0,
        domain           VARCHAR(128),
        entity_name      VARCHAR(512),
        updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        coherence        DOUBLE PRECISION         -- BP-82: mean resultant length R∈[0,1]
    );
    """,
]


# ============================================================================
# Tier 2a — ContextGroup (BP-28)
# ============================================================================

CREATE_TIER2A_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS context_groups (
        id                      VARCHAR(64)  PRIMARY KEY,
        name                    VARCHAR(512) NOT NULL,
        stage                   VARCHAR(32)  NOT NULL,
        cluster_source          VARCHAR(32)  NOT NULL,
        training_method         VARCHAR(64),
        confidence              REAL,
        member_count_own        INTEGER      DEFAULT 0,
        member_count_total      INTEGER      DEFAULT 0,
        depth                   INTEGER      DEFAULT 0,
        version                 INTEGER      DEFAULT 1,
        narrative_summary       TEXT,
        order_index             BIGINT,
        domain                  VARCHAR(128) NOT NULL,
        parent_id               VARCHAR(64)  REFERENCES context_groups(id),
        plan_stage              VARCHAR(32),
        plan_id                 VARCHAR(64),
        expected_entities       JSONB,
        abandon_reason          TEXT,
        template_id             VARCHAR(64),
        transition_pattern      JSONB,
        source_cg_ids           JSONB,
        instance_count          INTEGER,
        valid_from              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS event_belongs_to_context (
        event_id    VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        target_id   VARCHAR(64) NOT NULL,
        target_kind CHAR(3)     NOT NULL,
        kind        VARCHAR(32),
        order_index INTEGER,
        PRIMARY KEY (event_id, target_id, target_kind),
        CHECK (target_kind IN ('CG ','ETG'))
    );
    """,
]


# ============================================================================
# Tier 2b — ETG / PlanNode / PlanDirectionNode (BP-30)
# ============================================================================

CREATE_TIER2B_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS event_template_groups (
        id                    VARCHAR(64)  PRIMARY KEY,
        name                  VARCHAR(512) NOT NULL,
        domain                VARCHAR(128) NOT NULL,
        transition_pattern    JSONB,
        structure_description TEXT,
        entity_list           JSONB,
        confidence            REAL,
        instance_count        INTEGER,
        created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_nodes (
        id                    VARCHAR(64)  PRIMARY KEY,
        plan_id               VARCHAR(64)  NOT NULL,
        etg_id                VARCHAR(64)  REFERENCES event_template_groups(id),
        domain                VARCHAR(128) NOT NULL,
        name                  VARCHAR(512),
        stage                 VARCHAR(32)  NOT NULL,
        objective_summary     TEXT,
        structure_description TEXT,
        entity_ids            JSONB,
        entity_summary        TEXT,
        order_index           INTEGER,
        realized_event_id     VARCHAR(64)  REFERENCES events(id),
        -- Plan tree: NULL = root node.  Children are promoted to root
        -- when their parent is deleted (ON DELETE SET NULL).
        parent_id             VARCHAR(64)  REFERENCES plan_nodes(id) ON DELETE SET NULL,
        abandon_reason        TEXT,
        created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_direction_nodes (
        id                VARCHAR(64)  PRIMARY KEY,
        plan_id           VARCHAR(64)  NOT NULL,
        domain            VARCHAR(128) NOT NULL,
        goal_text         TEXT,
        stage             VARCHAR(32)  NOT NULL,
        entity_ids        JSONB,
        entity_summary    TEXT,
        order_index       INTEGER,
        realized_event_id VARCHAR(64)  REFERENCES events(id),
        abandon_reason    TEXT,
        created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_from (
        event_id    VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        target_id   VARCHAR(64) NOT NULL,
        target_kind CHAR(3)     NOT NULL,
        source_type VARCHAR(32),
        PRIMARY KEY (event_id, target_id, target_kind),
        CHECK (target_kind IN ('CG ','PLN'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_next (
        from_id    VARCHAR(64) NOT NULL,
        from_kind  CHAR(3)     NOT NULL,
        to_id      VARCHAR(64) NOT NULL,
        to_kind    CHAR(3)     NOT NULL,
        order_idx  INTEGER     DEFAULT 0,
        PRIMARY KEY (from_id, from_kind, to_id, to_kind),
        CHECK (from_kind IN ('CG ','PLN','DIR')),
        CHECK (to_kind   IN ('CG ','PLN','DIR'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS realized_as (
        from_id   VARCHAR(64) NOT NULL,
        from_kind CHAR(3)     NOT NULL,
        event_id  VARCHAR(64) NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        PRIMARY KEY (from_id, from_kind, event_id),
        CHECK (from_kind IN ('CG ','PLN','DIR'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_archive_ref (
        id         VARCHAR(64) PRIMARY KEY,
        event_id   VARCHAR(64) REFERENCES events(id),
        uri        TEXT        NOT NULL,
        sha256     CHAR(64),
        size_bytes BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
]


# ============================================================================
# BP-34 Audit
# ============================================================================
# events_audit: records score_before / score_after / reason on each
# set_influence call.  Written only on explicit user/LLM calls —
# not automatically by K2G internals.  Maintained in SQLite parity.

CREATE_AUDIT_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS events_audit (
        id            BIGSERIAL    PRIMARY KEY,
        event_id      VARCHAR(64)  NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        score_before  DOUBLE PRECISION NOT NULL,
        score_after   DOUBLE PRECISION NOT NULL,
        reason        TEXT,
        set_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
]

CREATE_AUDIT_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_events_audit_event_id ON events_audit(event_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_audit_set_at ON events_audit(set_at);",
]


# ============================================================================
# BP-38 Build Audit
# ============================================================================
# build_audit: records one row per LLM call made by PoolManager.
# Source of evidence for the cost dashboard and retry tracking.
# Maintained in SQLite parity (same schema as sqlite/schema.py).

CREATE_BUILD_AUDIT_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS build_audit (
        id              BIGSERIAL    PRIMARY KEY,
        domain          VARCHAR(64)  NOT NULL,
        persona         VARCHAR(64)  NOT NULL,
        tier_provider   VARCHAR(32),
        tier_model      VARCHAR(128),
        event_id        VARCHAR(64),
        target_kind     VARCHAR(16),
        target_id       VARCHAR(255),
        attempt         INTEGER      NOT NULL DEFAULT 1,
        error_class     VARCHAR(32),
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cost_usd        DOUBLE PRECISION,
        latency_ms      INTEGER,
        reason          TEXT,
        build_id        VARCHAR(64),
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
]

CREATE_BUILD_AUDIT_INDEXES_SQL = [
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


# ============================================================================
# BP-44 Build Failure Manifest
# ============================================================================

CREATE_FAILURE_MANIFEST_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS build_failure_manifest (
        id              BIGSERIAL    PRIMARY KEY,
        domain          VARCHAR(64)  NOT NULL,
        stage           VARCHAR(16)  NOT NULL,
        persona         VARCHAR(64),
        target_kind     VARCHAR(16)  NOT NULL,
        target_id       VARCHAR(255) NOT NULL,
        file_path       TEXT,
        error_class     VARCHAR(32)  NOT NULL,
        error_detail    TEXT,
        last_tier       VARCHAR(128),
        attempt_count   INTEGER      NOT NULL DEFAULT 1,
        build_id        VARCHAR(64)  NOT NULL,
        failed_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
        UNIQUE(domain, stage, target_kind, target_id, build_id)
    );
    """,
]

CREATE_FAILURE_MANIFEST_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_failure_pending "
    "ON build_failure_manifest(domain, status) WHERE status = 'pending';",
    "CREATE INDEX IF NOT EXISTS idx_failure_class "
    "ON build_failure_manifest(error_class);",
    "CREATE INDEX IF NOT EXISTS idx_failure_target "
    "ON build_failure_manifest(domain, target_kind, target_id);",
]


# ============================================================================
# BP-45 Train State Manifest + Cluster Narrative Cache
# ============================================================================

CREATE_TRAIN_STATE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS train_state_manifest (
        id                BIGSERIAL    PRIMARY KEY,
        domain            VARCHAR(64)  NOT NULL,
        target_kind       VARCHAR(16)  NOT NULL,
        target_id         VARCHAR(255) NOT NULL,
        phase             VARCHAR(32)  NOT NULL,
        state             VARCHAR(16)  NOT NULL,
        version           INTEGER      NOT NULL DEFAULT 1,
        claim_owner       VARCHAR(128),
        claim_expires_at  TIMESTAMPTZ,
        signature         TEXT,
        last_input_hash   TEXT,
        last_trained_at   TIMESTAMPTZ,
        last_phase_run    TIMESTAMPTZ,
        upstream_deps     TEXT,
        error_count       INTEGER      NOT NULL DEFAULT 0,
        last_error        TEXT,
        algorithm_version VARCHAR(64),
        created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        UNIQUE(domain, target_kind, target_id, phase)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_narrative_cache (
        id                       BIGSERIAL    PRIMARY KEY,
        domain                   VARCHAR(64)  NOT NULL,
        cluster_signature        VARCHAR(128) NOT NULL,
        narrative                TEXT         NOT NULL,
        member_count             INTEGER,
        model_version            VARCHAR(128) NOT NULL,
        narrative_format_version INTEGER      NOT NULL DEFAULT 1,
        created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        used_count               INTEGER      NOT NULL DEFAULT 1,
        UNIQUE(domain, cluster_signature, model_version, narrative_format_version)
    );
    """,
]

CREATE_TRAIN_STATE_INDEXES_SQL = [
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


# ============================================================================
# BP-89 Train Run Manifest (global batch audit) + community assignment
# ============================================================================

CREATE_TRAIN_RUN_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS train_run (
        id              VARCHAR(64)  PRIMARY KEY,
        kind            VARCHAR(32)  NOT NULL,
        domain          VARCHAR(64),
        started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        finished_at     TIMESTAMPTZ,
        status          VARCHAR(16)  NOT NULL DEFAULT 'running',
        event_count     INTEGER,
        entity_count    INTEGER,
        edge_count      INTEGER,
        params_json     JSONB,
        metrics_json    JSONB,
        rows_written    INTEGER,
        error_text      TEXT,
        notes           TEXT,
        triggered_by    VARCHAR(32)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_community_assignment (
        run_id        VARCHAR(64) NOT NULL REFERENCES train_run(id) ON DELETE CASCADE,
        entity_id     VARCHAR(64) NOT NULL REFERENCES entities(id)  ON DELETE CASCADE,
        community_id  INTEGER     NOT NULL,
        PRIMARY KEY (run_id, entity_id)
    );
    """,
    # BP-90: event community (leidenalg on event_jaccard_connected).
    """
    CREATE TABLE IF NOT EXISTS event_community_assignment (
        run_id        VARCHAR(64) NOT NULL REFERENCES train_run(id) ON DELETE CASCADE,
        event_id      VARCHAR(64) NOT NULL REFERENCES events(id)    ON DELETE CASCADE,
        community_id  INTEGER     NOT NULL,
        PRIMARY KEY (run_id, event_id)
    );
    """,
]

CREATE_TRAIN_RUN_INDEXES_SQL = [
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


# ============================================================================
# Covenant Metadata (integrated into main DB)
# ============================================================================
# k2g_covenant: domain × source registry (filesystem/vcs/database/email/rss).
# k2g_covenant_history: change audit log (add/remove/enable/disable/modify).
# k2g_source_file: per-file/message/article ingestion metadata.
#
# Maintained in SQLite parity (same schema as sqlite/schema.py;
# only JSONB casting differs).  Any legacy covenant.db (separate SQLite)
# is migrated once by scripts/migrate_covenant_db.py.

CREATE_COVENANT_META_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_covenant (
        id           BIGSERIAL    PRIMARY KEY,
        domain       VARCHAR(128) NOT NULL,
        source_id    VARCHAR(255) NOT NULL,
        type         VARCHAR(32)  NOT NULL,
        config_json  JSONB        NOT NULL,
        group_policy VARCHAR(16)  NOT NULL,
        enabled      BOOLEAN      NOT NULL DEFAULT TRUE,
        description  TEXT,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        UNIQUE (domain, source_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_covenant_history (
        id           BIGSERIAL    PRIMARY KEY,
        covenant_id  BIGINT,
        action       VARCHAR(16)  NOT NULL,
        domain       VARCHAR(128) NOT NULL,
        source_id    VARCHAR(255) NOT NULL,
        before_json  JSONB,
        after_json   JSONB,
        acted_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_source_file (
        id            VARCHAR(64)  PRIMARY KEY,
        covenant_id   VARCHAR(64)  NOT NULL,
        relative_path TEXT         NOT NULL,
        content_hash  VARCHAR(128) NOT NULL,
        size_bytes    BIGINT,
        mime_type     VARCHAR(128),
        last_ingested TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        last_vcs_sha  VARCHAR(64),
        extractor_key VARCHAR(64)  NOT NULL DEFAULT 'default',
        unit_strategy VARCHAR(32)  NOT NULL DEFAULT 'whole_file',
        domain        VARCHAR(128)
    );
    """,
]

CREATE_COVENANT_META_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_covenant_domain "
    "ON k2g_covenant (domain);",
    "CREATE INDEX IF NOT EXISTS idx_covenant_enabled "
    "ON k2g_covenant (enabled);",
    "CREATE INDEX IF NOT EXISTS idx_covenant_history_domain "
    "ON k2g_covenant_history (domain);",
    "CREATE INDEX IF NOT EXISTS idx_sf_covenant "
    "ON k2g_source_file (covenant_id);",
]


# ============================================================================
# BP-47 Phase B — Share Group + Member + Audit
# ============================================================================

CREATE_SHARE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_share_group (
        id           VARCHAR(128) PRIMARY KEY,
        domain       VARCHAR(128) NOT NULL,
        name         VARCHAR(255) NOT NULL,
        description  TEXT,
        owner_id     VARCHAR(128),
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_share_member (
        share_group_id VARCHAR(128) NOT NULL
            REFERENCES k2g_share_group(id) ON DELETE CASCADE,
        member_kind    VARCHAR(16)  NOT NULL,
        member_id      VARCHAR(255) NOT NULL,
        role           VARCHAR(16)  NOT NULL DEFAULT 'reader',
        added_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        PRIMARY KEY (share_group_id, member_kind, member_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_share_audit (
        id             BIGSERIAL    PRIMARY KEY,
        action         VARCHAR(32)  NOT NULL,
        share_group_id VARCHAR(128),
        member_kind    VARCHAR(16),
        member_id      VARCHAR(255),
        actor_id       VARCHAR(128),
        before_json    JSONB,
        after_json     JSONB,
        acted_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    """,
]

CREATE_SHARE_INDEXES_SQL = [
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


# ============================================================================
# BP-47 Phase C — Cross-domain primitives
# ============================================================================

CREATE_CROSS_DOMAIN_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_entity_alias (
        entity_id_a   VARCHAR(64)  NOT NULL,
        entity_id_b   VARCHAR(64)  NOT NULL,
        relation      VARCHAR(32)  NOT NULL DEFAULT 'same',
        confidence    DOUBLE PRECISION DEFAULT 1.0,
        asserted_by   VARCHAR(128),
        asserted_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        PRIMARY KEY (entity_id_a, entity_id_b)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_event_reference (
        event_id_a    VARCHAR(64)  NOT NULL,
        event_id_b    VARCHAR(64)  NOT NULL,
        kind          VARCHAR(32)  NOT NULL,
        confidence    DOUBLE PRECISION DEFAULT 1.0,
        asserted_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        PRIMARY KEY (event_id_a, event_id_b, kind)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS k2g_domain_share (
        source_domain  VARCHAR(128) NOT NULL,
        target_kind    VARCHAR(16)  NOT NULL,
        target_id      VARCHAR(255) NOT NULL,
        scope_filter   JSONB,
        role           VARCHAR(16)  NOT NULL DEFAULT 'reader',
        granted_by     VARCHAR(128),
        granted_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        expires_at     TIMESTAMPTZ,
        PRIMARY KEY (source_domain, target_kind, target_id)
    );
    """,
]

CREATE_CROSS_DOMAIN_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_entity_alias_b "
    "ON k2g_entity_alias (entity_id_b);",
    "CREATE INDEX IF NOT EXISTS idx_event_reference_b "
    "ON k2g_event_reference (event_id_b);",
    "CREATE INDEX IF NOT EXISTS idx_domain_share_target "
    "ON k2g_domain_share (target_kind, target_id, source_domain);",
]


# ============================================================================
# Domain soft-registry — empty-domain support (see sqlite/schema.py twin)
# ============================================================================

CREATE_DOMAIN_REGISTRY_SQL = [
    """
    CREATE TABLE IF NOT EXISTS domain_registry (
        name        TEXT        PRIMARY KEY,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
]


# ============================================================================
# Indexes — B-tree + HNSW (pgvector)
# ============================================================================

CREATE_INDEXES_SQL = [
    # entities
    "CREATE INDEX IF NOT EXISTS idx_entities_domain ON entities(domain);",
    "CREATE INDEX IF NOT EXISTS idx_entities_name_lower ON entities(LOWER(name));",
    # events
    "CREATE INDEX IF NOT EXISTS idx_events_domain ON events(domain);",
    "CREATE INDEX IF NOT EXISTS idx_events_domain_order ON events(domain, order_index);",
    "CREATE INDEX IF NOT EXISTS idx_events_vector_id ON events(vector_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_influence "
    "ON events(domain, influence_score DESC);",
    # groups
    "CREATE INDEX IF NOT EXISTS idx_groups_domain_name ON groups(domain, name);",
    "CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_groups_discriminator ON groups(discriminator);",
    # event edges
    "CREATE INDEX IF NOT EXISTS idx_participated_in_event ON participated_in(event_id);",
    "CREATE INDEX IF NOT EXISTS idx_event_member_of_group ON event_member_of(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_event_sequential_next_next ON event_sequential_next(next_id);",
    # read-time theta_e filter + ego UNION b_id arm (SQLite parity)
    "CREATE INDEX IF NOT EXISTS idx_ejc_entity_jaccard ON event_jaccard_connected(entity_jaccard);",
    "CREATE INDEX IF NOT EXISTS idx_ejc_b_id ON event_jaccard_connected(b_id);",
    # entity_embedding_meta (SQLite parity)
    "CREATE INDEX IF NOT EXISTS idx_entity_embedding_meta_domain "
    "ON entity_embedding_meta(domain);",
    # pgvector HNSW (events.embedding / entities.embedding)
    "CREATE INDEX IF NOT EXISTS idx_events_embedding_hnsw "
    "ON events USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);",
    "CREATE INDEX IF NOT EXISTS idx_entities_embedding_hnsw "
    "ON entities USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);",
]

CREATE_TIER2A_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_cg_domain_stage ON context_groups(domain, stage);",
    "CREATE INDEX IF NOT EXISTS idx_cg_parent ON context_groups(parent_id);",
    "CREATE INDEX IF NOT EXISTS idx_cg_cluster_source ON context_groups(cluster_source);",
    "CREATE INDEX IF NOT EXISTS idx_cg_training_method ON context_groups(training_method);",
    "CREATE INDEX IF NOT EXISTS idx_ebtc_target ON event_belongs_to_context(target_id, target_kind);",
]

CREATE_TIER2B_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_etg_domain ON event_template_groups(domain);",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_plan_id ON plan_nodes(plan_id);",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_stage ON plan_nodes(stage);",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_domain ON plan_nodes(domain);",
    "CREATE INDEX IF NOT EXISTS idx_direction_nodes_plan_id ON plan_direction_nodes(plan_id);",
    "CREATE INDEX IF NOT EXISTS idx_direction_nodes_stage ON plan_direction_nodes(stage);",
    "CREATE INDEX IF NOT EXISTS idx_plan_next_to ON plan_next(to_id, to_kind);",
    "CREATE INDEX IF NOT EXISTS idx_realized_as_event ON realized_as(event_id);",
]


# ============================================================================
# build_run_summary
# ============================================================================
# One row per build run: INSERT at start (status='running'),
# UPDATE at end (success/failed/partial).
# Aggregated summary row for data spread across build_audit and
# build_failure_manifest.

CREATE_BP51_RUN_SUMMARY_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS build_run_summary (
        build_id           VARCHAR(64) PRIMARY KEY,
        session_id         VARCHAR(64) NOT NULL,
        domain             VARCHAR(64) NOT NULL,
        owner_id           VARCHAR(128),
        started_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ended_at           TIMESTAMPTZ,
        status             VARCHAR(16) NOT NULL DEFAULT 'running',
        stages_run         JSONB,
        events_produced    INTEGER,
        entities_loaded    INTEGER,
        llm_calls          INTEGER,
        llm_cost_usd       NUMERIC(14, 6),
        llm_failures       INTEGER,
        source_purge_count INTEGER DEFAULT 0,
        error_summary      TEXT,
        config_snapshot    JSONB
    );
    """,
]

CREATE_BP51_RUN_SUMMARY_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_build_run_summary_owner_time "
    "ON build_run_summary (owner_id, started_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_build_run_summary_domain_time "
    "ON build_run_summary (domain, started_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_build_run_summary_status "
    "ON build_run_summary (status, started_at DESC);",
]


# ============================================================================
# BP-51 Phase B — storage_usage_daily
# ============================================================================

CREATE_BP51_STORAGE_USAGE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS storage_usage_daily (
        id                  BIGSERIAL PRIMARY KEY,
        snapshot_date       DATE NOT NULL,
        domain              VARCHAR(64) NOT NULL,
        owner_id            VARCHAR(128),
        events_count        INTEGER,
        events_bytes        BIGINT,
        segment_blob_bytes  BIGINT,
        vector_count        INTEGER,
        total_bytes         BIGINT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (snapshot_date, domain, owner_id)
    );
    """,
]

CREATE_BP51_STORAGE_USAGE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_storage_usage_owner_date "
    "ON storage_usage_daily (owner_id, snapshot_date DESC);",
    "CREATE INDEX IF NOT EXISTS idx_storage_usage_domain_date "
    "ON storage_usage_daily (domain, snapshot_date DESC);",
]


# ============================================================================
# BP-51 Phase C — Cost Reconciliation (L2)
# ============================================================================

CREATE_BP51_BALANCE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS provider_balance_snapshot (
        snapshot_id    BIGSERIAL PRIMARY KEY,
        snapshot_at    TIMESTAMPTZ NOT NULL,
        provider       VARCHAR(32) NOT NULL,
        metric_kind    VARCHAR(16) NOT NULL,
        value_usd      NUMERIC(14, 6) NOT NULL,
        currency       VARCHAR(8) DEFAULT 'USD',
        fx_rate        NUMERIC(10, 6),
        raw_response   JSONB,
        poll_status    VARCHAR(32) NOT NULL,
        poll_error     TEXT,
        UNIQUE (provider, snapshot_at)
    );
    """,
]

CREATE_BP51_BALANCE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_balance_snapshot_provider_time "
    "ON provider_balance_snapshot (provider, snapshot_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_balance_snapshot_status "
    "ON provider_balance_snapshot (poll_status, snapshot_at DESC) "
    "WHERE poll_status != 'success';",
]


# ============================================================================
# BP-51 Phase C — Cost Calibration (L3 lazy)
# ============================================================================

CREATE_BP51_CALIBRATION_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS provider_calibration (
        provider              VARCHAR(32) NOT NULL,
        model                 VARCHAR(128) NOT NULL,
        token_kind            VARCHAR(16) NOT NULL,
        unit_price_usd_per_1m NUMERIC(12, 6) NOT NULL,
        fitted_at             TIMESTAMPTZ NOT NULL,
        window_start          TIMESTAMPTZ NOT NULL,
        window_end            TIMESTAMPTZ NOT NULL,
        window_count          INTEGER NOT NULL,
        r_squared             NUMERIC(6, 4),
        drift_vs_yaml_pct     NUMERIC(6, 2),
        fitted_method         VARCHAR(16) NOT NULL,
        PRIMARY KEY (provider, model, token_kind, fitted_at)
    );
    """,
]

CREATE_BP51_CALIBRATION_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_calibration_lookup "
    "ON provider_calibration (provider, model, token_kind, fitted_at DESC);",
]


# ============================================================================
# k2g_sql_audit — activate SQL audit log
# ============================================================================
# Already reserved in DATA_OWNER_EXCLUDED (data_owner.py).
# Inserted immediately after each k2g_sql_query call in mcp/sql_tools.py.

CREATE_BP51_SQL_AUDIT_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS k2g_sql_audit (
        id              BIGSERIAL PRIMARY KEY,
        invoked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        actor_id        VARCHAR(128),
        domain          VARCHAR(64),
        query_sql       TEXT NOT NULL,
        row_count       INTEGER,
        duration_ms     INTEGER,
        status          VARCHAR(16) NOT NULL,
        error_class     VARCHAR(64),
        error_detail    TEXT
    );
    """,
]

CREATE_BP51_SQL_AUDIT_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_sql_audit_actor_time "
    "ON k2g_sql_audit (actor_id, invoked_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_sql_audit_status "
    "ON k2g_sql_audit (status, invoked_at DESC) "
    "WHERE status != 'success';",
]


# ============================================================================
# Views — cost drift and calibrated cost
# ============================================================================
# Postgres uses LATERAL joins; semantically equivalent to SQLite correlated
# scalar sub-queries.
#
# DROP + CREATE pattern: ``CREATE OR REPLACE VIEW`` is only allowed when
# the existing column order/names are unchanged or new columns are appended
# at the end.  v_build_audit_calibrated uses ``ba.*``, so re-running the
# schema after a build_audit ALTER (e.g. adding owner_id) would cause
# InvalidTableDefinition because the stored column order conflicts with the
# new expansion.  Use ``DROP VIEW IF EXISTS ... CASCADE; CREATE VIEW ...``
# (same pattern as sqlite/schema.py) to avoid this.

CREATE_BP51_VIEWS_SQL = [
    "DROP VIEW IF EXISTS v_owner_usage_monthly CASCADE;",
    """
    CREATE VIEW v_owner_usage_monthly AS
    SELECT
      COALESCE(owner_id, 'system') AS owner_id,
      TO_CHAR(created_at, 'YYYY-MM') AS month,
      COUNT(*) FILTER (WHERE error_class IS NULL) AS llm_success,
      COUNT(*) FILTER (WHERE error_class IS NOT NULL) AS llm_fail,
      COALESCE(SUM(cost_usd), 0.0) AS cost_usd_estimated,
      COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens
    FROM build_audit
    GROUP BY COALESCE(owner_id, 'system'), TO_CHAR(created_at, 'YYYY-MM');
    """,
    "DROP VIEW IF EXISTS v_provider_drift_hourly CASCADE;",
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
             DATE_TRUNC('hour', created_at) AS hour,
             SUM(cost_usd) AS local_cost
      FROM build_audit
      WHERE cost_usd IS NOT NULL
      GROUP BY tier_provider, DATE_TRUNC('hour', created_at)
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
            AND l.hour = DATE_TRUNC('hour', d.snapshot_at);
    """,
    "DROP VIEW IF EXISTS v_build_audit_calibrated CASCADE;",
    """
    CREATE VIEW v_build_audit_calibrated AS
    SELECT
      ba.*,
      COALESCE(
          (COALESCE(ba.input_tokens, 0)  / 1e6) * ci.unit_price_usd_per_1m
        + (COALESCE(ba.output_tokens, 0) / 1e6) * co.unit_price_usd_per_1m,
          ba.cost_usd
      ) AS cost_usd_calibrated,
      GREATEST(ci.fitted_at, co.fitted_at) AS calibrated_at,
      CASE WHEN ci.fitted_at IS NOT NULL AND co.fitted_at IS NOT NULL
           THEN 'calibrated' ELSE 'estimated' END AS cost_source
    FROM build_audit ba
    LEFT JOIN LATERAL (
        SELECT unit_price_usd_per_1m, fitted_at
          FROM provider_calibration
         WHERE provider = ba.tier_provider AND model = ba.tier_model
           AND token_kind = 'input' AND fitted_at <= ba.created_at
         ORDER BY fitted_at DESC LIMIT 1
    ) ci ON true
    LEFT JOIN LATERAL (
        SELECT unit_price_usd_per_1m, fitted_at
          FROM provider_calibration
         WHERE provider = ba.tier_provider AND model = ba.tier_model
           AND token_kind = 'output' AND fitted_at <= ba.created_at
         ORDER BY fitted_at DESC LIMIT 1
    ) co ON true;
    """,
]


__all__ = [
    "CREATE_EXTENSIONS_SQL",
    "CREATE_TIER1_TABLES_SQL",
    "CREATE_TIER1_EDGE_TABLES_SQL",
    "CREATE_TIER2A_TABLES_SQL",
    "CREATE_TIER2B_TABLES_SQL",
    "CREATE_AUDIT_TABLES_SQL",
    "CREATE_BUILD_AUDIT_TABLES_SQL",
    "CREATE_FAILURE_MANIFEST_TABLES_SQL",
    "CREATE_TRAIN_STATE_TABLES_SQL",
    "CREATE_TRAIN_RUN_TABLES_SQL",
    "CREATE_COVENANT_META_TABLES_SQL",
    "CREATE_SHARE_TABLES_SQL",
    "CREATE_CROSS_DOMAIN_TABLES_SQL",
    "CREATE_DOMAIN_REGISTRY_SQL",
    "CREATE_INDEXES_SQL",
    "CREATE_TIER2A_INDEXES_SQL",
    "CREATE_TIER2B_INDEXES_SQL",
    "CREATE_AUDIT_INDEXES_SQL",
    "CREATE_BUILD_AUDIT_INDEXES_SQL",
    "CREATE_FAILURE_MANIFEST_INDEXES_SQL",
    "CREATE_TRAIN_STATE_INDEXES_SQL",
    "CREATE_TRAIN_RUN_INDEXES_SQL",
    "CREATE_COVENANT_META_INDEXES_SQL",
    "CREATE_SHARE_INDEXES_SQL",
    "CREATE_CROSS_DOMAIN_INDEXES_SQL",
    # BP-51 — Log System / Cost Reconciliation
    "CREATE_BP51_RUN_SUMMARY_TABLES_SQL",
    "CREATE_BP51_RUN_SUMMARY_INDEXES_SQL",
    "CREATE_BP51_STORAGE_USAGE_TABLES_SQL",
    "CREATE_BP51_STORAGE_USAGE_INDEXES_SQL",
    "CREATE_BP51_BALANCE_TABLES_SQL",
    "CREATE_BP51_BALANCE_INDEXES_SQL",
    "CREATE_BP51_CALIBRATION_TABLES_SQL",
    "CREATE_BP51_CALIBRATION_INDEXES_SQL",
    "CREATE_BP51_SQL_AUDIT_TABLES_SQL",
    "CREATE_BP51_SQL_AUDIT_INDEXES_SQL",
    "CREATE_BP51_VIEWS_SQL",
]
