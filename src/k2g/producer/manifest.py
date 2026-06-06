"""ManifestProducer: ingest AI-authored JSON manifests without making LLM calls.

The sole difference from ``DocBatchProducer``: K2G trusts the AI-produced
summary and NER output carried by the manifest instead of re-extracting them
via LLM (``ner_method="caller_provided"``). Only the NER step is bypassed;
the layers below (embedding, content, event, ``chunk_order`` chain,
entity_connection, atomic load) reuse the existing
``produce_ner_output`` + ``stream_load_by_file`` machinery.

Group/tag resolution uses the **same shared helper as the MCP ``remember``
path** (`k2g.memory.save_context`), applied in the post-load stage, so
manifest ingestion produces the same group/tag structure as an ``mw save``
call (save-context parity). The producer's ``auto_groups``/``domain_config``
group logic is not used on this path.

Data flow:
    discover  load manifest.json -> validate complete:true -> yield 1 unit
    produce   assemble NEROutput directly for each item (0 NER calls) ->
              produce_ner_output (embedding=summary, prev_local_ref for
              chunk_order chain)
    load      stream_load_by_file (atomic commit per file) -> post-load:
              shared helper applies group/tag + forced save_tags
              event_member_of; manifest.prev_event_id extends the
              cross-file chain.

Embedding/grounding note: the grounding filter in produce_ner_output
(L1/L6) only discards entities when ``raw_segment_text`` is provided.
Manifests follow the AI-trust semantics of a one-shot ``mweft_remember``
call (grounding=0), so ``raw_segment_text`` is not passed and caller-
provided entities are preserved. Embedding text uses ``summary`` only
(not content+entities), matching the remember path.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from k2g.producer._shared import produce_ner_output, stream_load_by_file
from k2g.producer.base import LoadResult, ProduceResult, Scope
from k2g.producer.manifest_schema import ParsedManifest, load_manifest

if TYPE_CHECKING:
    from k2g.core.config import DomainConfig
    from k2g.db_store import DbStore
    from k2g.db_store.content_backend import ContentBackend
    from k2g.db_store.object_backend import ObjectBackend
    from k2g.embedding.client import EmbeddingClient
    from k2g.staging import StageReader, StageWriter

logger = logging.getLogger(__name__)

# file_key for manifest events — all items share the same key so that
# one manifest maps to one atomic commit batch (all-or-nothing).
_MANIFEST_FILE_KEY = "manifest"


def _manifest_embed_text(ner_output) -> str:
    """Embed using summary (matching the remember path) — falls back to content.data."""
    return (ner_output.event.summary or "").strip() or ner_output.content.data


def deterministic_event_id(domain: str, working_folder: str | None, content: str) -> str:
    """Deterministic event_id = evt_ + sha256(domain:working_folder:content)[:32].

    Re-running the same manifest produces the same id, so the bulk-load
    ON CONFLICT(id) DO NOTHING clause prevents duplicate events and chain
    entries. The id is content-based and therefore stable across
    summary/ordering changes. Stable ids also keep the local_ref->event_id
    mapping consistent across partial re-runs, preserving chain integrity.
    """
    raw = f"{domain}:{working_folder or ''}:{content}"
    return "evt_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class ManifestUnit:
    """Single manifest processing unit — parsed/validated items plus chain metadata."""

    manifest: ParsedManifest


class ManifestProducer:
    """AI manifest (JSON) Producer implementation (kind="manifest").

    Args:
        embedding_client: Local BGE-M3 (not an LLM). Used for summary embeddings.
        content / object_storage: content/object backends for produce_ner_output.
        graph: Graph store for post-load group/tag attachment.
        settings: Used to resolve save-context (env domain/save-root/save_tags).
        manifest_path: Path to the ``*.manifest.json`` file to ingest.
    """

    kind: str = "manifest"

    def __init__(
        self,
        *,
        embedding_client: "EmbeddingClient",
        content: "ContentBackend",
        object_storage: "ObjectBackend",
        graph: Any,
        settings: Any,
        manifest_path: str,
        source_hash: str | None = None,
        working_folder_override: str | None = None,
        curated_tags: list[str] | None = None,
    ) -> None:
        self._embedding = embedding_client
        self._content = content
        self._object = object_storage
        self._graph = graph
        self._settings = settings
        self._manifest_path = manifest_path
        # Document-build (curated) mode. CLI ``--working-folder`` / ``--tag``.
        # working_folder_override: dedicated root for documents (avoids
        # polluting the env ai_memory root).
        # When curated_tags is not None the producer runs in curated mode:
        #   - autotag (category cascade) is disabled (category=None)
        #   - these user-confirmed tags are attached as forced tags instead of
        #     inheriting the env session's forced tags.
        self._working_folder_override = working_folder_override
        self._curated_tags = curated_tags
        # Source-bytes hash passed in by the CLI (--source flag).
        # When present, activates file-level source-hash mode: change
        # detection uses the *immutable source* hash rather than
        # per-chunk hashes (which are unstable due to LLM chunking
        # variance). None = per-chunk (--incremental) or dedup-only.
        self._source_hash = source_hash
        self._source_mode = False   # determined during produce()
        self._domain_config: "DomainConfig | None" = None
        # Populated by produce() — per-event tag for post-load group attachment.
        # Deterministic event_id -> tag mapping (order-independent, safe across
        # LOAD order changes in incremental mode).
        self._produced_tag_by_event_id: dict[str, str | None] = {}
        # Populated by produce() — manifest segment rows to promote to
        # 'loaded' state after a successful LOAD.
        self._produced_manifest_rows: list[dict[str, Any]] = []
        self._manifest: ParsedManifest | None = None

    # ------------------------------------------------------------------
    def set_domain_config(self, domain_config: "DomainConfig") -> None:
        self._domain_config = domain_config

    # ------------------------------------------------------------------
    def discover(
        self, config: "DomainConfig", scope: Scope,
    ) -> Iterator[ManifestUnit]:
        """Load and validate the manifest (complete:true gate, all-or-nothing). Yields 1 unit.

        On validation failure, raises :class:`ManifestError` so the
        orchestrator/CLI can abort before produce() (no partial ingest).
        """
        manifest = load_manifest(self._manifest_path)
        self._manifest = manifest
        logger.info(
            "manifest loaded: path=%s items=%d source=%s working_folder=%s",
            self._manifest_path, len(manifest.items), manifest.source,
            manifest.working_folder,
        )
        yield ManifestUnit(manifest=manifest)

    # ------------------------------------------------------------------
    def produce(
        self,
        unit: ManifestUnit,
        *,
        writer: "StageWriter",
        updater: Any | None = None,
    ) -> ProduceResult:
        """Assemble NEROutput directly for each item and call produce_ner_output (0 NER calls).

        All-or-nothing: if produce_ner_output fails or raises for any item,
        returns success=False for the whole batch so the CLI aborts before
        entering load(). Staging uses a temporary session, so no DB
        corruption can occur.
        """
        from k2g.core.models import NERContent, NEREntity, NEREvent, NEROutput

        domain_config = self._domain_config
        if domain_config is None:
            return ProduceResult(
                success=False,
                error="domain_config not injected — call set_domain_config() first",
            )
        manifest = unit.manifest
        self._produced_tag_by_event_id = {}
        self._produced_manifest_rows = []
        domain = domain_config.domain
        working_folder = self._resolve_working_folder()

        # Incremental mode — active only when updater is injected and
        # manifest.file_path (original document identity) is set.
        # Enables file-level hash skip + segment-level hash skip/supersede.
        seg_action_map: dict[int, Any] = {}
        file_hash: str | None = None
        file_key = manifest.file_path
        self._source_mode = (
            updater is not None and bool(file_key)
            and self._source_hash is not None
        )
        if self._source_hash is not None and not file_key:
            logger.warning(
                "--source provided but manifest has no file_path — source-hash "
                "mode disabled (deterministic event_id dedup only). "
                "Add file_path to the manifest to enable source-hash mode.",
            )

        # Source-hash mode (recommended) — file-level change detection using the
        # original-bytes hash. Avoids per-chunk hashes and is therefore robust
        # against LLM chunking variance. Unchanged -> skip the entire file;
        # changed -> stage all items (unchanged items auto-dedup via deterministic
        # event_id + ON CONFLICT, resulting in 0 new events).
        if self._source_mode:
            mstore = getattr(updater, "_manifest", None)
            if mstore is not None and mstore.check_file_hash(
                domain, file_key, self._source_hash,
            ):
                logger.info("manifest source-hash skip_file (source unchanged): %s", file_key)
                return ProduceResult(success=True, skipped=True)
            # Source changed -> stage all items. record_file is called after LOAD.

        incremental = (
            updater is not None and bool(file_key) and not self._source_mode
        )
        if incremental:
            file_content = "\n\n".join(it.content for it in manifest.items)
            segments = [
                {"text": it.content, "order_index": i, "meta": {}}
                for i, it in enumerate(manifest.items)
            ]
            try:
                check = updater.check_and_prepare(
                    domain=domain, file_path=file_key,
                    file_content=file_content, segments=segments,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "manifest updater.check_and_prepare failed (%s) — "
                    "falling back to full ingest: %s",
                    file_key, exc,
                )
                incremental = False
            else:
                file_hash = check.file_hash
                if check.action == "skip_file":
                    logger.info("manifest incremental skip_file: %s", file_key)
                    return ProduceResult(success=True, skipped=True)
                for act in check.segment_actions:
                    seg_action_map[act.segment_index] = act

        prev_local_ref: str | None = None
        last_result = ProduceResult(success=True)
        success_cnt = 0
        skipped_cnt = 0
        had_failure = False

        for idx, item in enumerate(manifest.items):
            # Incremental segment skip (unchanged item).
            act = seg_action_map.get(idx) if incremental else None
            if act is not None and act.action == "skip":
                skipped_cnt += 1
                continue

            # In incremental mode, embed the matching keys (file_path/segment_key/
            # segment_index) into inline_meta via build_inline_meta so LOAD
            # can promote the segment state.
            if incremental and act is not None:
                from k2g.staging.event_builder import (
                    StagingEventBuildKeys,
                    build_inline_meta,
                )
                inline_meta = build_inline_meta(
                    StagingEventBuildKeys(
                        file_path=file_key,
                        segment_key=act.segment_key,
                        segment_index=idx,
                        extra={"schema": "manifest", "source": "manifest_ingest"},
                    ),
                    schema="doc_batch",
                )
            else:
                inline_meta = {
                    "schema": "manifest",
                    "manifest_item_index": idx,
                    "source": "manifest_ingest",
                }

            try:
                entities = [
                    NEREntity(name=e["name"], type=(e.get("type") or "unknown"))
                    for e in item.entities
                ]
                ner_output = NEROutput(
                    entities=entities,
                    event=NEREvent(summary=item.summary, timestamp=item.timestamp),
                    groups=[],   # group/tag resolved in post-load via shared helper
                    content=NERContent(
                        content_type="text/plain",
                        data=item.content,
                        inline_meta=inline_meta,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — pydantic or other assembly error
                logger.error("manifest item[%d] NEROutput assembly failed: %s", idx, exc)
                return ProduceResult(
                    success=False, error=f"item[{idx}] assembly failed: {exc}",
                )

            local_ref = writer.next_ref()
            # Pre-assign deterministic event_id (content-based) for re-run dedup.
            ev_id = deterministic_event_id(domain, working_folder, item.content)
            try:
                res = produce_ner_output(
                    ner_output,
                    domain_config,
                    writer=writer,
                    local_ref=local_ref,
                    prev_local_ref=prev_local_ref,
                    content=self._content,
                    object_storage=self._object,
                    embedding_client=self._embedding,
                    embed_text_fn=_manifest_embed_text,
                    # remember parity — trust caller-provided NER (no grounding applied).
                    ner_method="caller_provided",
                    event_id=ev_id,
                )
            except Exception as exc:  # noqa: BLE001 — embedding/object/content failure
                logger.error("manifest item[%d] produce failed: %s", idx, exc)
                return ProduceResult(
                    success=False, error=f"item[{idx}] produce failed: {exc}",
                )
            if not res.success:
                return ProduceResult(
                    success=False,
                    error=f"item[{idx}] produce_ner_output failed",
                )
            success_cnt += 1
            prev_local_ref = local_ref
            last_result = res
            # Per-item tag falls back to the manifest-level tag.
            # Stored by deterministic event_id for order-independent lookup.
            self._produced_tag_by_event_id[ev_id] = item.tag or manifest.tag

            # Incremental manifest record — the deterministic event_id is already
            # known at produce time (LOAD uses ON CONFLICT with this id), so ev_id
            # is recorded directly. vector_id temporarily uses the staging
            # local_ref (same pattern as DocBatch).
            if incremental and act is not None:
                try:
                    updater.record_result(
                        domain=domain, file_path=file_key,
                        segment_key=act.segment_key, segment_index=act.segment_index,
                        segment_hash=act.segment_hash, vector_id=local_ref,
                        event_id=ev_id, old_vector_id=getattr(act, "old_vector_id", None),
                    )
                    # Accumulate rows to promote 'produced'->'loaded' after LOAD succeeds.
                    self._produced_manifest_rows.append({
                        "domain": domain, "file_path": file_key,
                        "segment_key": act.segment_key, "build_id": "",
                        "event_id": ev_id,
                    })
                except Exception as exc:  # noqa: BLE001
                    logger.warning("manifest updater.record_result failed: %s", exc)

        # Finalize incremental file record — only when there were no failures.
        if incremental and file_hash is not None and not had_failure:
            try:
                updater.finalize_file(
                    domain=domain, file_path=file_key,
                    file_hash=file_hash, segment_count=success_cnt,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("manifest updater.finalize_file failed: %s", exc)

        # All segments skipped (0 new events) -> return skipped (normal incremental result).
        if incremental and success_cnt == 0 and skipped_cnt > 0:
            logger.info("manifest incremental: 0 changed segments (all skipped)")
            return ProduceResult(success=True, skipped=True)

        logger.info(
            "manifest produce complete: %d staged, %d skipped", success_cnt, skipped_cnt,
        )
        last_result.success = success_cnt > 0
        return last_result

    # ------------------------------------------------------------------
    def load(
        self,
        reader: "StageReader",
        db: "DbStore",
        *,
        domain_config: "DomainConfig",
        updater: Any | None = None,
    ) -> LoadResult:
        """Atomic commit via stream_load_by_file, then attach group/tag in post-load.

        Post-load: applies per-event group/tag + forced save_tags
        ``event_member_of`` via the shared helper (same structure as
        ``mw save``). Extends the cross-file chain using
        manifest.prev_event_id. When updater is injected, also promotes
        manifest_store segments from 'produced' to 'loaded'.
        """
        # One manifest = one batch — all staged events share the same file_key
        # (atomicity).
        # NOTE: manifest_store is NOT passed to stream_load_by_file, because
        # that path forces iter_active_produced_events for flat staging
        # (session_id=""). Manifests use per-session staging (only changed
        # items are staged), so normal iter_events is used here, and the
        # 'loaded' promotion is performed manually below.
        report = stream_load_by_file(
            reader, db,
            domain_config=domain_config,
            desc="LOAD [manifest]",
            file_key_fn=lambda staged: _MANIFEST_FILE_KEY,
        )

        if report.fail_count > 0 or not report.event_ids:
            logger.error(
                "ManifestProducer.load failed — success=%d fail=%d "
                "(skipping post-load attach)",
                report.success_count, report.fail_count,
            )
            return report

        domain = domain_config.domain
        working_folder = self._resolve_working_folder()
        event_ids = report.event_ids

        # Post-load group/tag attach — look up via deterministic event_id -> tag
        # mapping (order-independent, safe across incremental LOAD order changes).
        # Falls back to the manifest-level common tag when not in the mapping.
        common_tag = self._manifest.tag if self._manifest else None
        tag_by_id = self._produced_tag_by_event_id

        from k2g.memory.save_context import (
            attach_event_memberships,
            resolve_tag_groups,
        )

        curated = self._curated_tags is not None  # document-build (curated) mode
        if working_folder:
            for event_id in event_ids:
                # Curated mode: disable per-item autotag (category=None) and
                # attach user-confirmed tags as forced (does not inherit env
                # session forced tags). Normal mode: standard behaviour.
                category = None if curated else tag_by_id.get(event_id, common_tag)
                forced_override = self._curated_tags if curated else None
                try:
                    tagres = resolve_tag_groups(
                        self._graph,
                        domain=domain,
                        working_folder=working_folder,
                        category=category,
                        settings=self._settings,
                        forced_tags_override=forced_override,
                    )
                    attach_event_memberships(self._graph, event_id, tagres)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "manifest post-load group attach failed event=%s: %s",
                        event_id, exc,
                    )
        else:
            logger.warning(
                "working_folder not resolved (K2G_USER_MEMORY_SAVE_GROUP not set) — "
                "group/tag attach skipped. Events were loaded successfully.",
            )

        # Cross-file chain — extend from manifest.prev_event_id to the first
        # event of this batch.
        prev_event_id = self._manifest.prev_event_id if self._manifest else None
        src = self._manifest.source if self._manifest else "chunk_order"
        if prev_event_id and event_ids:
            try:
                self._graph.link_sequential_next(
                    prev_event_id, event_ids[0], source=src,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "manifest cross-file sequential_next failed (%s->%s): %s",
                    prev_event_id, event_ids[0], exc,
                )

        # Source-hash mode — record the source hash in file_manifest only after a
        # successful LOAD (on failure it stays unrecorded so the next run re-ingests).
        # Replace the active record: supersede first, then record.
        if self._source_mode and self._source_hash is not None:
            mstore = getattr(updater, "_manifest", None)
            file_key = self._manifest.file_path if self._manifest else None
            if mstore is not None and file_key:
                try:
                    mstore.supersede_file(domain=domain, file_path=file_key)
                    mstore.record_file(
                        domain=domain, file_path=file_key,
                        file_hash=self._source_hash,
                        segment_count=len(event_ids),
                        build_id=getattr(updater, "_build_id", "") or "",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "manifest source-hash record_file failed: %s "
                        "(graph load succeeded — next run will re-ingest)", exc,
                    )

        # Promote successfully loaded segments from 'produced' to 'loaded'
        # (used as the old_loaded baseline for the next build), and promote
        # the file_manifest entry as well.
        if updater is not None and self._produced_manifest_rows:
            mstore = getattr(updater, "_manifest", None)
            if mstore is not None:
                try:
                    mstore.mark_segments_loaded_bulk(self._produced_manifest_rows)
                    file_key = self._manifest.file_path if self._manifest else None
                    if file_key:
                        mstore.mark_file_loaded(domain=domain, file_path=file_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "manifest mark_loaded failed: %s "
                        "(graph load succeeded — manifest state only is stale)", exc,
                    )

        logger.info(
            "ManifestProducer.load complete: success=%d fail=%d "
            "(group attach for %d events)",
            report.success_count, report.fail_count, len(event_ids),
        )
        return report

    # ------------------------------------------------------------------
    def _resolve_working_folder(self) -> str | None:
        """Resolve working folder: CLI override -> manifest.working_folder -> env K2G_USER_MEMORY_SAVE_GROUP.

        Document-build mode passes ``--working-folder`` to use a dedicated root,
        keeping the conversational memory root (ai_memory) clean.
        The explicit override always takes priority.
        """
        from k2g.memory.save_context import resolve_working_folder

        if self._working_folder_override:
            return self._working_folder_override
        wf = self._manifest.working_folder if self._manifest else None
        resolved, _ = resolve_working_folder(self._settings, wf)
        return resolved

    # ------------------------------------------------------------------
    @property
    def manifest(self) -> ParsedManifest | None:
        return self._manifest


__all__ = ["ManifestProducer", "ManifestUnit"]
