"""Pure functions shared by all Producer implementations.

Extracted from the legacy `IngestionPipeline.produce_to_stage /
load_from_stage / ingest_commit_to_stage / load_commits_from_stage`
onto a DbStore foundation.  These are free functions (not class
methods) so each producer can compose them freely.

Dependencies:
- ContentBackend / ObjectBackend (db_store.content / db_store.object)
- EmbeddingClient (embedding computation)
- StageWriter (staging writes)
- DbStore (load-phase graph/vector)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from k2g.core.models import VectorMetadata, new_vector_id
from k2g.producer.base import LoadResult, ProduceResult
from k2g.staging import (
    StagedEntity,
    StagedEvent,
    StagedGroup,
)
from k2g.staging.event_keys import extract_event_keys

if TYPE_CHECKING:
    import numpy as np

    from k2g.core.config import DomainConfig
    from k2g.core.models import NEROutput
    from k2g.db_store import DbStore
    from k2g.db_store.content_backend import ContentBackend
    from k2g.db_store.object_backend import ObjectBackend
    from k2g.embedding.client import EmbeddingClient
    from k2g.security.data_owner import DataOwner
    from k2g.staging import StageWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NER output validation -- Phase 1 lenient
# ---------------------------------------------------------------------------
#
# On entry to `produce_ner_output`, detect suspicious signals in
# ner_output and record warning + inline_meta flags.  Strict mode is
# added in the subsequent Phase 2 (connected via had_failure trigger).
#
# Domain thresholds from `DomainConfig._raw["ner"]` keys:
#   min_summary_length (default 10) -- minimum summary length
#   min_entities       (default 0)  -- minimum entity count (may be 0
#                                      depending on domain)
#   min_entity_name_length (default 1) -- minimum entity name length
#                                         (all single-char -> suspicious)
# strict_validation (default False) -- when True, issues cause produce
# failure (currently unused -- activated in Phase 2).


def _validate_ner_output(
    ner_output: "NEROutput",
    domain_config: "DomainConfig",
    *,
    raw_text: str | None = None,
) -> list[str]:
    """Return list of suspicious signals from ner_output. Empty list means OK.

    Includes L1 (input_too_short) + L4 (suspicious_entity_pattern).
    If raw_text is None, L1 is skipped (backward compat).
    """
    issues: list[str] = []
    raw = getattr(domain_config, "_raw", {}) or {}
    ner_cfg = raw.get("ner", {}) if isinstance(raw, dict) else {}

    min_summary = int(ner_cfg.get("min_summary_length", 10))
    min_entities = int(ner_cfg.get("min_entities", 0))
    min_entity_name = int(ner_cfg.get("min_entity_name_length", 1))
    min_input_chars = int(ner_cfg.get("min_input_chars", 200))

    # L1 -- input length floor
    if raw_text is not None and len(raw_text.strip()) < min_input_chars:
        issues.append(f"input_too_short_{len(raw_text.strip())}")

    summary = (ner_output.event.summary or "").strip()
    if not summary:
        issues.append("empty_summary")
    elif len(summary) < min_summary:
        issues.append(f"summary_too_short_{len(summary)}")

    if min_entities > 0 and len(ner_output.entities) < min_entities:
        issues.append(f"entities_below_min_{min_entities}")

    if ner_output.entities:
        too_short = [
            e for e in ner_output.entities
            if len(e.name.strip()) <= min_entity_name
        ]
        if len(too_short) == len(ner_output.entities):
            issues.append("all_entities_too_short")

        # L4 -- suspicious entity name pattern (chain runaway / abnormal length)
        for e in ner_output.entities:
            name = e.name.strip()
            if _is_suspicious_entity_name(name):
                issues.append(f"suspicious_entity_pattern:{name[:60]}")
                break  # record one issue and stop (avoid looping)

    return issues


# L4 -- suspicious entity name patterns
_CHAIN_PATTERN = re.compile(r"_and_\w+_and_\w+_and_\w+", re.IGNORECASE)
_MAX_ENTITY_NAME_LEN = 100


def _is_suspicious_entity_name(name: str) -> bool:
    """L4 -- detect chain runaway (`_and_*` 3+ accumulated) or length 100+.

    Blocks LLM hallucination chains (e.g.,
    `is_llm_unsupported_and_required_and_opt_in_and_opt_out_and_...`).
    """
    if len(name) > _MAX_ENTITY_NAME_LEN:
        return True
    if _CHAIN_PATTERN.search(name):
        return True
    return False


# ---------------------------------------------------------------------------
# L6 -- Entity Grounding (token Jaccard >= 0.5)
# ---------------------------------------------------------------------------
#
# Verify that 50%+ of each extracted entity name's tokens actually
# exist in raw_text.  A code-level hallucination prevention layer that
# does not rely solely on LLM prompts.
#
# Algorithm:
#   1. normalize: lowercase + punctuation/underscore -> space + collapse
#   2. tokenize: whitespace split + camelCase split (PoolManager -> [pool, manager])
#   3. Jaccard score = |entity_tokens & text_tokens| / |entity_tokens|
#   4. score < threshold -> grounding fail
#
# Threshold 0.5 rationale: blocks hallucination scores in the 0.0-0.3
# range while allowing normal paraphrases (0.5+) to pass.

_TOKEN_SPLIT_PATTERN = re.compile(r"[\s\W_]+", re.UNICODE)
_CAMEL_TOKEN_PATTERN = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z]|$)|\d+")


def _normalize_for_grounding(s: str) -> str:
    """L6 normalize -- lowercase + non-word chars/underscore -> space."""
    return _TOKEN_SPLIT_PATTERN.sub(" ", s.lower()).strip()


def _tokenize_for_grounding(s: str) -> list[str]:
    """L6 tokenize -- whitespace/punctuation/underscore split + camelCase split.

    Handles mixed domains of CJK (space-delimited) and code identifiers
    (camelCase / snake_case).  ASCII tokens get camelCase splitting;
    non-ASCII tokens are preserved as-is from the split result.
    """
    out: list[str] = []
    for tok in _TOKEN_SPLIT_PATTERN.split(s):
        if not tok:
            continue
        # Only split camelCase for ASCII code identifiers -- preserve non-ASCII as-is
        if tok.isascii():
            sub = _CAMEL_TOKEN_PATTERN.findall(tok)
            if sub:
                out.extend(t.lower() for t in sub)
                continue
        out.append(tok.lower())
    return out


def _validate_entity_grounding(
    ner_output: "NEROutput",
    raw_text: str,
    *,
    threshold: float = 0.5,
) -> list[tuple[str, float]]:
    """L6 -- return entities whose token Jaccard score < threshold.

    Args:
        ner_output: NER result.
        raw_text: Original segment text (NER input).
        threshold: Passing score threshold (default 0.5).

    Returns:
        [(entity_name, score), ...] for entities below threshold.
        Empty list means all passed.
    """
    if not ner_output.entities or not raw_text or not raw_text.strip():
        return []
    text_tokens = set(_tokenize_for_grounding(_normalize_for_grounding(raw_text)))
    issues: list[tuple[str, float]] = []
    for ne in ner_output.entities:
        ent_tokens = set(_tokenize_for_grounding(_normalize_for_grounding(ne.name)))
        if not ent_tokens:
            continue
        overlap = ent_tokens & text_tokens
        score = len(overlap) / len(ent_tokens)
        if score < threshold:
            issues.append((ne.name, score))
    return issues


# ---------------------------------------------------------------------------
# L3 -- Phase 2 strict (hard issue -> entity discard)
# ---------------------------------------------------------------------------
#
# Phase 1 lenient only warns on issues and loads entities as-is.
# Phase 2 strict discards entity / group on hard issues (the event
# itself is still loaded -- zero manifest impact in LOAD phase).

_HARD_ISSUE_PREFIXES = (
    "input_too_short_",            # L1
    "empty_summary",               # existing
    "all_entities_too_short",      # existing
    "all_entities_low_grounding",  # L6 -- added in produce_ner_output
    "suspicious_entity_pattern:",  # L4
)


def _classify_ner_issues(issues: list[str]) -> tuple[bool, str | None]:
    """L3 -- detect hard issues. Returns (skip_entities, skip_reason).

    When skip_entities=True, produce_ner_output discards entities/groups
    and records the reason in StagedEvent.ner_skip_reason.
    """
    for iss in issues:
        for prefix in _HARD_ISSUE_PREFIXES:
            if iss.startswith(prefix):
                # Truncate reason for log readability (the column itself
                # is unlimited TEXT)
                return True, iss[:200]
    return False, None


def _format_source_locator(inline_meta: dict) -> str:
    """Build a location hint string from inline_meta.

    Priority:
    1. ``path_parts`` (DocBatch -- `["docs", "foo.md"]` etc.) -> ``docs/foo.md``
    2. ``source_path`` (Novel -- absolute path string)
    3. ``compound_kind`` + ``section_heading`` (Doxygen)
    Then append ``::seg=N`` / ``::slide=N`` / ``::page=N`` / ``::sect=...``
    if ``segment_index`` / ``slide`` / ``page`` / ``section_heading`` are present.
    """
    if not isinstance(inline_meta, dict):
        return ""

    path_parts = inline_meta.get("path_parts")
    source_path = inline_meta.get("source_path")
    if path_parts and isinstance(path_parts, (list, tuple)) and path_parts:
        base = "/".join(str(p) for p in path_parts)
    elif source_path:
        base = str(source_path)
    else:
        base = ""

    suffix_parts: list[str] = []
    sect = inline_meta.get("section_heading")
    if sect:
        suffix_parts.append(f"sect={sect}")
    seg_idx = inline_meta.get("segment_index")
    if seg_idx is not None:
        suffix_parts.append(f"seg={seg_idx}")
    slide = inline_meta.get("slide")
    if slide is not None:
        suffix_parts.append(f"slide={slide}")
    page = inline_meta.get("page")
    if page is not None:
        suffix_parts.append(f"page={page}")
    if not base and not suffix_parts:
        return ""
    if suffix_parts:
        return f"{base}::" + ",".join(suffix_parts) if base else ",".join(suffix_parts)
    return base


# ---------------------------------------------------------------------------
# NER-based produce (shared by doc_batch / doxygen / novel)
# ---------------------------------------------------------------------------

def produce_ner_output(
    ner_output: "NEROutput",
    domain_config: "DomainConfig",
    *,
    writer: "StageWriter",
    local_ref: str,
    prev_local_ref: str | None,
    content: "ContentBackend",
    object_storage: "ObjectBackend",
    embedding_client: "EmbeddingClient",
    embed_text_fn=None,
    # Source Lineage (additive, default None / False)
    source_provider: str | None = None,
    source_id: str | None = None,
    source_locator: dict | None = None,
    raw_document_content: str | None = None,
    is_first_segment: bool = False,
    # DataOwner (additive, default = public).
    # When data_owner is specified, to_columns()'s 5 fields are merged
    # into inline_meta -> loader propagates via graph.add_event INSERT.
    data_owner: "DataOwner | None" = None,
    # NER metadata + grounding validation
    # raw_segment_text: NER input text (L1 input length floor + L6 grounding).
    #   None skips both layers (backward compat). Filled by doc_batch/novel/c2nt.
    # ner_method: 'llm' | 'dummy' | 'doxygen' etc. -> propagated to events.ner_method.
    raw_segment_text: str | None = None,
    ner_method: str | None = None,
    # Pre-assigned deterministic event_id (manifest dedup).  None means
    # the loader issues a new UUID (existing behaviour).  Propagated to
    # StagedEvent.event_id -> bulk load INSERTs with that id
    # (ON CONFLICT DO NOTHING).
    event_id: str | None = None,
) -> ProduceResult:
    """Legacy produce_to_stage (no graph/vector access) -- rebuilt on db_store.

    embed_text_fn: Function to build text for embedding.  Defaults to
    `[entity_names] content.data`.  Special cases like doxygen's
    structured / structured+llm inject their own embed_text_fn.

    Source Lineage (additive -- None default preserves existing path 100%):
    - source_provider: 'text_blob_postgres' / 'external_db' / 'doxygen' / 'vcs'
    - source_id: per-provider source identifier (file uuid / commit hash / compound id)
    - source_locator: location within source (per-provider schema)
    - raw_document_content: first segment only, full source attached (store_raw=true)
    - is_first_segment: True triggers raw_document INSERT

    NER hallucination defense (6-layer):
    - L1 (input_too_short): raw_segment_text < 200 chars -> entity discard
    - L3 (Phase 2 strict): hard issue -> entity/group discard + ner_skip_reason recorded
    - L4 (suspicious entity name): chain `_and_*` / length 100+ -> discard
    - L5 (dead column activation): ner_method / ner_skip_reason -> events column INSERT
    - L6 (entity grounding): 50%+ of entity name tokens must exist in raw_text
    """
    domain = domain_config.domain

    # Producer segment milestone (all 4 producers pass through this helper,
    # so a single logger call covers doc_batch / novel / doxygen / vcs).
    logger.info("producer_segment_start", extra={
        "source_provider": source_provider,
        "source_id": source_id,
        "is_first_segment": is_first_segment,
    })

    # NER output validation (Phase 2 strict -- discard entities on hard issue).
    # If raw_segment_text is provided, L1 (input_too_short) + L6 (grounding) also apply.
    validation_issues = _validate_ner_output(
        ner_output, domain_config, raw_text=raw_segment_text,
    )

    # L6 -- entity grounding validation
    grounding_issues: list[tuple[str, float]] = []
    if raw_segment_text and ner_output.entities:
        grounding_issues = _validate_entity_grounding(ner_output, raw_segment_text)
        # All entities grounding-failed -> hard issue (suspected full hallucination)
        if grounding_issues and len(grounding_issues) == len(ner_output.entities):
            validation_issues.append("all_entities_low_grounding")
        elif grounding_issues:
            # Partial fail -- record as soft issue (partial entity removal is
            # optional; simplified to all-or-nothing here; soft mode candidate)
            validation_issues.append(
                f"partial_low_grounding_{len(grounding_issues)}/"
                f"{len(ner_output.entities)}"
            )

    # L3 -- Phase 2 strict classification
    skip_entities, ner_skip_reason = _classify_ner_issues(validation_issues)

    if validation_issues:
        # Location hint -- built from path_parts / source_path /
        # segment_index / section_heading populated by the producer in inline_meta.
        src = _format_source_locator(ner_output.content.inline_meta)
        logger.warning("ner_validation_issues", extra={
            "domain": domain,
            "issues": list(validation_issues),
            "summary_preview": (ner_output.event.summary or "")[:60],
            "entity_count": len(ner_output.entities),
            "grounding_issues": [
                {"name": n[:60], "score": round(s, 3)}
                for n, s in grounding_issues[:5]  # limit to 5 for log readability
            ],
            "source_locator": src,
            "ner_skip_reason": ner_skip_reason,
        })
    # Suspected silent self-censorship (NER returned 0 entities but summary has normal length)
    if (
        not ner_output.entities
        and ner_output.event.summary
        and len(ner_output.event.summary) >= 50
    ):
        logger.warning("ner_zero_entities", extra={
            "domain": domain,
            "summary_len": len(ner_output.event.summary),
            "source_id": source_id,
        })

    # Object Storage
    storage_uri = object_storage.put_text(
        text=ner_output.content.data,
        content_type=ner_output.content.content_type,
        domain=domain,
        hint=ner_output.event.summary[:32],
    )

    # Content Store
    vector_id = new_vector_id()
    # Propagate validation_issues to content_store so that post-hoc
    # graph JOIN queries (events JOIN content_store) can filter suspect
    # events.  Other flags in NEROutput.content.inline_meta (e.g.
    # divider_failed) are included via the spread automatically.
    content_inline_meta = {
        **ner_output.content.inline_meta,
        "event_summary": ner_output.event.summary,
        "timestamp": ner_output.event.timestamp,
    }
    if validation_issues:
        content_inline_meta["ner_validation_issues"] = list(validation_issues)
    content_id = content.save(
        domain=domain,
        vector_id=vector_id,
        content_type=ner_output.content.content_type,
        storage_uri=storage_uri,
        inline_meta=content_inline_meta,
    )

    # Embedding
    text = embed_text_fn(ner_output) if embed_text_fn else _default_embed_text(ner_output)
    vector = embedding_client.embed(text)

    # L3 -- groups are unaffected by LLM hallucination (producer fills
    # them from path_parts) -> always loaded.  Only entity loading
    # (which depends on entity/group_names) is skipped.
    # Staging: group / entity / event
    for level, gname in enumerate(ner_output.groups):
        discriminator: str | None = None
        if "::vcs/" in gname:
            discriminator = "vcs"
        elif "::db/" in gname:
            discriminator = "db"
        elif "::memory/" in gname:
            discriminator = "memory"
        parent_name = ner_output.groups[level - 1] if level > 0 else None
        writer.write_group(
            StagedGroup(
                name=gname, level=level,
                discriminator=discriminator, parent_name=parent_name,
            )
        )

    entity_types: dict[str, str] = {}
    entity_names: list[str] = []
    if skip_entities:
        # L3 -- hard issue -> discard entities.  Event itself is still
        # loaded (manifest + summary searchable).  Stage with
        # entity_names = [] / entity_types = {}.
        logger.warning("bp61_entities_skipped", extra={
            "domain": domain,
            "ner_skip_reason": ner_skip_reason,
            "dropped_count": len(ner_output.entities),
            "source_locator": _format_source_locator(ner_output.content.inline_meta),
        })
    else:
        for ne in ner_output.entities:
            entity_names.append(ne.name)
            entity_types[ne.name] = ne.type
            writer.write_entity(
                StagedEntity(
                    name=ne.name, type=ne.type,
                    first_seen_event_ref=local_ref,
                )
            )

    event_inline_meta = dict(ner_output.content.inline_meta)
    if validation_issues:
        event_inline_meta["ner_validation_issues"] = list(validation_issues)
    # Store grounding scores in inline_meta (for post-hoc analysis)
    if grounding_issues:
        event_inline_meta["grounding_issues"] = [
            {"name": n, "score": round(s, 3)} for n, s in grounding_issues
        ]
    if skip_entities:
        event_inline_meta["entities_dropped_by_bp61"] = True
    # Merge DataOwner.to_columns() 5 fields into inline_meta.
    # Loader propagates via graph.add_event at INSERT time.
    # Skipped when data_owner is not specified (loader default 'public').
    if data_owner is not None:
        for k, v in data_owner.to_columns().items():
            # Do not overwrite existing inline_meta values (user-explicit takes precedence).
            event_inline_meta.setdefault(k, v)
    # Timestamp normalisation -- when the LLM responds with a placeholder
    # ("null"/"None"), partial timestamp ("2026", "2026-04"), or free-form
    # text, the value would be stuck in staging and cause a timestamptz
    # cast failure at LOAD time.  Only ISO 8601-validated values are kept.
    _ts_norm: str = ""
    try:
        from k2g.db_store.postgres.graph import _normalize_timestamp
        _ts_chk = _normalize_timestamp(ner_output.event.timestamp)
        if _ts_chk:
            _ts_norm = _ts_chk
    except Exception:  # noqa: BLE001
        # Fallback for environments without postgres -- simple placeholder normalisation
        _raw = ner_output.event.timestamp
        if _raw and str(_raw).strip().lower() not in {
            "", "null", "none", "undefined", "n/a",
        }:
            _ts_norm = str(_raw).strip()
    event = StagedEvent(
        local_ref=local_ref,
        event_id=event_id,
        prev_local_ref=prev_local_ref,
        domain=domain,
        timestamp=_ts_norm,
        summary=ner_output.event.summary or "",
        content_id=content_id,
        vector_id=vector_id,
        storage_uri=storage_uri,
        content_type=ner_output.content.content_type,
        entity_names=entity_names,
        entity_types=entity_types,
        group_names=list(ner_output.groups),
        inline_meta=event_inline_meta,
        # Source Lineage (None default preserves existing path)
        source_provider=source_provider,
        source_id=source_id,
        source_locator=dict(source_locator) if source_locator else {},
        raw_document_content=raw_document_content,
        is_first_segment=is_first_segment,
        # NER metadata propagation (activating dead columns)
        ner_method=ner_method,
        ner_skip_reason=ner_skip_reason,
    )
    writer.write_event(event, embedding=vector)

    logger.info("producer_segment_complete", extra={
        "source_provider": source_provider,
        "source_id": source_id,
        "entity_count": len(entity_names),
        "group_count": len(ner_output.groups),
        "had_validation_issues": bool(validation_issues),
        "ner_method": ner_method,
        "ner_skip_reason": ner_skip_reason,
    })
    return ProduceResult(
        success=True,
        local_ref=local_ref,
        entity_count=len(entity_names),
        group_count=len(ner_output.groups),
    )


def _default_embed_text(ner_output: "NEROutput") -> str:
    """`data+entities` default embedding text. Doxygen structured etc. override in the producer."""
    entity_names = ", ".join(e.name for e in ner_output.entities)
    content_data = ner_output.content.data
    if entity_names:
        return f"[{entity_names}] {content_data}"
    return content_data


# ---------------------------------------------------------------------------
# NER-based load -- staging -> DbStore
# ---------------------------------------------------------------------------

def load_staged_event(
    staged: "StagedEvent",
    *,
    embedding: "np.ndarray",
    domain_config: "DomainConfig",
    db: "DbStore",
    prev_event_id: str | None = None,
) -> tuple[bool, str, str | None]:
    """Load a single StagedEvent into Graph + Vector.

    Returns:
        (success, event_id_or_empty, error_or_None)

    Same semantics as legacy IngestionPipeline.load_from_stage.
    group_cache is managed by the producer (state persists across
    calls).  Simplified here -- MERGE every group each time
    (graph backend's link_or_create_group is idempotent).
    """
    domain = staged.domain
    graph = db.graph
    vector = db.vector
    content = db.content

    try:
        # Group MERGE
        group_ids: list[str] = []
        prev_gid: str | None = None
        for level, gname in enumerate(staged.group_names):
            if "::path/" in gname:
                gid = graph.register_path_group_with_ancestors(gname, domain=domain)
            else:
                discriminator: str | None = None
                if "::vcs/" in gname:
                    discriminator = "vcs"
                elif "::db/" in gname:
                    discriminator = "db"
                elif "::memory/" in gname:
                    discriminator = "memory"
                gid = graph.link_or_create_group(
                    name=gname, level=level, domain=domain,
                    parent_id=prev_gid,
                    summary=" > ".join(staged.group_names[: level + 1]),
                    discriminator=discriminator,
                )
            group_ids.append(gid)
            prev_gid = gid

        # Vector upsert (initial metadata)
        initial_meta = VectorMetadata(
            entity_ids=[], event_id="",
            group_ids=group_ids, content_id=staged.content_id,
            domain=domain,
        )
        vector.upsert(staged.vector_id, embedding, initial_meta)

        # Graph event + entity + participated_in
        # Extract owner 5-column set from staged.inline_meta ->
        # populate events table owner columns at graph.create_event INSERT.
        # Unspecified -> NULL / visibility default 'public'.
        _imeta = dict(staged.inline_meta or {})
        order_index = graph.get_order_index(domain)
        event_id = graph.create_event(
            vector_id=staged.vector_id,
            domain=domain,
            timestamp=staged.timestamp,
            order_index=order_index,
            summary=staged.summary,
            owner_id=_imeta.get("owner_id"),
            org_id=_imeta.get("org_id"),
            visibility=_imeta.get("visibility") or "public",
            acl_json=_imeta.get("acl_json"),
            share_group_id=_imeta.get("share_group_id"),
            # NER metadata propagation (activating dead columns)
            ner_method=staged.ner_method,
            ner_skip_reason=staged.ner_skip_reason,
        )
        if prev_event_id:
            graph.link_sequential_next(prev_event_id, event_id, source="chunk_order")
        # If staged.group_kinds is present, apply 'contains'/'refers' branching.
        # None -> all default 'contains' (doc/code producer compat).
        kinds = staged.group_kinds
        for idx, gid in enumerate(group_ids):
            kind = (
                kinds[idx]
                if kinds is not None and idx < len(kinds)
                else "contains"
            )
            graph.link_event_member_of(event_id, gid, kind=kind)

        entity_ids: list[str] = []
        for name in staged.entity_names:
            etype = staged.entity_types.get(name, "unknown")
            eid = graph.link_or_create_entity(name=name, domain=domain, type=etype)
            graph.link_participated_in(eid, event_id)
            entity_ids.append(eid)

        # entity_connection -- pairwise entity co-occurrence for this event (a < b normalised).
        # Skip outlier events to avoid N(N-1)/2 explosion.  Upper bound is the setting
        # community_max_entities_per_event (default 50), shared with the MCP remember path.
        from k2g.core.config import get_settings
        _max_ent = get_settings().community_max_entities_per_event
        if 2 <= len(entity_ids) < _max_ent:
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    graph.upsert_entity_connection(entity_ids[i], entity_ids[j], event_id)

        # Vector metadata update (event_id / entity_ids)
        updated_meta = VectorMetadata(
            entity_ids=entity_ids, event_id=event_id,
            group_ids=group_ids, content_id=staged.content_id,
            domain=domain,
        )
        vector.upsert(staged.vector_id, embedding, updated_meta)

        return True, event_id, None
    except Exception as exc:
        logger.error("load_staged_event failed ref=%s: %s", staged.local_ref, exc)
        # Postgres transaction isolation -- one INSERT failure puts the connection
        # in abort state, causing all subsequent calls to cascade-fail with
        # 'current transaction is aborted'.  Rollback each backend's connection
        # so the next event enters a fresh transaction.
        for backend in (graph, vector, content):
            conn = getattr(backend, "_conn", None)
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        try:
            content.mark_orphan(staged.content_id)
        except Exception:
            pass
        return False, "", f"{type(exc).__name__}: {exc}"


def load_staged_events_batch(
    staged_events: list["StagedEvent"],
    *,
    embeddings: list["np.ndarray"],
    domain_config: "DomainConfig",
    db: "DbStore",
    prev_event_id: str | None = None,
    manifest_store: "Any | None" = None,
    manifest_segments: list[dict] | None = None,
) -> tuple[list[bool], list[str], list[str | None]]:
    """File-level batch load (bulk version of single-row load_staged_event).

    Only group MERGE runs per-event (group_cache is identical within a
    file -- link_or_create_group is idempotent).  Events / entities /
    participated_in / connection / sequential_next / member_of / manifest
    accumulate in a BulkInsertPack -> single db.session() transaction.

    Equivalence -- all graph state must be *logically identical* to the
    single-row call:
      - entity MERGE: (name, domain) key, same as link_or_create_entity
      - participated_in: ON CONFLICT DO NOTHING
      - entity_connection: pair count accumulation (a < b normalised)
      - sequential_next: prev_event_id (last of prior file or staged.prev_local_ref)
      - vector.upsert: initial + updated, 2 calls (second fills entity_ids)

    Args:
        staged_events: StagedEvent list within a file (order = chunk order).
        embeddings: np.ndarray list of same length.
        prev_event_id: Previous event_id *outside* this file (prev for the first event).
        manifest_store, manifest_segments: Manifest load options.

    Returns:
        (success_flags, event_ids, error_messages) -- same length.
        On failure: success_flags[i]=False, event_ids[i]="", error_messages[i]!=None.
        The entire batch is one transaction so any segment failure fails all.
    """
    from k2g.producer._bulk_pack import BulkInsertPack

    n = len(staged_events)
    if n == 0:
        return [], [], []
    if len(embeddings) != n:
        raise ValueError(
            f"load_staged_events_batch: embeddings length {len(embeddings)} != events {n}"
        )

    graph = db.graph
    content = db.content
    domain = staged_events[0].domain

    # 1. Group MERGE (per-event — idempotent; caching group_ids within a file
    #    is possible when group_names are identical, but for simplicity always
    #    call link_or_create_group on each event.)
    per_event_groups: list[tuple[list[str], list[str | None]]] = []
    try:
        for staged in staged_events:
            group_ids: list[str] = []
            prev_gid: str | None = None
            for level, gname in enumerate(staged.group_names):
                if "::path/" in gname:
                    gid = graph.register_path_group_with_ancestors(gname, domain=domain)
                else:
                    discriminator: str | None = None
                    if "::vcs/" in gname:
                        discriminator = "vcs"
                    elif "::db/" in gname:
                        discriminator = "db"
                    elif "::memory/" in gname:
                        discriminator = "memory"
                    gid = graph.link_or_create_group(
                        name=gname, level=level, domain=domain,
                        parent_id=prev_gid,
                        summary=" > ".join(staged.group_names[: level + 1]),
                        discriminator=discriminator,
                    )
                group_ids.append(gid)
                prev_gid = gid
            per_event_groups.append((group_ids, list(staged.group_kinds or [])))
    except Exception as exc:
        logger.error(
            "load_staged_events_batch group MERGE failed domain=%s: %s", domain, exc
        )
        # rollback connection
        conn = getattr(graph, "_conn", None)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        return (
            [False] * n,
            [""] * n,
            [f"{type(exc).__name__}: {exc}"] * n,
        )

    # 2. Accumulate into BulkInsertPack
    pack = BulkInsertPack()
    base_order_index = graph.get_order_index(domain)

    # local_ref → event_index mapping (for sequential_next prev_local_ref matching)
    local_ref_to_idx: dict[str, int] = {}

    for i, staged in enumerate(staged_events):
        _imeta = dict(staged.inline_meta or {})
        # Initial vector metadata (event_id empty; filled in the subsequent update)
        initial_meta = VectorMetadata(
            entity_ids=[], event_id="",
            group_ids=per_event_groups[i][0], content_id=staged.content_id,
            domain=domain,
        )
        ev_idx = pack.add_event(
            domain=domain,
            vector_id=staged.vector_id,
            timestamp=staged.timestamp,
            order_index=base_order_index + i,
            summary=staged.summary,
            embedding=embeddings[i],
            vector_metadata=initial_meta,
            owner_id=_imeta.get("owner_id"),
            org_id=_imeta.get("org_id"),
            visibility=_imeta.get("visibility") or "public",
            acl_json=_imeta.get("acl_json"),
            share_group_id=_imeta.get("share_group_id"),
            # NER metadata propagation (activates dead columns)
            ner_method=staged.ner_method,
            ner_skip_reason=staged.ner_skip_reason,
            # Pre-assigned deterministic event_id (manifest dedup). None means new UUID.
            event_id=staged.event_id,
        )
        local_ref_to_idx[staged.local_ref] = ev_idx

        # member_of
        group_ids, kinds = per_event_groups[i]
        for j, gid in enumerate(group_ids):
            kind = kinds[j] if j < len(kinds) else "contains"
            pack.add_member_of(ev_idx, gid, kind=kind or "contains")

        # entities + participated_in + entity_connection
        ent_indices: list[int] = []
        for name in staged.entity_names:
            etype = staged.entity_types.get(name, "unknown")
            ent_idx = pack.add_entity(name=name, domain=domain, type=etype)
            pack.add_participated_in(ev_idx, ent_idx)
            ent_indices.append(ent_idx)
        pack.add_entity_connections_for_event(ev_idx, ent_indices)

    # sequential_next linking:
    #   Within the file: when staged.prev_local_ref refers to another staged
    #     event in the same batch, call add_sequential_next inside the pack.
    #   Across files: when the first staged.prev_local_ref is None and
    #     prev_event_id is present, link via a separate link_sequential_next
    #     call (linking to an event outside this batch).
    external_sequential: tuple[str, str] | None = None
    for i, staged in enumerate(staged_events):
        prev_ref = staged.prev_local_ref
        if not prev_ref:
            continue
        prev_idx = local_ref_to_idx.get(prev_ref)
        if prev_idx is not None:
            pack.add_sequential_next(prev_idx, i, source="chunk_order")

    # manifest segments
    if manifest_segments:
        for ms in manifest_segments:
            pack.add_manifest_segment(**ms)

    # 3. Commit
    try:
        event_ids = pack.commit(db, manifest_store=manifest_store)
    except Exception as exc:
        logger.error(
            "load_staged_events_batch commit failed domain=%s n=%d: %s", domain, n, exc
        )
        # Rollback the connection. If BulkInsertPack failed inside db.session,
        # it was already rolled back; vector backend failure needs separate handling.
        conn = getattr(graph, "_conn", None)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        # content mark_orphan
        for staged in staged_events:
            try:
                content.mark_orphan(staged.content_id)
            except Exception:  # noqa: BLE001
                pass
        return (
            [False] * n,
            [""] * n,
            [f"{type(exc).__name__}: {exc}"] * n,
        )

    # 4. Update vector metadata — fill entity_ids and event_id, then re-upsert.
    #    Equivalent to the second vector.upsert in single-row load_staged_event.
    #    Reuse BulkInsertPack.entity_id_by_index mapping (no extra DB round-trip).
    name_to_entity_id: dict[str, str] = {}
    for ent_idx, ent in enumerate(pack.entities):
        if ent_idx < len(pack.entity_id_by_index):
            name_to_entity_id[ent.name] = pack.entity_id_by_index[ent_idx]
    for i, staged in enumerate(staged_events):
        try:
            entity_ids = [
                name_to_entity_id[name]
                for name in staged.entity_names
                if name in name_to_entity_id
            ]
            updated_meta = VectorMetadata(
                entity_ids=entity_ids, event_id=event_ids[i],
                group_ids=per_event_groups[i][0], content_id=staged.content_id,
                domain=domain,
            )
            db.vector.upsert(staged.vector_id, embeddings[i], updated_meta)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "load_staged_events_batch vector metadata update failed ref=%s: %s",
                staged.local_ref, exc,
            )

    # 5. Cross-file sequential_next: link last event of prior file → first event of this file.
    if prev_event_id and event_ids:
        try:
            graph.link_sequential_next(prev_event_id, event_ids[0], source="chunk_order")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross-file sequential_next failed: %s", exc)

    return [True] * n, event_ids, [None] * n


def stream_load_by_file(
    reader,
    db: "DbStore",
    *,
    domain_config: "DomainConfig",
    desc: str,
    file_key_fn,
    on_event_success=None,
    on_post_flush=None,
    manifest_store=None,        # BP-54 Phase B-2 — manifest mark_segment_loaded
    build_id: str = "",
):
    """Shared stream/flush helper for file-level batch LOAD.

    Shared by the load() methods of doc_batch, novel, and doxygen producers.
    Uses file_key_fn to group staged events from the same file into a single
    batch and commits them in one load_staged_events_batch transaction.

    Args:
        reader: StageReader.
        db: DbStore.
        domain_config: DomainConfig.
        desc: tqdm progress bar label.
        file_key_fn: callable(staged) -> str. Events with the same key form
            one batch.
        on_event_success: callable(staged, event_id) -> None. Hook called
            after each successful event load (e.g. source lineage recording).
            Exceptions are absorbed as warnings.
        on_post_flush: callable() -> None. Hook called after each flush.
        manifest_store: BuildManifestStore. When provided, successful segment
            stage_state is promoted from 'produced' to 'loaded'. None skips
            manifest updates (legacy / standalone LOAD test).
        build_id: Manifest row matching key — typically the session_id.

    Returns:
        LoadResult.
    """
    from pathlib import Path as _Path  # noqa: F401
    from tqdm import tqdm

    report = LoadResult()
    prev_event_id_across_files: str | None = None
    total = reader.event_count() if hasattr(reader, "event_count") else 0
    pbar = tqdm(
        total=total or None, desc=desc, unit="event",
        leave=True, dynamic_ncols=True,
    )

    current_file: str | None = None
    batch_staged: list = []
    batch_embeddings: list = []
    batch_skipped: list = []

    def _flush() -> None:
        nonlocal prev_event_id_across_files, batch_staged, batch_embeddings, batch_skipped
        if not batch_staged:
            batch_skipped = []
            return
        try:
            ok_flags, event_ids, errs = load_staged_events_batch(
                batch_staged,
                embeddings=batch_embeddings,
                domain_config=domain_config,
                db=db,
                prev_event_id=prev_event_id_across_files,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("LOAD batch exception: %s", exc)
            report.fail_count += len(batch_staged)
            report.errors.append(f"batch exception: {exc}")
            pbar.update(len(batch_staged) + len(batch_skipped))
            batch_staged = []
            batch_embeddings = []
            batch_skipped = []
            return

        # Promote successful segments in the manifest (successful ones only).
        manifest_rows: list[dict] = []
        for i, ok in enumerate(ok_flags):
            if ok:
                report.success_count += 1
                report.event_ids.append(event_ids[i])
                if on_event_success is not None:
                    try:
                        on_event_success(batch_staged[i], event_ids[i])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "on_event_success hook failed (ref=%s): %s",
                            batch_staged[i].local_ref, exc,
                        )
                # Accumulate rows for manifest.mark_segments_loaded_bulk.
                # Schema branching logic is absorbed by staging.event_keys helper.
                # Fall back to local_ref when file_path / segment_key are absent
                # (exception flow; cosmetic loss on manifest match failure).
                if manifest_store is not None and build_id:
                    keys = extract_event_keys(batch_staged[i])
                    manifest_rows.append({
                        "domain": batch_staged[i].domain,
                        "file_path": keys.file_path or batch_staged[i].local_ref,
                        "segment_key": keys.segment_key or batch_staged[i].local_ref,
                        "build_id": build_id,
                        "event_id": event_ids[i],
                    })
            else:
                report.fail_count += 1
                err_msg = errs[i] or "(no error message — ok_flags=False without errs)"
                if errs[i]:
                    report.errors.append(errs[i])
                # Log silent failures explicitly so the cause is visible
                # (previously only accumulated in the errors list, no logger output).
                logger.warning(
                    "LOAD segment failed: ref=%s file=%s seg_idx=%s -- %s",
                    batch_staged[i].local_ref,
                    (batch_staged[i].inline_meta or {}).get("file_path", "?"),
                    (batch_staged[i].inline_meta or {}).get("segment_index", "?"),
                    err_msg,
                )

        # Promote only successful segments in the manifest (record successes only).
        if manifest_rows and manifest_store is not None:
            try:
                manifest_store.mark_segments_loaded_bulk(manifest_rows)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mark_segments_loaded_bulk failed: %s "
                    "(graph load succeeded — manifest is stale, "
                    "next build can backfill)",
                    exc,
                )

        # File-level promotion: call mark_file_loaded after every flush.
        # mark_file_loaded uses a NOT EXISTS subquery and only UPDATEs when
        # all segments are loaded — idempotent, safe to call again for the
        # same file in a subsequent batch.
        # Use the schema-aware normalised file_path from manifest_rows[0]
        # instead of the file_key_fn result (current_file), because
        # file_key_fn may fall back to local_ref for fqn-only schemas (e.g.
        # doxygen) causing a mismatch with the manifest.
        if (
            manifest_store is not None
            and manifest_rows
        ):
            mark_file_path = manifest_rows[0]["file_path"]
            try:
                manifest_store.mark_file_loaded(
                    domain=manifest_rows[0]["domain"],
                    file_path=mark_file_path,
                    build_id=build_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mark_file_loaded failed: %s "
                    "(graph load succeeded — file_manifest is stale)",
                    exc,
                )

        for i in range(len(ok_flags) - 1, -1, -1):
            if ok_flags[i]:
                prev_event_id_across_files = event_ids[i]
                break

        pbar.update(len(batch_staged) + len(batch_skipped))
        batch_staged = []
        batch_embeddings = []
        batch_skipped = []
        if on_post_flush is not None:
            try:
                on_post_flush()
            except Exception:  # noqa: BLE001
                pass

    # When manifest_store is provided, iterate only active produced segments.
    # This prevents re-loading staging events already marked 'loaded' in the graph.
    # Without manifest_store (None), iterate all events (legacy / non-build tools).
    if manifest_store is not None and getattr(reader, "domain", None):
        from k2g.staging.loader import iter_active_produced_events
        events_iter = iter_active_produced_events(reader.domain, manifest_store)
    else:
        events_iter = reader.iter_events()

    try:
        for staged in events_iter:
            fkey = file_key_fn(staged)
            if current_file is None:
                current_file = fkey
            if fkey != current_file:
                _flush()
                current_file = fkey

            inline = staged.inline_meta or {}
            seg_idx = inline.get("segment_index", "?")
            logger.info("[LOAD] ref=%s src=%s seg=%s", staged.local_ref, fkey, seg_idx)
            pbar.set_postfix_str(f"ref={staged.local_ref}", refresh=False)

            embedding = reader.get_embedding(staged.local_ref)
            if embedding is None:
                report.fail_count += 1
                report.errors.append(f"embedding missing ref={staged.local_ref}")
                logger.warning(
                    "LOAD embedding missing: ref=%s file=%s",
                    staged.local_ref, fkey,
                )
                batch_skipped.append(staged.local_ref)
                continue
            batch_staged.append(staged)
            batch_embeddings.append(embedding)
        _flush()
    finally:
        pbar.close()

    # Summary of failures at the end of the stream.
    if report.fail_count > 0:
        logger.warning(
            "LOAD complete -- success=%d fail=%d errors_recorded=%d. First 10 reasons:",
            report.success_count, report.fail_count, len(report.errors),
        )
        for idx, err in enumerate(report.errors[:10]):
            logger.warning("  [%d] %s", idx + 1, err)
        if len(report.errors) > 10:
            logger.warning(
                "  ... (%d more; full list retained in report.errors)",
                len(report.errors) - 10,
            )

    # End-of-LOAD file_manifest backfill. Because mark_file_loaded is only
    # called inside the flush flow, files that had no produced segments in
    # this LOAD run (all segments already loaded from a prior build) may
    # still show a cosmetic 'produced' status. The NOT EXISTS subquery in
    # backfill_file_loaded promotes only files whose every active segment is
    # loaded — idempotent.
    if manifest_store is not None and getattr(reader, "domain", None):
        try:
            n_backfilled = manifest_store.backfill_file_loaded(reader.domain)
            if n_backfilled > 0:
                logger.info(
                    "backfill_file_loaded: domain=%s files_promoted=%d",
                    reader.domain, n_backfilled,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backfill_file_loaded failed: %s "
                "(graph load succeeded — file_manifest is stale)",
                exc,
            )

    return report


__all__ = [
    "produce_ner_output",
    "load_staged_event",
    "load_staged_events_batch",
    "stream_load_by_file",
]
