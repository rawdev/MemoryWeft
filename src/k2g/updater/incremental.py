"""IncrementalBuilder -- two-stage hash comparison for selective ingestion.

Ported from legacy `pipeline/incremental.py`.  If the file-level hash
check (Step 1) passes, a segment-level hash check (Step 2) is
performed.  Files whose hash is unchanged are safely skipped even when
the Context Divider configuration changes.
"""

from __future__ import annotations

import hashlib
import logging

from k2g.updater.base import FileCheckResult, SegmentAction
from k2g.updater.manifest import BuildManifestStore

logger = logging.getLogger(__name__)


class IncrementalBuilder:
    """Identify changed segments via two-stage hash comparison and record in manifest."""

    def __init__(
        self,
        manifest: BuildManifestStore,
        build_id: str,
        *,
        supersede_judge: object | None = None,
        influence_service: object | None = None,
    ) -> None:
        self._manifest = manifest
        self._build_id = build_id
        # Opt-in AI judge supersede + influence_service.  When neither
        # is injected both are None and _maybe_judge_supersede is a no-op.
        self._supersede_judge = supersede_judge
        self._influence_service = influence_service

    def _maybe_judge_supersede(
        self,
        *,
        domain: str,
        file_path: str,
        seg_key: str,
        old_record: dict,
        new_text: str,
    ) -> None:
        """Call SupersedeJudge on supersede and apply set_influence.

        No-op (legacy) when judge or influence_service is not injected.
        """
        if self._supersede_judge is None or self._influence_service is None:
            return
        old_event_id = old_record.get("event_id") or ""
        if not old_event_id:
            return
        try:
            # old_text is not stored directly in the manifest -- only
            # available when the caller injects it.  This hook is only
            # meaningful when text is present.  Graceful skip to avoid
            # breaking the legacy path; actual wiring in a separate PR.
            old_text = old_record.get("text") or ""
            if not old_text:
                logger.debug(
                    "supersede_judge: old_text not provided -- skip (legacy hook env)",
                )
                return
            judgment = self._supersede_judge.evaluate(
                old_text=old_text, new_text=new_text,
                old_segment_key=seg_key, new_segment_key=seg_key,
                generation_index=0, total_generations=2,
            )
            self._influence_service.set_influence(
                old_event_id,
                judgment.suggested_old_score,
                f"ai_judge_supersede:{judgment.reason}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "supersede_judge hook failed (%s) -- skip", exc,
            )

    @property
    def build_id(self) -> str:
        return self._build_id

    # ------------------------------------------------------------------
    # Hash
    # ------------------------------------------------------------------

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ------------------------------------------------------------------
    # Segment key
    # ------------------------------------------------------------------

    @staticmethod
    def derive_segment_key(segment: dict, index: int) -> str:
        """Base key for a single segment -- duplicates within a file are
        made unique by the caller (derive_segment_keys) via #N suffix.

        Priority: section_heading > slide > page > segment_{index}.
        """
        meta = segment.get("meta", {})
        heading = meta.get("section_heading", "")
        if heading and heading.strip():
            return heading.strip()
        slide = meta.get("slide")
        if slide is not None:
            return f"slide_{slide}"
        page = meta.get("page")
        if page is not None:
            return f"page_{page}"
        return f"segment_{index}"

    @classmethod
    def derive_segment_keys(cls, segments: list[dict]) -> list[str]:
        """Determine segment_key list per file -- duplicate base keys
        within the same file are made unique via #N suffix.

        Heading-based dedup intent is preserved: first occurrence keeps
        the bare heading, subsequent ones become ``heading#1``,
        ``heading#2``, etc.  slide/page are naturally unique and
        unaffected.

        Observed: ``benchmarks/results/.../report.md`` shares the same
        ``Sample k2g-01`` heading across multiple chunks, causing a
        UNIQUE constraint violation.  This method auto-deduplicates.
        """
        seen: dict[str, int] = {}
        keys: list[str] = []
        for idx, seg in enumerate(segments):
            base = cls.derive_segment_key(seg, idx)
            n = seen.get(base, 0)
            keys.append(base if n == 0 else f"{base}#{n}")
            seen[base] = n + 1
        return keys

    # ------------------------------------------------------------------
    # Step 1: file-level check
    # ------------------------------------------------------------------

    def check_file(self, domain: str, file_path: str, file_content: str) -> bool:
        """True means SKIP is possible (file hash unchanged)."""
        file_hash = self.compute_hash(file_content)
        return self._manifest.check_file_hash(domain, file_path, file_hash)

    # ------------------------------------------------------------------
    # Step 1 + 2: combined check
    # ------------------------------------------------------------------

    def check_and_prepare(
        self,
        *,
        domain: str,
        file_path: str,
        file_content: str,
        segments: list[dict],
    ) -> FileCheckResult:
        file_hash = self.compute_hash(file_content)

        if self._manifest.check_file_hash(domain, file_path, file_hash):
            # Flat-staging cross-build LOAD: when file_hash matches,
            # always skip_file -- the produce step is skipped.
            # Segments at stage='produced' that have not been loaded
            # are picked up by the LOAD step's manifest join
            # (loader.iter_active_produced_events) in flat staging,
            # loaded into the graph, and promoted to 'loaded'.
            # This avoids supersede + reprocessing (NER cost);
            # only LOAD is retried.
            logger.debug("File hash unchanged (skip_file): %s", file_path)
            return FileCheckResult(action="skip_file", file_hash=file_hash)

        old_segments = self._manifest.get_active_segments(domain, file_path)
        old_map: dict[str, dict] = {}
        for row in old_segments:
            old_map[row["segment_key"]] = dict(row)

        segment_actions: list[SegmentAction] = []
        new_keys: set[str] = set()

        # Batch dedup segment_keys per file (add #N suffix on heading dups).
        all_keys = self.derive_segment_keys(segments)

        for idx, seg in enumerate(segments):
            text = seg.get("text", "")
            if not text.strip():
                continue

            seg_key = all_keys[idx]
            seg_hash = self.compute_hash(text)
            new_keys.add(seg_key)

            old_record = old_map.get(seg_key)
            # Even when segment_hash matches, if LOAD is incomplete
            # (stage_state != 'loaded') supersede + re-ingest so the
            # next LOAD can load into the graph and promote to
            # stage='loaded'.  Applies to both the changed-file and
            # unchanged-file branches.
            old_loaded = (
                old_record is not None
                and old_record["segment_hash"] == seg_hash
                and old_record.get("stage_state") == "loaded"
            )
            if old_loaded:
                segment_actions.append(SegmentAction(
                    segment_key=seg_key,
                    segment_index=idx,
                    segment_hash=seg_hash,
                    action="skip",
                    old_vector_id=None,
                ))
            else:
                old_vector_id = None
                if old_record:
                    old_vector_id = self._manifest.supersede_segment(
                        domain, file_path, seg_key,
                    )
                    # AI judge supersede hook -- opt-in (only when judge injected)
                    self._maybe_judge_supersede(
                        domain=domain,
                        file_path=file_path,
                        seg_key=seg_key,
                        old_record=old_record,
                        new_text=text,
                    )
                segment_actions.append(SegmentAction(
                    segment_key=seg_key,
                    segment_index=idx,
                    segment_hash=seg_hash,
                    action="ingest",
                    old_vector_id=old_vector_id,
                ))

        superseded_keys: list[str] = []
        for old_key in old_map:
            if old_key not in new_keys:
                self._manifest.supersede_segment(domain, file_path, old_key)
                superseded_keys.append(old_key)

        ingest_count = sum(1 for a in segment_actions if a.action == "ingest")
        skip_count = sum(1 for a in segment_actions if a.action == "skip")
        logger.info(
            "Incremental check: %s -> ingest=%d, skip=%d, superseded=%d",
            file_path, ingest_count, skip_count, len(superseded_keys),
        )
        return FileCheckResult(
            action="check_segments",
            file_hash=file_hash,
            segment_actions=segment_actions,
            superseded_keys=superseded_keys,
        )

    # ------------------------------------------------------------------
    # Record
    # ------------------------------------------------------------------

    def record_result(
        self,
        *,
        domain: str,
        file_path: str,
        segment_key: str,
        segment_index: int,
        segment_hash: str,
        vector_id: str,
        event_id: str,
        old_vector_id: str | None,
    ) -> None:
        self._manifest.record_segment(
            domain=domain,
            file_path=file_path,
            segment_key=segment_key,
            segment_index=segment_index,
            segment_hash=segment_hash,
            vector_id=vector_id,
            old_vector_id=old_vector_id,
            event_id=event_id,
            build_id=self._build_id,
        )

    def finalize_file(
        self,
        *,
        domain: str,
        file_path: str,
        file_hash: str,
        segment_count: int,
        source_root: str | None = None,
    ) -> None:
        """Update the manifest at the file level.

        ``source_root`` is recorded alongside the manifest entry so
        that Reader / MCP can resolve the original file in the
        consumer environment via the (source_root, file_path) pair.
        """
        self._manifest.supersede_file(domain, file_path)
        self._manifest.record_file(
            domain=domain,
            file_path=file_path,
            file_hash=file_hash,
            segment_count=segment_count,
            build_id=self._build_id,
            source_root=source_root,
        )

    # ------------------------------------------------------------------
    # BP-35 Extractor stage
    # ------------------------------------------------------------------

    def check_extract(
        self, *, domain: str, extractor_kind: str,
        input_path: str, input_hash: str,
    ) -> bool:
        """True when active record's input_hash matches -- extract can be skipped."""
        return self._manifest.check_extract_hash(
            domain, extractor_kind, input_path, input_hash,
        )

    def record_extract_result(
        self, *, domain: str, extractor_kind: str,
        input_path: str, input_hash: str, output_path: str,
    ) -> None:
        """Record extractor output in the manifest."""
        self._manifest.record_extract(
            domain=domain,
            extractor_kind=extractor_kind,
            input_path=input_path,
            input_hash=input_hash,
            output_path=output_path,
            build_id=self._build_id,
        )


__all__ = ["IncrementalBuilder"]
