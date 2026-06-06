"""Staging record schemas.

Each ndjson file stores one pydantic-serialized dict per line. The models are
intentionally kept thin so the loader can stream large sessions while keeping
peak RSS bounded.

local_ref design:
- Only Event requires a local_ref (used for prev_local_ref chaining).
- Entity / Group use name as their key — the loader deduplicates via MERGE.
- vector_id / content_id are pre-assigned by the producer (ULID) and stored
  in staging; the loader reuses them as-is (Qdrant upsert is idempotent).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StagedEvent(BaseModel):
    """Event record produced by the producer stage.

    Each line captures the output of NER steps 1-3. The loader uses this
    information to reconstruct the graph (step 4) and the vector upsert.

    ID policy:
    - vector_id / content_id: finalized by the producer (ContentStore already
      has the INSERT). The loader reuses the same ids for Qdrant upsert, which
      is idempotent.
    - event_id / order_index: decided by the loader in graph.create_event.
      Not stored in staging (depends on graph state).
    - prev_local_ref chain: the loader maintains a local_ref → event_id map
      and reconstructs the chain from it.
    """

    local_ref: str = Field(..., description="Session-local sequential ref (e_0001, e_0002, ...)")
    event_id: str | None = Field(
        default=None,
        description=(
            "Pre-assigned deterministic event_id (e.g. sha256 hash of the "
            "manifest). If None, the loader issues a new UUID in "
            "graph.create_event (legacy behaviour). If set, bulk load uses "
            "that id for INSERT (ON CONFLICT DO NOTHING), so re-running the "
            "same manifest produces zero duplicate events or chains."
        ),
    )
    prev_local_ref: str | None = Field(
        default=None,
        description=(
            "local_ref of the preceding event — used for SEQUENTIAL_NEXT "
            "chaining. None at domain or file boundaries."
        ),
    )
    domain: str
    timestamp: str = Field(..., description="ISO-8601 timestamp (naive or tz-aware string)")
    summary: str
    content_id: str = Field(..., description="content_id already inserted into ContentStore")
    vector_id: str = Field(..., description="Assigned by the producer; reused by the loader on Qdrant upsert")
    storage_uri: str = Field(default="", description="ObjectStorage URI (LocalObjectStorage file://...)")
    content_type: str = Field(default="text/plain")
    entity_names: list[str] = Field(default_factory=list)
    entity_types: dict[str, str] = Field(
        default_factory=dict,
        description="name → type mapping; populated by the loader during Entity MERGE.",
    )
    group_names: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical names in group hierarchy order. The loader "
            "reconstructs the level from the list index."
        ),
    )
    group_kinds: list[str] | None = Field(
        default=None,
        description=(
            "List of 'contains'/'refers' kinds, same length as group_names. "
            "VCS commits use [contains, refers, refers, ...] "
            "(branch=contains, changed files=refers). "
            "None means all groups default to 'contains' — "
            "doc/code producers leave this as None."
        ),
    )
    inline_meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional payload fields (reference values used as Qdrant metadata).",
    )

    # Source Lineage fields (additive; default None/"" for backward compat)
    source_provider: str | None = Field(
        default=None,
        description="'text_blob_postgres' | 'external_db' | 'doxygen' | ...",
    )
    source_id: str | None = Field(
        default=None,
        description="Provider-specific identifier of the source document.",
    )
    source_locator: dict[str, Any] = Field(
        default_factory=dict,
        description="Location within the source document (provider-specific schema).",
    )
    raw_document_content: str | None = Field(
        default=None,
        description=(
            "Populated only for the *first segment* of a producer with "
            "store_raw=True. The loader inserts raw_document once, then "
            "treats subsequent segments as None."
        ),
    )
    is_first_segment: bool = Field(
        default=False,
        description=(
            "Marks the first segment for a given source_id "
            "(triggers raw_document INSERT)."
        ),
    )

    # NER metadata propagation (activates events.ner_method / ner_skip_reason
    # columns). Default None for backward compatibility.
    ner_method: str | None = Field(
        default=None,
        description=(
            "NER processing method — 'llm' | 'dummy' | 'doxygen' | etc. "
            "None when NER was skipped or the caller did not set a label."
        ),
    )
    ner_skip_reason: str | None = Field(
        default=None,
        description=(
            "Reason NER was skipped — 'input_too_short' | 'empty_summary' | "
            "'all_entities_low_grounding' | 'suspicious_entity_pattern' | etc. "
            "None means NER ran normally without being skipped."
        ),
    )


class StagedEntity(BaseModel):
    """Entity name and type record. Merged by name, so no local_ref."""

    name: str
    type: str = "unknown"
    first_seen_event_ref: str | None = None


class StagedGroup(BaseModel):
    """Group hierarchy record. The loader restores it via
    register_path_group_with_ancestors."""

    name: str = Field(..., description="Canonical full path (`domain::path/to/file`)")
    level: int = Field(default=0, description="0=root, 1=first segment, 2=..., path depth")
    discriminator: str | None = Field(
        default=None,
        description=(
            "Discriminator for disambiguating duplicate names. None for normal paths."
        ),
    )
    parent_name: str | None = Field(
        default=None,
        description="Canonical name of the parent group. None for the root.",
    )


class StagingManifest(BaseModel):
    """One manifest per domain directory. The loader requires produce_completed_at
    to be set before it will process the domain."""

    session_id: str
    domain: str
    bp_version: str = "staging-v1"
    produce_started_at: str | None = None
    produce_completed_at: str | None = None
    load_started_at: str | None = None
    load_completed_at: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    incremental_base: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "IncrementalBuilder integration — e.g. {'manifest_db': 'build_manifest.db'}"
        ),
    )
    extras: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "StagedEvent",
    "StagedEntity",
    "StagedGroup",
    "StagingManifest",
]
