"""DbStore — facade for graph / vector / content / object backends.

Callers (producer / trainer / reader / updater) depend only on this
facade so that backend swaps (postgres <-> sqlite <-> future backends)
do not propagate into caller code.

Usage::

    from k2g.core.config import get_settings
    from k2g.db_store import DbStore

    db = DbStore.from_settings(get_settings())
    try:
        order_index = db.graph.get_order_index("k2g")
        event_id = db.graph.create_event(...)
        db.vector.upsert(vid, embedding, meta)
        uri = db.object.put_text("summary", domain="k2g")
    finally:
        db.close()

``db.session()`` provides a standardised context-manager transaction
scope. It holds the graph backend's ``_conn`` and guarantees
commit/rollback. SQLite uses ``BEGIN IMMEDIATE`` (DB-wide write lock);
Postgres uses native transactions.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from k2g.core.config import Settings
    from k2g.db_store.content_backend import ContentBackend
    from k2g.db_store.graph_backend import GraphStoreProtocol
    from k2g.db_store.object_backend import ObjectBackend
    from k2g.db_store.vector_backend import VectorStoreProtocol

logger = logging.getLogger(__name__)


@dataclass
class DbStore:
    """Composition facade for the four backends (graph/vector/content/object)."""

    graph: "GraphStoreProtocol"
    vector: "VectorStoreProtocol"
    content: "ContentBackend"
    object: "ObjectBackend"

    @classmethod
    def from_settings(cls, settings: "Settings") -> "DbStore":
        """Assemble backends from Settings provider configuration.

        graph / vector: selected by graph_db_provider / vector_store_provider
        (postgres or sqlite). SQLite backends share sqlite_all_in_one_path.
        content: determined by content_store_mode (sqlite | postgres).
        object: currently only LocalObjectStorage (extend Literal for S3 etc.).
        """
        from k2g.db_store.factory import (
            build_content_backend,
            build_graph_backend,
            build_object_backend,
            build_vector_backend,
        )

        return cls(
            graph=build_graph_backend(settings),
            vector=build_vector_backend(settings),
            content=build_content_backend(settings),
            object=build_object_backend(settings),
        )

    @contextmanager
    def session(self) -> Iterator["DbStore"]:
        """Graph backend transaction scope. All statements inside the
        yield form a single commit unit; exceptions trigger rollback.

        SQLite: ``BEGIN IMMEDIATE`` (DB write lock) + commit/rollback.
        Postgres: native transaction (assumes autocommit=False).
        Vector backend is external (e.g. Qdrant) and cannot be bundled
        into the same transaction. If the caller upserts vectors *then*
        commits events/manifest via this session(), a partial failure
        may leave zombie vector rows that a cleanup script can remove.
        """
        conn = getattr(self.graph, "_conn", None)
        if conn is None:
            # backend does not expose transaction support — no-op session
            yield self
            return

        backend_name = type(self.graph).__name__.lower()
        if "sqlite" in backend_name:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception:  # noqa: BLE001
                # may already be in a transaction — autocommit mode / nested call
                pass
            try:
                yield self
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise
            return

        # Postgres (psycopg2) — autocommit=False default
        try:
            yield self
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise

    def close(self) -> None:
        """Close all backend connections. Dispatches safely for backends without close."""
        for name, store in (
            ("graph", self.graph),
            ("vector", self.vector),
            ("content", self.content),
            ("object", self.object),
        ):
            if store is None:
                continue
            try:
                if hasattr(store, "close"):
                    store.close()
            except Exception as exc:
                logger.warning("DbStore.close %s failed (ignored): %s", name, exc)


__all__ = ["DbStore"]
