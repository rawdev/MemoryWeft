"""BP-74 — Column whitelists and JSONL serializer.

Each archive table member has a fixed column set, pinned here at the module
level.  `manifest.schema_version` major-bumps when a column list changes
incompatibly (insertion of a non-default NOT NULL column, etc.); minor bumps
when new optional columns appear.

Why fixed lists rather than runtime `PRAGMA table_info`?
- Archive format becomes a stable contract independent of the source DB's
  current ALTER state.
- The importer can validate the archive against its own DB schema deterministically.
- Round-trip across SQLite ↔ Postgres is unambiguous.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from typing import Any


# --- Tier 1 column lists --------------------------------------------------

ENTITIES_COLUMNS: tuple[str, ...] = (
    "id", "name", "domain", "type", "user_tag", "deprecated", "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
)

EVENTS_COLUMNS: tuple[str, ...] = (
    "id", "domain", "summary", "vector_id", "timestamp", "order_index",
    "deprecated", "influence_score", "ner_method", "ner_skip_reason",
    "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
    "source_provider", "source_id", "source_locator", "source_version",
    "source_tombstoned_at", "tombstoned_at", "tombstoned_reason", "tombstoned_by",
)

# ``type`` is part of the uniqueness key — ``UNIQUE (name, domain, type)`` — so
# it must travel: it is what lets a 'system' mirror coexist with a same-named
# user tag. Dropping it collapses both rows onto the DEFAULT 'user' and the
# restore dies on the unique index.
GROUPS_COLUMNS: tuple[str, ...] = (
    "id", "name", "level", "domain", "parent_id", "discriminator",
    "original_name", "source", "type", "user_tag", "summary", "deprecated",
    "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
)

PARTICIPATED_IN_COLUMNS: tuple[str, ...] = ("entity_id", "event_id", "created_at")

EVENT_MEMBER_OF_COLUMNS: tuple[str, ...] = ("event_id", "group_id", "kind")

EVENT_SEQUENTIAL_NEXT_COLUMNS: tuple[str, ...] = (
    "prev_id", "next_id", "source", "created_at",
)

ENTITY_CONNECTION_COLUMNS: tuple[str, ...] = (
    "a_id", "b_id", "event_count", "created_at",
)


# --- Tier 2 column lists (used by Step 8) ---------------------------------

CONTEXT_GROUPS_COLUMNS: tuple[str, ...] = (
    "id", "name", "stage", "cluster_source", "training_method", "confidence",
    "member_count_own", "member_count_total", "depth", "version",
    "narrative_summary", "order_index", "domain", "parent_id",
    "plan_stage", "plan_id", "expected_entities", "abandon_reason",
    "template_id", "transition_pattern", "source_cg_ids", "instance_count",
    "valid_from", "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
)

EVENT_TEMPLATE_GROUPS_COLUMNS: tuple[str, ...] = (
    "id", "name", "domain", "transition_pattern", "structure_description",
    "entity_list", "confidence", "instance_count", "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
)

EVENT_BELONGS_TO_CONTEXT_COLUMNS: tuple[str, ...] = (
    "event_id", "target_id", "target_kind", "kind", "order_index",
)

PLAN_NODES_COLUMNS: tuple[str, ...] = (
    "id", "plan_id", "etg_id", "domain", "name", "stage",
    "objective_summary", "structure_description",
    "entity_ids", "entity_summary", "order_index",
    "realized_event_id", "abandon_reason", "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
)

PLAN_DIRECTION_NODES_COLUMNS: tuple[str, ...] = (
    "id", "plan_id", "domain", "goal_text", "stage",
    "entity_ids", "entity_summary", "order_index",
    "realized_event_id", "abandon_reason", "created_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
)

PLAN_FROM_COLUMNS: tuple[str, ...] = (
    "event_id", "target_id", "target_kind", "source_type",
)

PLAN_NEXT_COLUMNS: tuple[str, ...] = (
    "from_id", "from_kind", "to_id", "to_kind", "order_idx",
)

REALIZED_AS_COLUMNS: tuple[str, ...] = ("from_id", "from_kind", "event_id")


# --- Derived column lists (jaccard / community / training) ----------------
# Carried in the archive so a migration is lossless (no post-import recompute
# of jaccard + leiden communities). All optional — older archives omit them.

EVENT_JACCARD_CONNECTED_COLUMNS: tuple[str, ...] = (
    "a_id", "b_id", "entity_jaccard", "group_jaccard",
    "entity_intersection", "group_intersection", "created_at",
)

ENTITY_EMBEDDING_META_COLUMNS: tuple[str, ...] = (
    "entity_id", "computed_at", "method", "ref_event_count", "domain",
    "entity_name", "updated_at", "coherence",
)

TRAIN_RUN_COLUMNS: tuple[str, ...] = (
    "id", "kind", "domain", "started_at", "finished_at", "status",
    "event_count", "entity_count", "edge_count", "params_json", "metrics_json",
    "rows_written", "error_text", "notes", "triggered_by",
)

ENTITY_COMMUNITY_ASSIGNMENT_COLUMNS: tuple[str, ...] = (
    "run_id", "entity_id", "community_id",
)

EVENT_COMMUNITY_ASSIGNMENT_COLUMNS: tuple[str, ...] = (
    "run_id", "event_id", "community_id",
)

# id is BIGSERIAL — omitted so a restore regenerates it (the cache is keyed by
# its UNIQUE(domain, signature, model, format) and never FK-referenced).
CLUSTER_NARRATIVE_CACHE_COLUMNS: tuple[str, ...] = (
    "domain", "cluster_signature", "narrative", "member_count",
    "model_version", "narrative_format_version", "created_at", "used_count",
)

K2G_ENTITY_ALIAS_COLUMNS: tuple[str, ...] = (
    "entity_id_a", "entity_id_b", "relation", "confidence",
    "asserted_by", "asserted_at",
)

K2G_EVENT_REFERENCE_COLUMNS: tuple[str, ...] = (
    "event_id_a", "event_id_b", "kind", "confidence", "asserted_at",
)


# --- Segments column lists ------------------------------------------------

K2G_RAW_DOCUMENT_COLUMNS: tuple[str, ...] = (
    "id", "domain", "source_uri", "title", "content", "byte_size",
    "sha256", "ingested_at",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
    "tombstoned_at", "tombstoned_reason", "tombstoned_by",
)

K2G_SEGMENT_BLOB_COLUMNS: tuple[str, ...] = (
    "id", "raw_document_id", "order_index", "byte_start", "byte_end", "content",
    "owner_id", "org_id", "visibility", "acl_json", "share_group_id",
    "tombstoned_at", "tombstoned_reason", "tombstoned_by",
)


# --- Content store column list (BP-86) -----------------------------------

CONTENT_STORE_COLUMNS: tuple[str, ...] = (
    "content_id", "domain", "vector_id", "content_type", "storage_uri",
    "inline_meta", "created_at",
)


# --- Helpers --------------------------------------------------------------

_BYTES_PREFIX = "base64:"


# Columns whose values represent a boolean across all backends.  SQLite stores
# them as INTEGER 0/1; Postgres as BOOLEAN.  Historical PG→SQLite migrations
# also left ``'f'`` / ``'t'`` string values in some SQLite rows.  The archive
# format normalises all of these to JSON ``true`` / ``false`` so that both
# backends' default DB-API adapters (sqlite3: bool→INTEGER; psycopg2:
# bool→BOOLEAN) accept the value without manual casts.
BOOLEAN_COLUMNS: frozenset[str] = frozenset({"deprecated"})

_FALSY_TOKENS = {"0", "f", "false", "no", "n", "", None}
_TRUTHY_TOKENS = {"1", "t", "true", "yes", "y"}


def coerce_boolean(v: Any) -> bool | None:
    """Normalize any of ``0/1``, ``'f'/'t'``, ``'false'/'true'``,
    ``bool`` to a Python ``bool``.  Returns ``None`` for ``None``."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUTHY_TOKENS:
            return True
        if s in _FALSY_TOKENS:
            return False
    # Unknown form — preserve as-is so the caller can decide.  Returning False
    # could silently flip semantics on a "deprecated" row.
    raise ValueError(f"cannot coerce to boolean: {v!r}")


def normalize_value(v: Any) -> Any:
    """Normalize a single DB-API value for JSON serialization.

    Conversions:
    - ``datetime`` (aware or naive) → ISO 8601 UTC ``Z`` suffix
    - ``date`` → ``YYYY-MM-DD``
    - ``bytes`` / ``bytearray`` / ``memoryview`` → ``"base64:<b64>"``
    - everything else passes through unchanged (must be JSON-compatible)
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return _BYTES_PREFIX + base64.b64encode(bytes(v)).decode("ascii")
    return v


def denormalize_value(v: Any) -> Any:
    """Inverse of `normalize_value` for binary blobs.

    Only the bytes prefix is reversed.  Datetimes are kept as ISO strings —
    SQLite/PG accept ISO strings directly for TIMESTAMP columns, so the
    importer does not need to reconstruct datetime objects.
    """
    if isinstance(v, str) and v.startswith(_BYTES_PREFIX):
        return base64.b64decode(v[len(_BYTES_PREFIX):])
    return v


def denormalize_for_column(col: str, v: Any) -> Any:
    """Column-aware inverse normalization for the importer.

    Boolean columns are coerced (handles legacy archives that may carry
    ``'f'`` / ``'t'`` string or ``0`` / ``1`` integer instead of JSON
    ``true`` / ``false``).  Other columns fall through to
    ``denormalize_value`` (bytes prefix unwrap).
    """
    if col in BOOLEAN_COLUMNS:
        return coerce_boolean(v)
    if isinstance(v, (dict, list)):
        # JSON/JSONB column — exported as a nested object/array (normalize_value
        # passes JSON-compatible values through). Re-serialize to a JSON string:
        # Postgres coerces text→jsonb on INSERT, SQLite stores the text. Without
        # this, psycopg2 raises "can't adapt type 'dict'" on JSONB columns
        # (e.g. train_run params). pgvector embeddings export as a '[...]' string
        # (not a Python list), so they are unaffected.
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return denormalize_value(v)


def _normalize_for_column(col: str, v: Any) -> Any:
    """Apply column-aware normalization.

    Boolean columns get coerced from any of the input forms (int / str /
    bool) to a Python ``bool``; other columns fall through to the generic
    ``normalize_value``.
    """
    if col in BOOLEAN_COLUMNS:
        return coerce_boolean(v)
    return normalize_value(v)


def row_to_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    """Convert a DB-API row to a normalized ``{column: value}`` dict.

    Accepts ``sqlite3.Row``, ``dict``, or positional tuple/list.
    """
    if isinstance(row, dict):
        return {col: _normalize_for_column(col, row.get(col)) for col in columns}
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            available = set(keys())
            return {
                col: _normalize_for_column(col, row[col]) if col in available else None
                for col in columns
            }
        except (TypeError, IndexError):
            pass
    # positional
    return {col: _normalize_for_column(col, row[i]) for i, col in enumerate(columns)}


def dumps(obj: Any) -> str:
    """Compact JSON line for JSONL output (no whitespace, no ASCII coercion)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def paramstyle_placeholder(backend: str) -> str:
    """Return the SQL placeholder for the current backend.

    ``sqlite`` → ``?``, ``postgres`` → ``%s``.  Used to assemble dynamic
    SELECT/INSERT statements in exporter / importer.
    """
    if backend == "sqlite":
        return "?"
    if backend in ("postgres", "postgresql", "pg"):
        return "%s"
    raise ValueError(f"unknown backend: {backend!r}")


# Mapping for the exporter's "what JSONL files to write" loop. Tier 2 entries
# are exposed here too so Step 8 can extend Tier-1 without renaming anything.
TIER1_TABLES: dict[str, tuple[str, ...]] = {
    "entities": ENTITIES_COLUMNS,
    "events": EVENTS_COLUMNS,
    "groups": GROUPS_COLUMNS,
    "participated_in": PARTICIPATED_IN_COLUMNS,
    "event_member_of": EVENT_MEMBER_OF_COLUMNS,
    "event_sequential_next": EVENT_SEQUENTIAL_NEXT_COLUMNS,
    "entity_connection": ENTITY_CONNECTION_COLUMNS,
}


# Mapping for Tier-2 export/import. event_belongs_to_context / plan_from /
# realized_as now ride DERIVED_TABLES (below); plan_next is omitted as it has no
# domain anchor (from_id/to_id are CG/PLN/DIR ids, not events/entities).
TIER2_TABLES: dict[str, tuple[str, ...]] = {
    "context_groups": CONTEXT_GROUPS_COLUMNS,
    "event_template_groups": EVENT_TEMPLATE_GROUPS_COLUMNS,
    "plan_nodes": PLAN_NODES_COLUMNS,
    "plan_direction_nodes": PLAN_DIRECTION_NODES_COLUMNS,
    "k2g_entity_alias": K2G_ENTITY_ALIAS_COLUMNS,
    "k2g_event_reference": K2G_EVENT_REFERENCE_COLUMNS,
}


# Segment tables — gated by options.include_segments.
SEGMENT_TABLES: dict[str, tuple[str, ...]] = {
    "k2g_raw_document": K2G_RAW_DOCUMENT_COLUMNS,
    "k2g_segment_blob": K2G_SEGMENT_BLOB_COLUMNS,
}


# Derived tables — jaccard graph + leiden community assignments + their
# train_run parent + embedding meta + narrative cache. Gated by
# options.include_derived so a migration carries the computed graph instead of
# forcing a post-import recompute. plan_from / realized_as ride options
# .include_plan (plan edges). plan_next is omitted — it has no domain anchor.
DERIVED_TABLES: dict[str, tuple[str, ...]] = {
    "train_run": TRAIN_RUN_COLUMNS,
    "entity_embedding_meta": ENTITY_EMBEDDING_META_COLUMNS,
    "event_jaccard_connected": EVENT_JACCARD_CONNECTED_COLUMNS,
    "event_belongs_to_context": EVENT_BELONGS_TO_CONTEXT_COLUMNS,
    "entity_community_assignment": ENTITY_COMMUNITY_ASSIGNMENT_COLUMNS,
    "event_community_assignment": EVENT_COMMUNITY_ASSIGNMENT_COLUMNS,
    "cluster_narrative_cache": CLUSTER_NARRATIVE_CACHE_COLUMNS,
    "plan_from": PLAN_FROM_COLUMNS,
    "realized_as": REALIZED_AS_COLUMNS,
}


# Content store (BP-86) — a *separate* DB connection (content_store.db), not
# the graph DB.  Gated by options.include_content.  Carries the raw text
# bodies that mweft_remember / the build pipeline store outside the graph.
CONTENT_TABLES: dict[str, tuple[str, ...]] = {
    "content_store": CONTENT_STORE_COLUMNS,
}


# Primary key columns per table.  Used by the importer's overwrite
# (ON CONFLICT) and dry-run (existence probe) paths.
TABLE_PKS: dict[str, tuple[str, ...]] = {
    # Tier 1
    "entities": ("id",),
    "events": ("id",),
    "groups": ("id",),
    "participated_in": ("entity_id", "event_id"),
    "event_member_of": ("event_id", "group_id"),
    "event_sequential_next": ("prev_id", "next_id", "source"),
    "entity_connection": ("a_id", "b_id"),
    # Tier 2
    "context_groups": ("id",),
    "event_template_groups": ("id",),
    "plan_nodes": ("id",),
    "plan_direction_nodes": ("id",),
    "k2g_entity_alias": ("entity_id_a", "entity_id_b"),
    "k2g_event_reference": ("event_id_a", "event_id_b", "kind"),
    # Segments
    "k2g_raw_document": ("id",),
    "k2g_segment_blob": ("id",),
    # Content store (BP-86)
    "content_store": ("content_id",),
    # Derived (jaccard / community / training)
    "train_run": ("id",),
    "entity_embedding_meta": ("entity_id",),
    "event_jaccard_connected": ("a_id", "b_id"),
    "event_belongs_to_context": ("event_id", "target_id", "target_kind"),
    "entity_community_assignment": ("run_id", "entity_id"),
    "event_community_assignment": ("run_id", "event_id"),
    "cluster_narrative_cache": (
        "domain", "cluster_signature", "model_version", "narrative_format_version",
    ),
    "plan_from": ("event_id", "target_id", "target_kind"),
    "realized_as": ("from_id", "from_kind", "event_id"),
}
