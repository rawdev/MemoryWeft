"""StagingWriter — producer writes ndjson + npy + manifest files.

The producer flushes per file or per chunk, reusing one writer per
(session_id, domain) pair. Each writer:

* Opens ndjson files in append mode and writes one line per
  write_event/entity/group call.
* Accumulates embeddings in order of append_embedding(local_ref, vector)
  calls into a (N, dim) array, flushed to npy on close.
* Transitions produce.state from in_progress → done (or failed).
* Writes manifest.json on close.

Exception safety: even if the writer exits via an exception,
finalize_failed(error) records "failed: {msg}" in produce.state.
The loader skips any domain whose state is not done.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from k2g.staging import paths
from k2g.staging.schema import (
    StagedEntity,
    StagedEvent,
    StagedGroup,
    StagingManifest,
)

logger = logging.getLogger(__name__)


class StagingWriter:
    """Append-only writer for a single (session_id, domain) pair.

    Using it as a context manager is recommended, but open/close are also
    available as explicit methods to support resumption across subprocess
    boundaries.
    """

    def __init__(
        self,
        session_id: str,
        domain: str,
        *,
        embedding_dim: int,
        configured_root: str = "",
    ) -> None:
        self.session_id = session_id
        self.domain = domain
        self.configured_root = configured_root
        self.embedding_dim = int(embedding_dim)

        self.domain_dir = paths.ensure_domain_dir(
            session_id, domain, configured_root
        )
        self._events_file: Any = None
        self._entities_file: Any = None
        self._groups_file: Any = None
        self._entity_names_seen: set[str] = set()
        self._group_names_seen: set[str] = set()
        self._embeddings: list[np.ndarray] = []
        self._embeddings_index: dict[str, int] = {}
        self._counts = {
            "events": 0, "entities_unique": 0, "groups_unique": 0,
        }
        self._started_at: str | None = None
        self._closed = False
        # Writer-owned sequence counter to avoid local_ref collisions when
        # multiple subprocesses append to the same (session_id, domain).
        # Initialized to max+1 by scanning existing events.ndjson /
        # embeddings_index.json in open().
        self._next_ref_seq: int = 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> "StagingWriter":
        self._events_file = paths.events_path(
            self.session_id, self.domain, self.configured_root
        ).open("a", encoding="utf-8")
        self._entities_file = paths.entities_path(
            self.session_id, self.domain, self.configured_root
        ).open("a", encoding="utf-8")
        self._groups_file = paths.groups_path(
            self.session_id, self.domain, self.configured_root
        ).open("a", encoding="utf-8")
        self._started_at = _now_iso()
        self._write_state(paths.STATE_IN_PROGRESS)
        # On resume, restore the entity/group dedup sets from the existing
        # manifest so the same name is not appended again.
        self._rebuild_dedup_sets()
        # Scan existing events.ndjson + embeddings_index to prevent local_ref
        # collisions, and load existing embeddings into self._embeddings so
        # the next flush does not overwrite refs from a previous run.
        self._restore_embeddings_and_seq()
        return self

    def __enter__(self) -> "StagingWriter":
        return self.open()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            self.finalize_failed(f"{exc_type.__name__}: {exc_val}")
        else:
            self.finalize_done()

    def _restore_embeddings_and_seq(self) -> None:
        """Scan existing events.ndjson / embeddings_index.json / embeddings.npy
        to set _next_ref_seq and load the existing embedding buffer into memory.

        When multiple producer subprocesses append to the same
        (session_id, domain) staging dir (e.g. two doc_batch steps with
        different divider profiles), skipping this restore would cause
        (1) local_ref collisions in the e_0000001..N range and
        (2) _flush_embeddings to overwrite the existing .npy with only its
        own buffer, destroying embeddings from the previous run.
        """
        import re

        max_seq = 0
        events_path = paths.events_path(
            self.session_id, self.domain, self.configured_root
        )
        if events_path.exists():
            ref_pat = re.compile(r"^e_(\d+)$")
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref = rec.get("local_ref", "")
                m = ref_pat.match(ref)
                if m:
                    n = int(m.group(1))
                    if n > max_seq:
                        max_seq = n
                self._counts["events"] += 1
        self._next_ref_seq = max_seq + 1

        idx_path = paths.embeddings_index_path(
            self.session_id, self.domain, self.configured_root
        )
        npy_path = paths.embeddings_path(
            self.session_id, self.domain, self.configured_root
        )
        if idx_path.exists() and npy_path.exists():
            try:
                existing_index = json.loads(idx_path.read_text(encoding="utf-8"))
                existing_matrix = np.load(npy_path, allow_pickle=False)
            except Exception:
                logger.exception(
                    "failed to restore existing embeddings; starting fresh buffer"
                )
                return
            # index is local_ref → row; fill _embeddings list in row order.
            ordered = sorted(existing_index.items(), key=lambda kv: kv[1])
            if ordered:
                max_row = ordered[-1][1]
                if max_row + 1 != len(existing_matrix):
                    logger.warning(
                        "embeddings_index size (%d) != npy rows (%d); "
                        "rebuilding conservatively",
                        max_row + 1, len(existing_matrix),
                    )
                self._embeddings = [
                    np.asarray(existing_matrix[row], dtype=np.float32)
                    for _, row in ordered
                    if row < len(existing_matrix)
                ]
                self._embeddings_index = {
                    ref: i for i, (ref, _) in enumerate(ordered)
                    if _ < len(existing_matrix)
                }

    def next_ref(self) -> str:
        """Issue a local_ref directly from the writer. Callers must use only
        this method to create refs, preventing collisions among multiple
        producers within the same session/domain."""
        ref = f"e_{self._next_ref_seq:07d}"
        self._next_ref_seq += 1
        return ref

    def _rebuild_dedup_sets(self) -> None:
        entities_path = paths.entities_path(
            self.session_id, self.domain, self.configured_root
        )
        if entities_path.exists():
            for line in entities_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    name = rec.get("name")
                    if name:
                        self._entity_names_seen.add(name)
                except json.JSONDecodeError:
                    continue
        groups_path = paths.groups_path(
            self.session_id, self.domain, self.configured_root
        )
        if groups_path.exists():
            for line in groups_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    name = rec.get("name")
                    if name:
                        self._group_names_seen.add(name)
                except json.JSONDecodeError:
                    continue
        # Sync counts so the manifest unique counts reflect the restored data.
        self._counts["entities_unique"] = len(self._entity_names_seen)
        self._counts["groups_unique"] = len(self._group_names_seen)

    # ------------------------------------------------------------------
    # Record writes
    # ------------------------------------------------------------------

    def write_event(self, event: StagedEvent, embedding: np.ndarray | None = None) -> None:
        self._require_open()
        # Atomicity guarantee: validate the embedding first before writing the
        # event ndjson line. Otherwise, if append_embedding raises (e.g. dim
        # mismatch), the event would remain in staging as an orphan without
        # its embedding.
        if embedding is not None:
            self.append_embedding(event.local_ref, embedding)
        line = event.model_dump_json()
        self._events_file.write(line + "\n")
        self._counts["events"] += 1

    def write_entity(self, entity: StagedEntity) -> None:
        self._require_open()
        if entity.name in self._entity_names_seen:
            return
        self._entity_names_seen.add(entity.name)
        self._entities_file.write(entity.model_dump_json() + "\n")
        self._counts["entities_unique"] = len(self._entity_names_seen)

    def write_group(self, group: StagedGroup) -> None:
        self._require_open()
        if group.name in self._group_names_seen:
            return
        self._group_names_seen.add(group.name)
        self._groups_file.write(group.model_dump_json() + "\n")
        self._counts["groups_unique"] = len(self._group_names_seen)


    def append_embedding(self, local_ref: str, vector: np.ndarray) -> None:
        self._require_open()
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        if vec.shape[0] != self.embedding_dim:
            raise ValueError(
                f"embedding dim mismatch: expected {self.embedding_dim}, got {vec.shape[0]}"
            )
        self._embeddings_index[local_ref] = len(self._embeddings)
        self._embeddings.append(vec)

    def flush(self) -> None:
        """fsync all file handles. Call at chunk boundaries to minimize data
        loss on crash."""
        for f in (self._events_file, self._entities_file, self._groups_file):
            if f is not None:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Finalizers
    # ------------------------------------------------------------------

    def finalize_done(self) -> None:
        if self._closed:
            return
        self._close_handles()
        self._flush_embeddings()
        self._write_manifest(done=True)
        self._write_state(paths.STATE_DONE)
        self._closed = True

    def finalize_failed(self, error: str) -> None:
        if self._closed:
            return
        self._close_handles()
        # Even on failure, flush accumulated embeddings to disk. The loader
        # skips domains whose produce.state != done, so this is safe, and
        # the partial results can be reused on resume.
        try:
            self._flush_embeddings()
        except Exception:
            logger.exception("failed to flush embeddings during failure finalize")
        self._write_manifest(done=False)
        self._write_state(paths.STATE_FAILED, error=error)
        self._closed = True

    def _close_handles(self) -> None:
        for attr in ("_events_file", "_entities_file", "_groups_file"):
            f = getattr(self, attr, None)
            if f is not None:
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError:
                    pass
                f.close()
                setattr(self, attr, None)

    def _flush_embeddings(self) -> None:
        if not self._embeddings:
            return
        matrix = np.stack(self._embeddings, axis=0).astype(np.float32)
        npy_path = paths.embeddings_path(
            self.session_id, self.domain, self.configured_root
        )
        np.save(npy_path, matrix, allow_pickle=False)
        idx_path = paths.embeddings_index_path(
            self.session_id, self.domain, self.configured_root
        )
        idx_path.write_text(
            json.dumps(self._embeddings_index, ensure_ascii=False),
            encoding="utf-8",
        )

    def _write_manifest(self, *, done: bool) -> None:
        manifest = StagingManifest(
            session_id=self.session_id,
            domain=self.domain,
            produce_started_at=self._started_at,
            produce_completed_at=_now_iso() if done else None,
            counts=dict(self._counts),
        )
        manifest_path = paths.manifest_path(
            self.session_id, self.domain, self.configured_root
        )
        manifest_path.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _write_state(self, state: str, *, error: str | None = None) -> None:
        payload = {
            "state": state,
            "updated_at": _now_iso(),
        }
        if error:
            payload["last_error"] = error
        state_path = paths.produce_state_path(
            self.session_id, self.domain, self.configured_root
        )
        state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("StagingWriter is closed")
        if self._events_file is None:
            raise RuntimeError("StagingWriter.open() not called")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["StagingWriter"]
