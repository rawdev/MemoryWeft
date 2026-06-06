"""StagingLoader — streaming ndjson reader with embedding memmap.

The loader only processes domains whose produce.state == done. This file
provides the low-level reader; actual Qdrant / Graph ingestion is handled
by IngestionPipeline.load_from_stage.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

import numpy as np

from k2g.staging import paths
from k2g.staging.schema import (
    StagedEntity,
    StagedEvent,
    StagedGroup,
    StagingManifest,
)

logger = logging.getLogger(__name__)


class StagingLoadError(RuntimeError):
    """Raised when attempting to load a directory whose produce.state != done."""


class StagingLoader:
    def __init__(self, session_id: str, domain: str, *, configured_root: str = "") -> None:
        self.session_id = session_id
        self.domain = domain
        self.configured_root = configured_root
        self._embeddings: np.ndarray | None = None
        self._embeddings_index: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        state_path = paths.produce_state_path(
            self.session_id, self.domain, self.configured_root
        )
        if not state_path.exists():
            return False
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return payload.get("state") == paths.STATE_DONE

    def manifest(self) -> StagingManifest:
        path = paths.manifest_path(
            self.session_id, self.domain, self.configured_root
        )
        if not path.exists():
            raise StagingLoadError(f"manifest.json not found: {path}")
        return StagingManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def require_ready(self) -> None:
        if not self.is_ready():
            state_path = paths.produce_state_path(
                self.session_id, self.domain, self.configured_root
            )
            raise StagingLoadError(
                f"staging not ready (produce.state != done): {state_path}"
            )

    # ------------------------------------------------------------------
    # Stream readers
    # ------------------------------------------------------------------

    def iter_events(self) -> Iterator[StagedEvent]:
        path = paths.events_path(
            self.session_id, self.domain, self.configured_root
        )
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield StagedEvent.model_validate_json(line)

    def iter_entities(self) -> Iterator[StagedEntity]:
        path = paths.entities_path(
            self.session_id, self.domain, self.configured_root
        )
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield StagedEntity.model_validate_json(line)

    def iter_groups(self) -> Iterator[StagedGroup]:
        path = paths.groups_path(
            self.session_id, self.domain, self.configured_root
        )
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield StagedGroup.model_validate_json(line)

    # ------------------------------------------------------------------
    # Counts (used to derive totals for progress reporting)
    # ------------------------------------------------------------------

    def _count_lines(self, path) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def event_count(self) -> int:
        return self._count_lines(
            paths.events_path(self.session_id, self.domain, self.configured_root)
        )

    def entity_count(self) -> int:
        return self._count_lines(
            paths.entities_path(self.session_id, self.domain, self.configured_root)
        )

    def group_count(self) -> int:
        return self._count_lines(
            paths.groups_path(self.session_id, self.domain, self.configured_root)
        )

    # ------------------------------------------------------------------
    # Embedding access
    # ------------------------------------------------------------------

    def _load_embeddings(self) -> None:
        if self._embeddings is not None:
            return
        npy_path = paths.embeddings_path(
            self.session_id, self.domain, self.configured_root
        )
        idx_path = paths.embeddings_index_path(
            self.session_id, self.domain, self.configured_root
        )
        if not npy_path.exists() or not idx_path.exists():
            self._embeddings = np.empty((0, 0), dtype=np.float32)
            self._embeddings_index = {}
            return
        # Open as memmap to keep peak RSS low. Read-only.
        self._embeddings = np.load(npy_path, mmap_mode="r", allow_pickle=False)
        self._embeddings_index = json.loads(idx_path.read_text(encoding="utf-8"))

    def get_embedding(self, local_ref: str) -> np.ndarray | None:
        self._load_embeddings()
        if self._embeddings is None or self._embeddings_index is None:
            return None
        idx = self._embeddings_index.get(local_ref)
        if idx is None:
            return None
        # Copy the mmap slice to a regular array so the caller owns its lifetime.
        return np.array(self._embeddings[idx], dtype=np.float32)

    def close(self) -> None:
        """Release the embeddings.npy mmap handle. On Windows this must be
        called explicitly before renaming or deleting the staging directory."""
        emb = self._embeddings
        if emb is not None and isinstance(emb, np.memmap):
            try:
                emb._mmap.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._embeddings = None
        self._embeddings_index = None

    def __enter__(self) -> "StagingLoader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Source Lineage loader (additive)
# ---------------------------------------------------------------------------


def load_source_lineage_for_event(
    staged_event,
    provider,
    *,
    logger=None,
):
    """Store raw_document and segment_blob using the source_* fields of a
    StagedEvent.

    Call timing: before or after the events DB row is inserted (order does not
    matter when provider writes are idempotent).

    Args:
        staged_event: StagedEvent with source_* fields populated.
        provider: SourceProvider instance (e.g. K2GTextBlobProvider).
        logger: Optional logger.

    Returns:
        dict — {"raw_inserted": bool, "segment_inserted": bool}.
    """
    import logging
    log = logger or logging.getLogger("source_lineage")

    if not staged_event.source_provider or not staged_event.source_id:
        # Source lineage not applicable (legacy event or not yet backfilled) — skip
        return {"raw_inserted": False, "segment_inserted": False}

    raw_inserted = False
    segment_inserted = False

    # 1. Store raw_document (first segment + raw_document_content + provider has store_raw=True)
    if (
        staged_event.is_first_segment
        and staged_event.raw_document_content
        and getattr(provider, "_store_raw", False)
    ):
        try:
            provider.store_source(
                content=staged_event.raw_document_content,
                source_id=staged_event.source_id,
                domain=staged_event.domain,
                meta={
                    "owner_id": staged_event.inline_meta.get("owner_id"),
                    "org_id": staged_event.inline_meta.get("org_id"),
                    "visibility": staged_event.inline_meta.get("visibility", "public"),
                    "share_group_id": staged_event.inline_meta.get("share_group_id"),
                    "title": staged_event.inline_meta.get("title"),
                    "source_uri": staged_event.storage_uri,
                },
            )
            raw_inserted = True
            log.info("raw_document_stored", extra={
                "source_provider": staged_event.source_provider,
                "source_id": staged_event.source_id,
                "byte_size": len(staged_event.raw_document_content or ""),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("raw_document_store_failed", extra={
                "source_id": staged_event.source_id, "err": str(exc)[:200],
            })

    # 2. Store segment blob (when provider has store_segments=True)
    if getattr(provider, "_store_segments", False):
        try:
            locator = dict(staged_event.source_locator or {})
            provider.store_segment(
                source_id=staged_event.source_id,
                locator=locator,
                content=staged_event.summary,   # segment body (actual body vs summary resolved at fetch time)
                meta={
                    "owner_id": staged_event.inline_meta.get("owner_id"),
                    "org_id": staged_event.inline_meta.get("org_id"),
                    "visibility": staged_event.inline_meta.get("visibility", "public"),
                    "share_group_id": staged_event.inline_meta.get("share_group_id"),
                },
            )
            segment_inserted = True
            log.debug("segment_stored", extra={
                "source_provider": staged_event.source_provider,
                "source_id": staged_event.source_id,
                "locator": locator,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("segment_blob_store_failed", extra={
                "source_id": staged_event.source_id,
                "locator": staged_event.source_locator,
                "err": str(exc)[:200],
            })

    return {"raw_inserted": raw_inserted, "segment_inserted": segment_inserted}


def list_ready_domains(session_id: str = "", configured_root: str = "") -> list[str]:
    """Return the list of domains whose produce has completed.

    session_id is ignored (flat staging layout). All domain directories
    are checked.
    """
    domains = paths.list_domains(session_id, configured_root)
    ready: list[str] = []
    for d in domains:
        loader = StagingLoader(session_id, d, configured_root=configured_root)
        if loader.is_ready():
            ready.append(d)
    return ready


def iter_active_produced_events(
    domain: str,
    manifest_store,
    *,
    configured_root: str = "",
):
    """Yield only the staging events whose manifest entry is active+produced.

    Core cross-build LOAD flow:
    - When a producer skips a file because its hash matches, no new staging
      row is added, but the *existing* staging row from yesterday's build
      is still present in the flat directory.
    - This function joins the manifest's active+produced segments with the
      staging events and yields only those not yet loaded into the graph.

    Producer-specific inline_meta schema differences are normalised by
    ``k2g.staging.event_keys.extract_event_keys``, which is the single
    source of truth for all three call sites.

    Args:
        domain: The domain to process.
        manifest_store: BuildManifestStore instance.
        configured_root: Override for the staging root.

    Yields:
        StagedEvent for each segment that is active and produced in the
        manifest.
    """
    from k2g.staging.event_keys import extract_event_keys

    cur = manifest_store._conn.cursor()
    cur.execute(
        "SELECT file_path, segment_key, segment_index FROM build_segment_manifest "
        "WHERE domain = ? AND status = 'active' AND stage_state = 'produced'",
        (domain,),
    )
    # Primary index: (fp, seg_key) — common to doxygen and doc_batch.
    # Secondary index: (fp, seg_idx) — fallback for legacy schemas missing segment_key.
    by_segkey: set[tuple[str, str]] = set()
    by_segidx: set[tuple[str, int]] = set()
    for row in cur.fetchall():
        fp = (row["file_path"] or "").replace("\\", "/").strip("/")
        seg_key = row["segment_key"]
        seg_idx = row["segment_index"]
        if seg_key:
            by_segkey.add((fp, seg_key))
        if seg_idx is not None:
            by_segidx.add((fp, seg_idx))

    if not by_segkey and not by_segidx:
        return

    loader = StagingLoader(
        session_id="", domain=domain, configured_root=configured_root,
    )
    for ev in loader.iter_events():
        keys = extract_event_keys(ev)
        # 1. Exact segment_key match (primary source for both doc_batch and doxygen)
        if keys.segment_key and (keys.file_path, keys.segment_key) in by_segkey:
            yield ev
            continue
        # 2. segment_index fallback — compatibility with old schemas lacking segment_key
        if (
            keys.segment_index is not None
            and (keys.file_path, keys.segment_index) in by_segidx
        ):
            yield ev
            continue


__all__ = [
    "StagingLoader",
    "StagingLoadError",
    "list_ready_domains",
    "iter_active_produced_events",
]
