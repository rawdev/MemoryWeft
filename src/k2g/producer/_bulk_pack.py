"""Transactional bulk pack for events / entities / vectors / manifest.

Atomically applies a file- or N-segment unit to the DB during the
producer LOAD phase.  Bundles events insert + entity MERGE +
participated_in + entity_connection + sequential_next + member_of +
manifest record into a single ``db.session()`` transaction, preventing
zombie rows and reducing Postgres remote RTT (per-segment ~50
roundtrips down to per-file ~7).

Usage pattern::

    pack = BulkInsertPack()
    for seg in segments:
        ev_idx = pack.add_event(...)
        pack.add_vector(vector_id, embedding, metadata)
        pack.add_member_of(ev_idx, group_id)
        for name, type in seg.entities:
            ent_idx = pack.add_entity(name, domain, type)
            pack.add_participated_in(ev_idx, ent_idx)
        # entity pair co-occurrence
        pack.add_entity_connections_for_event(ev_idx, [ent_idx, ...])
        if prev_ev_idx is not None:
            pack.add_sequential_next(prev_ev_idx, ev_idx)
        pack.add_manifest_segment(...)
    pack.commit(db, manifest_store=manifest)

``pack.commit`` executes inside one transaction:

1. graph.add_events_bulk             -- 1 statement
2. graph.link_or_create_entities_bulk -- 1 statement (RETURNING/SELECT)
3. graph.link_participated_in_bulk    -- 1 statement
4. graph.upsert_entity_connections_bulk -- 1 statement
5. graph.link_event_member_of_bulk    -- 1 statement
6. graph.link_sequential_next_bulk    -- 1 statement
7. manifest.record_segments_bulk      -- 1 statement

All succeed -> commit; any failure -> rollback.

When the vector backend is external (Qdrant, etc.), transactions are
split: vector upsert runs *first* (failure leaves graph untouched,
zero zombies); on success the events / manifest commit in the same
session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from k2g.db_store import DbStore
    from k2g.updater.manifest import BuildManifestStore

logger = logging.getLogger(__name__)


@dataclass
class _PendingEvent:
    row: dict[str, Any]
    vector_id: str | None = None
    embedding: list[float] | None = None
    vector_metadata: dict[str, Any] | None = None


@dataclass
class _PendingMember:
    event_index: int    # list index into _PendingEvent (mapped after id assignment)
    group_id: str
    kind: str = "contains"


@dataclass
class _PendingEntity:
    name: str
    domain: str
    type: str = ""


@dataclass
class _PendingParticipated:
    event_index: int
    entity_index: int


@dataclass
class _PendingSequential:
    prev_event_index: int
    next_event_index: int
    source: str = "chunk_order"


@dataclass
class _PendingManifestSegment:
    domain: str
    file_path: str
    segment_key: str
    segment_index: int
    segment_hash: str
    vector_id: str
    event_index: int    # mapped to assigned event_id
    old_vector_id: str | None = None
    build_id: str = ""


@dataclass
class BulkInsertPack:
    """Batch accumulator + transactional commit for the LOAD phase."""

    events: list[_PendingEvent] = field(default_factory=list)
    members: list[_PendingMember] = field(default_factory=list)
    entities: list[_PendingEntity] = field(default_factory=list)
    # (name, domain) -> entities[] index -- unifies duplicate entity name indices
    _entity_index: dict[tuple[str, str], int] = field(default_factory=dict)
    participated: list[_PendingParticipated] = field(default_factory=list)
    # (a_idx, b_idx) -> count -- entity_index based, converted to entity_id at commit
    connections: dict[tuple[int, int], int] = field(default_factory=dict)
    sequentials: list[_PendingSequential] = field(default_factory=list)
    manifest_segments: list[_PendingManifestSegment] = field(default_factory=list)
    # Populated after commit -- reused by caller for vector metadata updates, etc.
    entity_id_by_index: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def add_event(
        self,
        *,
        domain: str,
        vector_id: str,
        timestamp: str | None,
        order_index: int,
        summary: str = "",
        embedding: list[float] | None = None,
        vector_metadata: dict[str, Any] | None = None,
        # Owner 5-column set (additive, default = public)
        owner_id: str | None = None,
        org_id: str | None = None,
        visibility: str = "public",
        acl_json: str | None = None,
        share_group_id: str | None = None,
        # NER metadata propagation (activating dead columns)
        ner_method: str | None = None,
        ner_skip_reason: str | None = None,
        # Pre-assigned deterministic event_id (manifest dedup).  None means
        # add_events_bulk issues a new UUID (existing behaviour).
        event_id: str | None = None,
    ) -> int:
        """Return accumulated event index for subsequent add_member_of / manifest mapping."""
        _row: dict[str, Any] = {
            "vector_id": vector_id,
            "domain": domain,
            "timestamp": timestamp,
            "order_index": order_index,
            "summary": summary,
            # Owner columns
            "owner_id": owner_id,
            "org_id": org_id,
            "visibility": visibility,
            "acl_json": acl_json,
            "share_group_id": share_group_id,
            # NER metadata
            "ner_method": ner_method,
            "ner_skip_reason": ner_skip_reason,
        }
        # If a pre-assigned id exists, pass it as row["id"] so add_events_bulk uses it as-is.
        if event_id:
            _row["id"] = event_id
        ev = _PendingEvent(
            row=_row,
            vector_id=vector_id,
            embedding=embedding,
            vector_metadata=vector_metadata,
        )
        self.events.append(ev)
        return len(self.events) - 1

    def add_member_of(
        self, event_index: int, group_id: str, *,
        kind: str = "contains",
    ) -> None:
        self.members.append(_PendingMember(
            event_index=event_index, group_id=group_id, kind=kind,
        ))

    def add_entity(self, name: str, domain: str, type: str = "") -> int:
        """Accumulate entity. Duplicate (name, domain) calls return the same index."""
        key = (name, domain)
        existing = self._entity_index.get(key)
        if existing is not None:
            return existing
        idx = len(self.entities)
        self.entities.append(_PendingEntity(name=name, domain=domain, type=type))
        self._entity_index[key] = idx
        return idx

    def add_participated_in(
        self, event_index: int, entity_index: int,
    ) -> None:
        self.participated.append(_PendingParticipated(
            event_index=event_index, entity_index=entity_index,
        ))

    def add_entity_connections_for_event(
        self, event_index: int, entity_indices: list[int],
    ) -> None:
        """Accumulate pairwise entity co-occurrence for one event (a_idx < b_idx normalised).

        If the same (a, b) pair appears across multiple events the count
        accumulates.  event_index is not currently used in the
        entity_connection table -- only count matters.
        """
        n = len(entity_indices)
        # Skip outlier events to avoid N(N-1)/2 explosion.  The upper bound comes
        # from the setting community_max_entities_per_event (default 50), shared
        # with the single-remember (memory_tools.py) and single-event (_shared.py)
        # paths.  Previously this path used a bare literal 50, ignoring the setting.
        from k2g.core.config import get_settings
        _max_ent = get_settings().community_max_entities_per_event
        if n < 2 or n >= _max_ent:
            return
        for i in range(n):
            for j in range(i + 1, n):
                a, b = entity_indices[i], entity_indices[j]
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                self.connections[key] = self.connections.get(key, 0) + 1

    def add_sequential_next(
        self, prev_event_index: int, next_event_index: int,
        *, source: str = "chunk_order",
    ) -> None:
        self.sequentials.append(_PendingSequential(
            prev_event_index=prev_event_index,
            next_event_index=next_event_index,
            source=source,
        ))

    def add_manifest_segment(
        self, *,
        domain: str, file_path: str, segment_key: str,
        segment_index: int, segment_hash: str, vector_id: str,
        event_index: int,
        old_vector_id: str | None = None,
        build_id: str = "",
    ) -> None:
        self.manifest_segments.append(_PendingManifestSegment(
            domain=domain, file_path=file_path, segment_key=segment_key,
            segment_index=segment_index, segment_hash=segment_hash,
            vector_id=vector_id, event_index=event_index,
            old_vector_id=old_vector_id, build_id=build_id,
        ))

    # ------------------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not self.events

    def __len__(self) -> int:
        return len(self.events)

    # ------------------------------------------------------------------
    def commit(
        self,
        db: "DbStore",
        *,
        manifest_store: "BuildManifestStore | None" = None,
    ) -> list[str]:
        """Atomically apply all accumulated data.

        Procedure:
        1. Vector upsert *first* (failure leaves graph untouched, zero zombies)
        2. Graph events bulk insert (inside db.session())
        3. Entities bulk MERGE -> entity_id mapping
        4. participated_in / entity_connections / sequential_next / member_of bulk
        5. Manifest record (same session)
        6. Commit all

        Returns:
            List of assigned event_ids (same order as events).
        """
        if self.empty:
            return []

        # 1. Vector upsert -- process before graph to prevent zombie rows
        for ev in self.events:
            if ev.embedding is not None:
                try:
                    db.vector.upsert(
                        ev.vector_id, ev.embedding,
                        ev.vector_metadata or {},
                    )
                except Exception as exc:  # noqa: BLE001
                    # Vector failure -> full abort.  We haven't entered the
                    # transaction so the graph is untouched.  Caller decides retry.
                    logger.error(
                        "BulkInsertPack vector upsert failed (vector_id=%s): %s -- abort",
                        ev.vector_id, exc,
                    )
                    raise

        # 2-5. graph + manifest in one transaction
        # Fallback for minimal mock fixtures without db.session() -- if
        # graph._conn exists, use it with a fallback context that commits.
        from contextlib import contextmanager
        session_fn = getattr(db, "session", None)
        if session_fn is not None and callable(session_fn):
            session_ctx = session_fn()
        else:
            @contextmanager
            def _fallback():
                conn = getattr(db.graph, "_conn", None)
                try:
                    yield db
                    if conn is not None:
                        try:
                            conn.commit()
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    raise
            session_ctx = _fallback()
        with session_ctx:
            # graph events -- inject inline embedding into row (SQLite add_events_bulk
            # writes events.embedding at INSERT time; PG ignores the key).
            event_rows = [
                ({**ev.row, "embedding": ev.embedding}
                 if ev.embedding is not None else ev.row)
                for ev in self.events
            ]
            event_ids = db.graph.add_events_bulk(event_rows)
            self.event_ids = event_ids

            # entities MERGE -- (name, domain) -> entity_id
            entity_id_by_index: list[str] = []
            if self.entities:
                ent_rows = [
                    {"name": e.name, "domain": e.domain, "type": e.type}
                    for e in self.entities
                ]
                ent_map = db.graph.link_or_create_entities_bulk(ent_rows)
                for e in self.entities:
                    eid = ent_map.get((e.name, e.domain))
                    if eid is None:
                        # MERGE failed -- raise since the next entity may not share the key
                        raise RuntimeError(
                            f"link_or_create_entities_bulk: entity_id mapping missing "
                            f"(name={e.name!r}, domain={e.domain!r})"
                        )
                    entity_id_by_index.append(eid)
            self.entity_id_by_index = entity_id_by_index

            # participated_in
            participated_rows = [
                {
                    "entity_id": entity_id_by_index[p.entity_index],
                    "event_id": event_ids[p.event_index],
                }
                for p in self.participated
            ]
            if participated_rows:
                db.graph.link_participated_in_bulk(participated_rows)

            # entity_connection -- convert (a_idx, b_idx) to entity_id + count
            if self.connections:
                conn_rows = [
                    {
                        "a_id": entity_id_by_index[a_idx],
                        "b_id": entity_id_by_index[b_idx],
                        "count": cnt,
                    }
                    for (a_idx, b_idx), cnt in self.connections.items()
                ]
                # Re-normalise a < b based on entity_id (UUID)
                normalized = []
                for r in conn_rows:
                    a, b = r["a_id"], r["b_id"]
                    if a > b:
                        a, b = b, a
                    normalized.append({"a_id": a, "b_id": b, "count": r["count"]})
                # Same (a_id, b_id) could theoretically arise from multiple
                # entity_index pairs after dedup (shouldn't happen since
                # _entity_index is unique). Safe-guard merge:
                merged: dict[tuple[str, str], int] = {}
                for r in normalized:
                    key = (r["a_id"], r["b_id"])
                    merged[key] = merged.get(key, 0) + r["count"]
                final_rows = [
                    {"a_id": a, "b_id": b, "count": c}
                    for (a, b), c in merged.items()
                ]
                db.graph.upsert_entity_connections_bulk(final_rows)

            # event_member_of
            member_rows = [
                {
                    "event_id": event_ids[m.event_index],
                    "group_id": m.group_id,
                    "kind": m.kind,
                }
                for m in self.members
            ]
            if member_rows:
                db.graph.link_event_member_of_bulk(member_rows)

            # sequential_next
            seq_rows = [
                {
                    "prev_id": event_ids[s.prev_event_index],
                    "next_id": event_ids[s.next_event_index],
                    "source": s.source,
                }
                for s in self.sequentials
            ]
            if seq_rows:
                db.graph.link_sequential_next_bulk(seq_rows)

            # manifest
            if manifest_store is not None and self.manifest_segments:
                rows = [
                    {
                        "domain": ms.domain,
                        "file_path": ms.file_path,
                        "segment_key": ms.segment_key,
                        "segment_index": ms.segment_index,
                        "segment_hash": ms.segment_hash,
                        "vector_id": ms.vector_id,
                        "old_vector_id": ms.old_vector_id,
                        "event_id": event_ids[ms.event_index],
                        "build_id": ms.build_id,
                    }
                    for ms in self.manifest_segments
                ]
                _bulk_record_segments(manifest_store, rows)

        return event_ids


def _bulk_record_segments(
    manifest_store: "BuildManifestStore",
    rows: list[dict[str, Any]],
) -> None:
    """Use manifest.record_segments_bulk if available; otherwise fall back to single-row loop."""
    bulk = getattr(manifest_store, "record_segments_bulk", None)
    if callable(bulk):
        bulk(rows)
        return
    # fallback -- single-row loop for legacy compatibility.
    for r in rows:
        manifest_store.record_segment(**r)


__all__ = ["BulkInsertPack"]
