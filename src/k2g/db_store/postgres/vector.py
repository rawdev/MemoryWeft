"""PgVectorStore -- Postgres + pgvector VectorStore.

Provides 1:1 public method parity with QdrantVectorStore
(VectorStoreProtocol).

Schema note:
    events.embedding / entities.embedding columns and HNSW indexes are
    created by PostgresGraphStore DDL.  ``setup_schema()`` here only
    verifies that the pgvector extension and required columns exist.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from k2g.core.models import VectorMetadata
from k2g.db_store.postgres.reconnect import KEEPALIVE_KWARGS, ReconnectingConnMixin

logger = logging.getLogger(__name__)


def _to_native_float_list(vector) -> list[float]:
    """Convert numpy.ndarray / numpy scalar elements to native Python floats.

    psycopg2 cannot adapt ``numpy.float32`` / ``numpy.float64`` directly.
    ``ndarray.tolist()`` casts elements to native float safely.
    """
    if hasattr(vector, "tolist"):
        return vector.tolist()
    return [float(v) for v in vector]


class PgVectorStore(ReconnectingConnMixin):
    """Postgres + pgvector VectorStore.

    Shares the same DB as PostgresGraphStore. events.embedding /
    entities.embedding columns + HNSW indexes are created by GraphStore
    DDL, so VectorStore adds no additional DDL.

    All 5 event-vector and 4 entity-vector methods are fully
    implemented with the same interface as SqliteVectorStore.
    ``register_vector`` registers the pgvector adapter for automatic
    list[float] binding / result deserialisation.
    """

    def __init__(
        self,
        dsn: str,
        collection: str = "k2g_embeddings",
        dim: int = 1024,
    ) -> None:
        self._dsn = dsn
        self._collection = collection
        self._dim = dim

        self._conn = self._new_connection()

        logger.info(
            "PgVectorStore init: dsn=%s, collection=%s, dim=%d",
            dsn.split("@")[-1] if "@" in dsn else dsn, collection, dim,
        )
        # Skip schema setup in read-only MCP environments
        # (same flag as the graph store — 2026-05-12 incident).
        import os
        if os.environ.get("K2G_DB_SKIP_SCHEMA_SETUP", "").strip().lower() in ("1", "true", "yes"):
            logger.info(
                "PgVectorStore: schema setup skip (K2G_DB_SKIP_SCHEMA_SETUP=true)"
            )
        else:
            self.setup_schema()

    def setup_schema(self) -> None:
        """Verify that the pgvector extension and required embedding columns exist.

        All table/index DDL is handled by PostgresGraphStore.setup_schema().
        This method only performs an integrity check, assuming that the
        graph-side DDL has already been executed.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "pgvector extension not installed. "
                    "Run: CREATE EXTENSION vector; "
                    "(see 69_Blue_print-32-AllInOneDB §8)"
                )

            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = 'embedding'",
                ("events",),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "events.embedding column not found. "
                    "PostgresGraphStore.setup_schema() must run first."
                )

            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = 'embedding'",
                ("entities",),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    "entities.embedding column not found. "
                    "PostgresGraphStore.setup_schema() must run first."
                )
        self._conn.commit()
        logger.info("PgVectorStore schema verified (pgvector + embedding columns)")

    # ------------------------------------------------------------------
    # (a) Event vector CRUD
    # ------------------------------------------------------------------

    def upsert(
        self,
        vector_id: str,
        vector: list[float],
        metadata: VectorMetadata,
    ) -> str:
        """UPDATE events.embedding column.

        Assumes the events row was already INSERTed by
        PostgresGraphStore.create_event. Metadata (domain / summary /
        timestamp) is already inline in the events row, so only the
        vector is updated here.
        """
        del metadata  # already inline in the events row
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE events SET embedding = %s::vector WHERE vector_id = %s",
                (_to_native_float_list(vector), vector_id),
            )
        self._conn.commit()
        return vector_id

    def search(
        self,
        query_vector: list[float],
        filter_domain: str | list[str] | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
        filter_event_ids: list[str] | None = None,
        filter_search_targets: list[tuple[str, list[str] | None]] | None = None,
    ) -> list[dict[str, Any]]:
        """HNSW + ``<=>`` (cosine distance) KNN search.

        Return shape matches SqliteVectorStore: ``{id, domain, summary,
        distance}``. ``score_threshold`` is the lower bound of
        ``1 - distance`` (cosine similarity).

        ``filter_domain``: ``list[str]`` uses ``IN (...)``, ``str``
        uses ``=``. ``filter_event_ids`` restricts to those IDs.

        ``filter_search_targets``: when provided, the above two args
        are ignored. Each tuple = ``(domain, event_ids | None)`` --
        ``None`` means the entire domain, a list narrows to that
        domain + event_ids combination. Multiple targets are OR-ed.
        """
        q = _to_native_float_list(query_vector)
        conds = ["embedding IS NOT NULL", "(deprecated IS NULL OR deprecated = FALSE)"]
        params: list[Any] = []

        if filter_search_targets is not None:
            target_clauses: list[str] = []
            for tgt_domain, tgt_event_ids in filter_search_targets:
                if tgt_event_ids is None:
                    target_clauses.append("domain = %s")
                    params.append(tgt_domain)
                elif not tgt_event_ids:
                    # Group sub-tree is empty -- skip this target
                    continue
                else:
                    event_placeholders = ", ".join(["%s"] * len(tgt_event_ids))
                    target_clauses.append(
                        f"(domain = %s AND id IN ({event_placeholders}))"
                    )
                    params.append(tgt_domain)
                    params.extend(tgt_event_ids)
            if not target_clauses:
                return []
            conds.append("(" + " OR ".join(target_clauses) + ")")
        else:
            if filter_domain:
                if isinstance(filter_domain, list):
                    placeholders = ", ".join(["%s"] * len(filter_domain))
                    conds.append(f"domain IN ({placeholders})")
                    params.extend(filter_domain)
                else:
                    conds.append("domain = %s")
                    params.append(filter_domain)
            if filter_event_ids is not None:
                if not filter_event_ids:
                    return []
                placeholders = ", ".join(["%s"] * len(filter_event_ids))
                conds.append(f"id IN ({placeholders})")
                params.extend(filter_event_ids)
        where = " AND ".join(conds)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, domain, summary,
                       (embedding <=> %s::vector) AS distance
                  FROM events
                 WHERE {where}
                 ORDER BY embedding <=> %s::vector
                 LIMIT %s
                """,
                (q, *params, q, int(limit)),
            )
            rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            distance = float(r["distance"] or 0.0)
            score = 1.0 - distance
            if score < float(score_threshold):
                continue
            results.append({
                "id": r["id"],
                "domain": r["domain"],
                "summary": r["summary"] or "",
                "distance": distance,
            })
        return results

    def get(self, vector_id: str) -> dict[str, Any] | None:
        """Return event row metadata + vector_id. Does not return the embedding itself."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT vector_id, domain, summary, timestamp FROM events "
                "WHERE vector_id = %s",
                (vector_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_vectors(self, vector_ids: list[str]) -> dict[str, list[float]]:
        """Map vector_ids to {vid: list[float]}. pgvector adapter converts to list."""
        if not vector_ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT vector_id, embedding FROM events "
                "WHERE vector_id = ANY(%s) AND embedding IS NOT NULL",
                (list(vector_ids),),
            )
            out: dict[str, list[float]] = {}
            for row in cur.fetchall():
                emb = row["embedding"]
                if emb is None:
                    continue
                # register_vector may return numpy.ndarray; normalise to list.
                out[row["vector_id"]] = list(emb)
        return out

    def iter_event_vectors(
        self, domain: str | None = None,
    ) -> list[tuple[str, list[float], str]]:
        """Scan (event_id, vector, summary) for HDBSCAN phase with domain filter.

        Only rows with a non-NULL events.embedding are included.
        """
        conds = ["embedding IS NOT NULL", "(deprecated IS NULL OR deprecated = FALSE)"]
        params: list[Any] = []
        if domain:
            conds.append("domain = %s")
            params.append(domain)
        where = " AND ".join(conds)

        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT id, embedding, summary FROM events WHERE {where}",
                params,
            )
            out: list[tuple[str, list[float], str]] = []
            for row in cur.fetchall():
                emb = row["embedding"]
                if emb is None:
                    continue
                out.append((str(row["id"]), list(emb), str(row["summary"] or "")))
        return out

    # ------------------------------------------------------------------
    # (b) Entity vector CRUD
    # ------------------------------------------------------------------
    #
    # entities.embedding is stored via pgvector, but metadata
    # (computed_at / method / ref_event_count / domain / entity_name)
    # lives in a separate entity_embedding_meta table (parity with
    # SQLite). ProjectionEngine uses ref_event_count comparison for
    # cache staleness.

    def upsert_entity_vector(
        self,
        entity_id: str,
        vector: list[float],
        metadata: dict[str, Any],
    ) -> None:
        """Update entities.embedding + entity_embedding_meta UPSERT."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE entities SET embedding = %s::vector WHERE id = %s",
                (_to_native_float_list(vector), entity_id),
            )
            cur.execute(
                """
                INSERT INTO entity_embedding_meta
                    (entity_id, computed_at, method, ref_event_count, domain,
                     entity_name, coherence)
                VALUES (%s, COALESCE(%s::timestamptz, NOW()), %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET
                    computed_at     = EXCLUDED.computed_at,
                    method          = EXCLUDED.method,
                    ref_event_count = EXCLUDED.ref_event_count,
                    domain          = EXCLUDED.domain,
                    entity_name     = EXCLUDED.entity_name,
                    coherence       = EXCLUDED.coherence,
                    updated_at      = NOW()
                """,
                (
                    entity_id,
                    metadata.get("computed_at") or None,
                    str(metadata.get("method") or "mean"),
                    int(metadata.get("ref_event_count") or 0),
                    metadata.get("domain"),
                    metadata.get("entity_name"),
                    metadata.get("coherence"),
                ),
            )
        self._conn.commit()

    def get_entity_vector(self, entity_id: str) -> dict[str, Any] | None:
        """Return vector + metadata combined. Same shape as SQLite."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT embedding FROM entities WHERE id = %s AND embedding IS NOT NULL",
                (entity_id,),
            )
            row = cur.fetchone()
            if row is None or row["embedding"] is None:
                return None
            vector = list(row["embedding"])

            cur.execute(
                "SELECT computed_at, method, ref_event_count, domain, "
                "       entity_name, updated_at, coherence "
                "  FROM entity_embedding_meta WHERE entity_id = %s",
                (entity_id,),
            )
            meta_row = cur.fetchone()
        metadata: dict[str, Any] = {}
        if meta_row is not None:
            metadata = {
                "computed_at": meta_row["computed_at"].isoformat() if meta_row["computed_at"] else None,
                "method": meta_row["method"],
                "ref_event_count": int(meta_row["ref_event_count"] or 0),
                "domain": meta_row["domain"],
                "entity_name": meta_row["entity_name"],
                "updated_at": meta_row["updated_at"].isoformat() if meta_row["updated_at"] else None,
                "coherence": (
                    float(meta_row["coherence"])
                    if meta_row["coherence"] is not None else None
                ),
            }
        return {
            "entity_id": entity_id,
            "vector": vector,
            "metadata": metadata,
        }

    def search_similar_entities(
        self,
        query_vector: list[float],
        domain: str | list[str] | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Top-N entities by cosine similarity. Domain filter on entities.domain.

        Return shape::
            [{"id", "name", "domain", "score", "distance"}, ...]

        ``domain`` as ``list[str]`` uses ``IN (...)``.
        """
        q = _to_native_float_list(query_vector)
        conds = ["embedding IS NOT NULL"]
        params: list[Any] = []
        if domain:
            if isinstance(domain, list):
                placeholders = ", ".join(["%s"] * len(domain))
                conds.append(f"domain IN ({placeholders})")
                params.extend(domain)
            else:
                conds.append("domain = %s")
                params.append(domain)
        where = " AND ".join(conds)

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, domain, type,
                       (embedding <=> %s::vector) AS distance
                  FROM entities
                 WHERE {where}
                 ORDER BY embedding <=> %s::vector
                 LIMIT %s
                """,
                (q, *params, q, int(limit)),
            )
            rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            distance = float(r["distance"] or 0.0)
            score = 1.0 - distance
            if score < float(score_threshold):
                continue
            results.append({
                "id": r["id"],
                "name": r["name"],
                "domain": r["domain"],
                "type": r["type"],
                "distance": distance,
                "score": score,
            })
        return results

    def delete_entity_vector(self, entity_id: str) -> None:
        """Set entities.embedding to NULL. entity_embedding_meta ON DELETE
        CASCADE only fires on entity row deletion, so explicit DELETE here."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE entities SET embedding = NULL WHERE id = %s",
                (entity_id,),
            )
            cur.execute(
                "DELETE FROM entity_embedding_meta WHERE entity_id = %s",
                (entity_id,),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # (c) Lifecycle
    # ------------------------------------------------------------------

    def get_collection_info(self) -> dict[str, Any]:
        """Return collection info based on the events table."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM events WHERE embedding IS NOT NULL"
            )
            row = cur.fetchone()
            points_count = int(row["cnt"]) if row else 0

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM entities WHERE embedding IS NOT NULL"
            )
            row = cur.fetchone()
            entity_count = int(row["cnt"]) if row else 0

        return {
            "name": self._collection,
            "points_count": points_count,
            "entity_points_count": entity_count,
            "dim": self._dim,
            "backend": "postgres+pgvector",
        }

    def _new_connection(self):
        """Open a fresh pgvector-registered connection with keepalives.

        Re-registers the pgvector adapter on every (re)connect so a managed-PG
        idle-drop recovery keeps automatic list[float] <-> vector binding.
        """
        conn = psycopg2.connect(
            self._dsn, cursor_factory=RealDictCursor, **KEEPALIVE_KWARGS
        )
        conn.autocommit = False
        try:
            from pgvector.psycopg2 import register_vector
            register_vector(conn)
        except ImportError as e:
            raise RuntimeError(
                "pgvector package required. pip install 'pgvector>=0.3.0,<0.5.0'"
            ) from e
        return conn

    def ping(self) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as e:
            logger.error("PgVectorStore connection failed: %s", e)
            return False

    def close(self) -> None:
        if self._close_conn():
            logger.info("PgVectorStore connection closed")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
