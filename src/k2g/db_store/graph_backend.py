"""GraphBackend Protocol — graph backend abstraction for db_store.

The Neo4j / Kuzu implementations have been moved to legacy/.
PostgresGraphStore and SqliteGraphStore are the two implementations
that structurally satisfy this Protocol. Uses ``typing.Protocol``
so no runtime inheritance (ABC) is needed -- static typing and
``isinstance`` (runtime_checkable) verify the contract.

Method groups:
    (a) Schema bootstrap
    (b) Tier 1 writes (Entity / Event / Group / edges)
    (c) Tier 1 reads / statistics
    (d) Jaccard / chain queries
    (e) ContextGroup Training (Tier 2a)
    (f) ETG / PlanNode / PlanDirectionNode (Tier 2b)
    (g) Lifecycle (ping / close)

Notes:
  - Protocol uses duck typing (structural subtyping). Existing
    Neo4j/Kuzu classes do not need an ``implements`` declaration.
  - Re-evaluate ABC migration when Alembic is introduced (Phase 3).
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from k2g.core.config import DomainConfig


SequentialSource = Literal[
    "chunk_order", "file_name", "folder_name",
    "thread", "topic_segment", "user_manual",
    "version up",
]


@runtime_checkable
class GraphStoreProtocol(Protocol):
    """K2G GraphStore public contract.

    Every backend (Neo4j, Kuzu, Postgres) must implement all methods
    of this Protocol with identical signatures. Follows the append-only
    principle.
    """

    # ------------------------------------------------------------------
    # (a) Schema bootstrap
    # ------------------------------------------------------------------

    def setup_schema(self) -> None:
        """Initialise Tier 1 schema (Entity / Event / Group / 6 edges)."""
        ...

    def setup_training_schema(self) -> None:
        """Initialise Tier 2a ContextGroup schema."""
        ...

    def setup_bp30_schema(self) -> None:
        """Initialise Tier 2b schema (ETG / PlanNode / PlanDirectionNode)."""
        ...

    # ------------------------------------------------------------------
    # (b) Tier 1 writes
    # ------------------------------------------------------------------

    def ensure_group_tree(self, domain_config: DomainConfig) -> dict[str, str]:
        """Bootstrap the domain group_tree. Returns {name: group_id}."""
        ...

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
        ...

    def register_path_group_with_ancestors(
        self,
        leaf_canonical: str,
        *,
        domain: str,
        summary: str = "",
    ) -> str:
        ...

    def get_group_id(self, group_name: str, domain: str) -> str | None:
        ...

    def link_or_create_entity(
        self, name: str, domain: str, type: str | None = None,
    ) -> str:
        ...

    def create_event(
        self,
        vector_id: str,
        domain: str,
        timestamp: str | None,
        order_index: int,
        summary: str = "",
    ) -> str:
        ...

    def upsert_entity_connection(
        self, entity_id_a: str, entity_id_b: str, event_id: str,
    ) -> None:
        ...

    def link_participated_in(self, entity_id: str, event_id: str) -> None:
        ...

    def link_event_member_of(
        self,
        event_id: str,
        group_id: str,
        *,
        kind: Literal["contains", "refers"] = "contains",
    ) -> None:
        ...

    def reparent_group(self, group_id: str, new_parent_id: str) -> None:
        ...

    def link_sequential_next(
        self,
        prev_event_id: str,
        event_id: str,
        source: SequentialSource = "chunk_order",
    ) -> None:
        ...

    def set_user_tag(
        self, node_type: str, node_id: str, user_tag: str | None,
    ) -> None:
        ...

    # ------------------------------------------------------------------
    # (c) Tier 1 reads / statistics
    # ------------------------------------------------------------------

    def get_last_event_id(self, domain: str) -> str | None:
        ...

    def get_order_index(self, domain: str) -> int:
        ...

    def get_entity_by_id(self, entity_id: str) -> dict[str, Any] | None:
        ...

    def get_event_by_id(self, event_id: str) -> dict[str, Any] | None:
        ...

    def get_connected_entities(
        self, entity_id: str, max_hops: int = 2,
    ) -> list[dict[str, Any]]:
        ...

    def get_statistics(self, domain: str | None = None) -> dict[str, int]:
        ...

    def get_all_entity_names(self, limit: int = 10000) -> list[str]:
        ...

    def list_entities(
        self,
        domain: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        ...

    def search_entities_by_name(
        self, name: str, domain: str | None = None, limit: int = 5,
    ) -> list[dict[str, Any]]:
        ...

    def search_groups_by_name(
        self,
        name: str,
        domain: str | list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring search on tag (groups).

        Twin of ``search_entities_by_name``. Only ``deprecated=0`` rows;
        sorted by ``LENGTH(name) ASC, name ASC`` (shortest match first).
        """
        ...

    def list_groups(self, domain: str) -> list[dict[str, Any]]:
        ...

    def merge_groups(self, alias_id: str, canonical_id: str) -> dict[str, Any]:
        """Merge ``alias_id`` group into ``canonical_id`` (BP — Page 2-1).

        Re-points ``event_member_of`` rows (dedup on conflict), re-parents child
        groups, then soft-deletes the alias (``deprecated=1``). Raises
        ``ValueError`` if ids match / a group is missing / canonical is deprecated.
        Returns counts ``{memberships_moved, memberships_dedup, children_reparented}``.
        """
        ...

    def get_entity_events(
        self,
        entity_id: str,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        ...

    def get_group_events(
        self,
        group_id: str,
        include_deleted: bool = False,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        ...

    def get_adjacent_events(self, event_id: str) -> dict[str, Any]:
        ...

    def get_event_context(self, event_id: str) -> dict[str, Any]:
        ...

    def list_domains(self) -> list[str]:
        ...

    # --- Domain soft-registry (empty-domain support) ------------------
    # `domain` stays a free-text tag on every table; ``domain_registry`` is a
    # side list so a domain can exist without data (create-then-delete-while-
    # empty). ``list_domains`` is unchanged (events-only); the union lives in
    # ``list_managed_domains``.

    def register_domain(self, name: str) -> bool:
        """Add ``name`` to the registry. True if newly added, False if it existed."""
        ...

    def list_registered_domains(self) -> list[str]:
        """Domains in ``domain_registry`` only (no data-derived domains)."""
        ...

    def list_managed_domains(self) -> list[str]:
        """Sorted union: registry ∪ DISTINCT(events/entities/groups).domain."""
        ...

    def domain_data_counts(self, name: str) -> dict[str, int]:
        """Per-domain row counts used for the empty check: events/entities/groups."""
        ...

    def delete_domain(self, name: str) -> dict[str, Any]:
        """Delete a domain only if it has no events/entities.

        Refuses with ``{"ok": False, "reason": "has_data", ...counts}`` when data
        exists; otherwise purges group scaffolding + the registry row and returns
        ``{"ok": True}``.
        """
        ...

    def rename_domain(self, old: str, new: str) -> dict[str, Any]:
        """Relabel ``old`` → ``new`` across every table with a ``domain`` column.

        ``new`` must be non-empty and not already exist (pure relabel, no merge).
        Runs in one transaction; returns ``{"ok": True, "tables": [...]}`` or
        ``{"ok": False, "reason": ...}``.
        """
        ...

    def check_entities_exist(self, names: list[str]) -> list[dict[str, Any]]:
        ...

    def get_entity_event_counts(
        self, entity_ids: list[str],
    ) -> dict[str, int]:
        """Return DB-wide event participation count per entity.

        Counts rows in ``participated_in`` (i.e. number of events the
        entity has appeared in across the whole DB). Missing ids map to 0.
        Used by the graph viewer to surface a stable "activity" metric in
        tooltips, independent of which subgraph is currently displayed.
        """
        ...

    def replace_entity_in_links(
        self, alias_id: str, canonical_id: str,
    ) -> dict[str, int]:
        """BP-87 entity merge helper.

        Move all link rows pointing at ``alias_id`` to ``canonical_id``:
        - ``participated_in``: dedup rows the canonical already has, then
          UPDATE the rest.
        - ``entity_connection``: re-insert alias-touching pairs with the
          canonical id (honoring CHECK a_id < b_id), merging event_count
          on conflict, then delete leftover alias rows.

        Idempotent. Caller should wrap in a single transaction. Returns
        counts dict for audit logging.
        """
        ...

    # ------------------------------------------------------------------
    # (c.2) BP-89 — Train Run Manifest (global batch audit)
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
        """Insert new ``train_run`` row with status='running'. Return run_id.

        ``params`` is JSON-serialized into ``params_json``. The
        ``event_count`` / ``entity_count`` / ``edge_count`` snapshot
        becomes the baseline for the next ``needs_rebuild`` judgment.
        """
        ...

    def train_run_complete(
        self,
        run_id: str,
        *,
        metrics: dict[str, Any] | None = None,
        rows_written: int | None = None,
    ) -> None:
        """Mark ``train_run`` row as completed.

        Sets ``status='completed'`` + ``finished_at=NOW`` + ``metrics_json``
        + ``rows_written``. No-op if the run is not in 'running' state.
        """
        ...

    def train_run_fail(self, run_id: str, error_text: str) -> None:
        """Mark ``train_run`` as failed with the given error text."""
        ...

    def train_run_latest_completed(
        self,
        kind: str,
        *,
        domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent completed run for the (kind, domain) tuple.

        Domain matching: when ``domain`` is None we match runs with
        ``domain IS NULL`` (cross-domain). When ``domain`` is a string we
        match equality. Returns None if no completed run exists.
        """
        ...

    def upsert_entity_community_assignments(
        self,
        run_id: str,
        assignments: "list[tuple[str, int]]",
    ) -> int:
        """Bulk upsert ``(run_id, entity_id, community_id)`` rows.

        Returns the count of rows written. Uses INSERT ... ON CONFLICT to
        be idempotent: re-running with the same (run_id, entity_id) updates
        ``community_id`` rather than failing.
        """
        ...

    def get_entity_community_for_run(
        self,
        run_id: str,
        entity_ids: "list[str]",
    ) -> dict[str, int]:
        """Return {entity_id: community_id} for the given run, restricted
        to the entity_ids subset. Missing pairs are omitted from the dict.
        """
        ...

    def upsert_event_community_assignments(
        self,
        run_id: str,
        assignments: "list[tuple[str, int]]",
    ) -> int:
        """BP-90 — bulk upsert ``(run_id, event_id, community_id)`` rows.

        Event-community mirror of ``upsert_entity_community_assignments``.
        Idempotent via INSERT ... ON CONFLICT. Returns rows written.
        """
        ...

    def get_event_community_for_run(
        self,
        run_id: str,
        event_ids: "list[str]",
    ) -> dict[str, int]:
        """BP-90 — return {event_id: community_id} for the run, restricted
        to the event_ids subset. Missing pairs omitted.
        """
        ...

    def list_community_members(
        self,
        run_id: str,
        node_kind: str,
    ) -> "list[dict[str, Any]]":
        """BP-90 — all members of a community run, one row per node.

        ``node_kind`` ∈ {'entity','event'} selects the assignment table.
        Each row: ``{node_id, community_id, name, weight}`` where ``name`` is
        entity name / event summary and ``weight`` is a prominence metric
        (entity: #events participated; event: #participants) for ranking.
        """
        ...

    # ------------------------------------------------------------------
    # (c.1) UI Manager additions (timeline / entity-graph)
    # ------------------------------------------------------------------

    def list_events_paged(
        self,
        domain: str,
        *,
        order_by: str = "timestamp",
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Domain timeline. order_by in {timestamp, order_index, created_at}."""
        ...

    def list_entity_connections(self, domain: str) -> list[dict[str, Any]]:
        """Full dump of domain entity-connections (a_id, b_id, event_count)."""
        ...

    def list_entity_connection_events(
        self, a_id: str, b_id: str, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List of events where two entities co-occur (JOIN on both participated_in)."""
        ...

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
        ...

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
    ) -> int:
        ...

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
    ) -> int:
        ...

    def get_jaccard_neighbors(
        self, event_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        ...

    def mark_deprecated(self, node_type: str, node_id: str) -> bool:
        ...

    def mark_events_deprecated_by_group(self, group_id: str) -> int:
        ...

    def find_group_by_path(self, relative_path: str, domain: str) -> str | None:
        ...

    def list_event_ids(self, domain: str) -> list[str]:
        ...

    def iter_sequential_pairs(self, domain: str) -> list[tuple[str, str]]:
        ...

    def iter_jaccard_pairs(self, domain: str) -> list[tuple[str, str, float]]:
        ...

    # ------------------------------------------------------------------
    # (e) ContextGroup Training (Tier 2a)
    # ------------------------------------------------------------------

    def create_context_group(self, cg: dict[str, Any]) -> str:
        ...

    def get_context_group(self, cg_id: str) -> dict[str, Any] | None:
        ...

    def delete_context_group(self, cg_id: str) -> None:
        ...

    def update_cg_stage(self, cg_id: str, stage: str) -> None:
        ...

    def update_cg_name(self, cg_id: str, name: str) -> None:
        ...

    def update_cg_member_counts(self, cg_id: str, own: int, total: int) -> None:
        ...

    def update_cg_narrative(self, cg_id: str, summary: str) -> None:
        ...

    def update_cg_order(self, cg_id: str, order_index: int) -> None:
        ...

    def link_event_belongs_to_context(
        self, event_id: str, cg_id: str, kind: str = "",
    ) -> None:
        ...

    def link_cg_child_of(
        self, child_cg_id: str, parent_cg_id: str, depth: int = 1,
    ) -> None:
        ...

    def link_cg_realized_as(self, cg_id: str, event_id: str) -> None:
        ...

    def link_plan_from(self, event_id: str, cg_id: str) -> None:
        ...

    def link_plan_next(
        self, from_cg_id: str, to_cg_id: str, order: int = 0,
    ) -> None:
        ...

    def get_cg_members(self, cg_id: str) -> list[dict[str, Any]]:
        ...

    def get_cg_events(self, cg_id: str) -> list[dict[str, Any]]:
        ...

    def get_contexts_of_event(self, event_id: str) -> list[dict[str, Any]]:
        ...

    def get_cg_children(self, cg_id: str) -> list[dict[str, Any]]:
        ...

    def get_cg_hierarchy(self, cg_id: str) -> dict[str, Any]:
        ...

    def list_cg_by_stage(self, stage: str) -> list[dict[str, Any]]:
        ...

    def count_cg_by_stage(self) -> dict[str, int]:
        ...

    def list_control_nodes(
        self, domain: str, include_seed: bool = False,
    ) -> list[dict[str, Any]]:
        ...

    def set_cg_plan_attrs(
        self,
        cg_id: str,
        plan_stage: str,
        plan_id: str,
        expected_entities_json: str,
        abandon_reason: str = "",
    ) -> None:
        ...

    def update_cg_plan_stage(
        self,
        cg_id: str,
        plan_stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        ...

    def list_plan_seed_cgs(self, domain: str) -> list[dict[str, Any]]:
        ...

    def get_plan_chain(self, start_cg_id: str) -> list[dict[str, Any]]:
        ...

    def list_abandoned_plans(
        self, domain: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        ...

    def set_cg_template_attrs(
        self,
        cg_id: str,
        template_id: str,
        transition_pattern_json: str,
        source_cg_ids_json: str,
        instance_count: int,
    ) -> None:
        ...

    def list_cg_templates(self, domain: str) -> list[dict[str, Any]]:
        ...

    # ------------------------------------------------------------------
    # (f) BP-30 Tier 2b: ETG / PlanNode / PlanDirectionNode
    # ------------------------------------------------------------------

    def create_etg(self, etg: dict[str, Any]) -> str:
        ...

    def get_etg(self, etg_id: str) -> dict[str, Any] | None:
        ...

    def list_etgs(self, domain: str) -> list[dict[str, Any]]:
        ...

    def link_event_belongs_to_etg(
        self, event_id: str, etg_id: str, order: int = 0,
    ) -> None:
        ...

    def get_etg_events(self, etg_id: str) -> list[dict[str, Any]]:
        ...

    def create_plan_node(self, pn: dict[str, Any]) -> str:
        ...

    def get_plan_node(self, plan_node_id: str) -> dict[str, Any] | None:
        ...

    def get_plan_chain_v2(self, plan_id: str) -> list[dict[str, Any]]:
        ...

    def update_plan_node_stage(
        self,
        plan_node_id: str,
        stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        ...

    def link_plan_from_etg(self, event_id: str, plan_node_id: str) -> None:
        ...

    def link_plan_next_plan(
        self, from_plan_id: str, to_plan_id: str, order: int = 0,
    ) -> None:
        ...

    def link_realized_as_plan(self, plan_node_id: str, event_id: str) -> None:
        ...

    def list_active_plans(
        self, domain: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        ...

    def create_direction_node(self, dn: dict[str, Any]) -> str:
        ...

    def get_direction_node(self, dir_id: str) -> dict[str, Any] | None:
        ...

    def get_direction_chain(self, plan_id: str) -> list[dict[str, Any]]:
        ...

    def update_direction_stage(
        self,
        dir_id: str,
        stage: str,
        realized_event_id: str | None = None,
        abandon_reason: str | None = None,
    ) -> None:
        ...

    def link_plan_next_direction(
        self, from_dir_id: str, to_dir_id: str, order: int = 0,
    ) -> None:
        ...

    def link_realized_as_direction(self, dir_id: str, event_id: str) -> None:
        ...

    # ------------------------------------------------------------------
    # (g) Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        ...

    def close(self) -> None:
        ...
