"""SqliteVectorStore — SQLite + sqlite-vec VectorStore.

SQLite-backed VectorStore with 1-to-1 public-method parity with
QdrantVectorStore and PgVectorStore (VectorStoreProtocol).

Schema notes:
    Embeddings are stored as inline BLOB columns (events.embedding /
    entities.embedding) using sqlite-vec serialize_float32.  Search uses
    the vec_distance_cosine scalar function with an inline WHERE domain
    filter — same architecture as PgVectorStore.  The legacy events_vec /
    entities_vec vec0 virtual tables have been retired; use
    migrate_inline_embeddings for a one-time migration.

Connection policy:
    Opens the same file as SqliteGraphStore via a separate connection.
    Because WAL mode makes writes visible only after commit, embeddings
    must be written after the corresponding events row is committed:
    single-row path — second upsert after create_event commit;
    bulk path — add_events_bulk writes the embedding at INSERT time.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from k2g.core.models import VectorMetadata

logger = logging.getLogger(__name__)


def _phase1b_sqlite(desc: str) -> NotImplementedError:
    return NotImplementedError(f"Phase 1b: {desc}")


class SqliteVectorStore:
    """SQLite + sqlite-vec VectorStore.

    Shares the same DB file as SqliteGraphStore.  Embeddings are stored
    in inline BLOB columns (events.embedding / entities.embedding) and
    searched with vec_distance_cosine, matching PgVectorStore's architecture.
    """

    def __init__(
        self,
        path: str,
        collection: str = "k2g_embeddings",
        dim: int = 1024,
    ) -> None:
        """Args:
            path: Path to the SQLite file shared with SqliteGraphStore.
            collection: Logical collection name (for API compatibility
                with PgVectorStore).
            dim: Embedding dimension (default 1024).
        """
        self._path = path
        self._collection = collection
        self._dim = dim

        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self._conn = sqlite3.connect(
            path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            # Thread-safe for threadpool use (FastAPI sync endpoints).
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

        self._load_sqlite_vec()

        self._conn.execute("PRAGMA foreign_keys = ON;")
        # WAL mode is already set by GraphStore; re-applying is idempotent.
        self._conn.execute("PRAGMA journal_mode = WAL;")

        logger.info(
            "SqliteVectorStore init: path=%s, collection=%s, dim=%d",
            path, collection, dim,
        )
        self.setup_schema()

    def _load_sqlite_vec(self) -> None:
        try:
            self._conn.enable_load_extension(True)
        except AttributeError as exc:
            raise RuntimeError(
                "This Python sqlite3 build has load_extension disabled. "
                "Use pysqlite3-binary (`pip install pysqlite3-binary`) or "
                "a statically-linked sqlite3 with extensions enabled."
            ) from exc

        try:
            import sqlite_vec
        except ImportError as exc:
            raise RuntimeError(
                "sqlite-vec required. pip install sqlite-vec "
                "(see 69_Blue_print-32-AllInOneDB §A)."
            ) from exc

        try:
            sqlite_vec.load(self._conn)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "sqlite-vec load failed. Some SQLite builds disable "
                "load_extension; install pysqlite3-binary."
            ) from exc
        finally:
            try:
                self._conn.enable_load_extension(False)
            except AttributeError:
                pass

    def setup_schema(self) -> None:
        """Verify that the sqlite-vec extension is loaded (vec_distance_cosine available).

        Embeddings are stored in inline BLOB columns guaranteed by
        SqliteGraphStore.setup_schema via ALTER.  The legacy events_vec /
        entities_vec vec0 virtual table dependency has been removed.
        """
        cur = self._conn.cursor()
        try:
            # Verify sqlite-vec is loaded: vec_version() / vec_distance_cosine
            # are undefined (OperationalError) when the extension is missing.
            try:
                cur.execute("SELECT vec_version()")
                cur.fetchone()
            except sqlite3.OperationalError as exc:
                raise RuntimeError(
                    "sqlite-vec extension not loaded. "
                    "pip install sqlite-vec "
                    "(see 69_Blue_print-32-AllInOneDB §A)"
                ) from exc
        finally:
            cur.close()
        logger.info("SqliteVectorStore schema verified (sqlite-vec inline embedding)")

    # ------------------------------------------------------------------
    # (a) Event vector CRUD
    # ------------------------------------------------------------------

    def upsert(
        self,
        vector_id: str,
        vector: list[float],
        metadata: VectorMetadata,
    ) -> str:
        """UPDATE events.embedding inline column (same contract as PgVectorStore.upsert).

        Assumes the events row has already been INSERTed by
        graph.create_event / add_events_bulk.  If the row does not yet
        exist, 0 rows are updated — for the single-row path, this upsert
        is called as a second step after create_event commits; for the
        bulk path, add_events_bulk writes the embedding at INSERT time.
        Metadata is already inline in the events row and is ignored here.
        """
        import sqlite_vec  # type: ignore[import-not-found]

        del metadata  # already inline in the events row
        vec_bytes = sqlite_vec.serialize_float32(list(vector))
        self._conn.execute(
            "UPDATE events SET embedding = ? WHERE vector_id = ?",
            (vec_bytes, vector_id),
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
        """Domain list support + filter_event_ids (post-group-filter).

        When ``filter_search_targets`` is provided, the other two arguments
        are ignored.  Each tuple = (domain, event_ids|None); multiple targets
        are OR-combined.

        Uses the events.embedding inline column + vec_distance_cosine(embedding, ?)
        (cosine distance) + inline WHERE domain filter, matching PgVectorStore.search.
        The domain filter is applied on the same row, so there is no
        cross-domain post-filter leakage.
        Return shape: {id, domain, summary, distance}. score = 1 - distance.
        """
        import sqlite_vec  # type: ignore[import-not-found]

        qbytes = sqlite_vec.serialize_float32(list(query_vector))

        # Direct cosine on inline column — vec_distance_cosine performs a
        # brute-force scan (acceptable for SQLite local/dev scale; PG keeps
        # HNSW).  The idx_events_domain B-tree assists the domain pre-filter.
        conds: list[str] = ["embedding IS NOT NULL"]
        params: list[Any] = []

        if filter_search_targets is not None:
            target_clauses: list[str] = []
            for tgt_domain, tgt_event_ids in filter_search_targets:
                if tgt_event_ids is None:
                    target_clauses.append("domain = ?")
                    params.append(tgt_domain)
                elif not tgt_event_ids:
                    continue
                else:
                    event_placeholders = ", ".join(["?"] * len(tgt_event_ids))
                    target_clauses.append(
                        f"(domain = ? AND id IN ({event_placeholders}))"
                    )
                    params.append(tgt_domain)
                    params.extend(tgt_event_ids)
            if not target_clauses:
                return []
            conds.append("(" + " OR ".join(target_clauses) + ")")
        else:
            if filter_domain:
                if isinstance(filter_domain, list):
                    placeholders = ", ".join(["?"] * len(filter_domain))
                    conds.append(f"domain IN ({placeholders})")
                    params.extend(filter_domain)
                else:
                    conds.append("domain = ?")
                    params.append(filter_domain)
            if filter_event_ids is not None:
                if not filter_event_ids:
                    return []
                placeholders = ", ".join(["?"] * len(filter_event_ids))
                conds.append(f"id IN ({placeholders})")
                params.extend(filter_event_ids)

        where_sql = " AND ".join(conds)
        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT id, domain, summary,
                   vec_distance_cosine(embedding, ?) AS distance
              FROM events
             WHERE {where_sql}
             ORDER BY distance
             LIMIT ?
            """,
            (qbytes, *params, int(limit)),
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall():
            dist = float(r["distance"] if isinstance(r, dict) else r[3])
            # score_threshold is a lower bound on (1 - distance); 0.0 means no filter.
            if score_threshold > 0.0 and (1.0 - dist) < score_threshold:
                continue
            out.append({
                "id": r["id"] if isinstance(r, dict) else r[0],
                "domain": r["domain"] if isinstance(r, dict) else r[1],
                "summary": r["summary"] if isinstance(r, dict) else r[2],
                "distance": dist,
            })
        return out

    def get(self, vector_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT vector_id, domain, summary, timestamp FROM events "
            "WHERE vector_id = ?",
            (vector_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def get_vectors(self, vector_ids: list[str]) -> dict[str, list[float]]:
        if not vector_ids:
            return {}
        import sqlite_vec  # type: ignore[import-not-found]

        placeholders = ",".join("?" * len(vector_ids))
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT vector_id, embedding FROM events "
            f"WHERE vector_id IN ({placeholders}) AND embedding IS NOT NULL",
            vector_ids,
        )
        out: dict[str, list[float]] = {}
        for row in cur.fetchall():
            vid = row["vector_id"] if isinstance(row, dict) else row[0]
            blob = row["embedding"] if isinstance(row, dict) else row[1]
            if blob is None:
                continue
            # Some sqlite_vec versions omit deserialize_float32; fall back to struct.
            deserialize = getattr(sqlite_vec, "deserialize_float32", None)
            if deserialize is not None:
                out[vid] = list(deserialize(blob))
            else:
                import struct
                n = len(blob) // 4
                out[vid] = list(struct.unpack(f"{n}f", blob))
        return out

    def iter_event_vectors(
        self, domain: str | None = None,
    ) -> list[tuple[str, list[float], str]]:
        """Scan (event_id, vector, summary) with optional domain filter for HDBSCAN.

        Reads the events.embedding inline column directly
        (replaces the legacy events_vec JOIN).
        summary may be NULL; falls back to an empty string.
        """
        import sqlite_vec  # type: ignore[import-not-found]

        cur = self._conn.cursor()
        if domain:
            cur.execute(
                """
                SELECT id, embedding, summary
                  FROM events
                 WHERE embedding IS NOT NULL
                   AND (deprecated IS NULL OR deprecated = 0)
                   AND domain = ?
                """,
                (domain,),
            )
        else:
            cur.execute(
                """
                SELECT id, embedding, summary
                  FROM events
                 WHERE embedding IS NOT NULL
                   AND (deprecated IS NULL OR deprecated = 0)
                """
            )

        deserialize = getattr(sqlite_vec, "deserialize_float32", None)
        out: list[tuple[str, list[float], str]] = []
        for row in cur.fetchall():
            event_id = row[0]
            blob = row[1]
            summary = row[2] or ""
            if blob is None:
                continue
            if deserialize is not None:
                vec = list(deserialize(blob))
            else:
                import struct
                n = len(blob) // 4
                vec = list(struct.unpack(f"{n}f", blob))
            out.append((str(event_id), vec, str(summary)))
        return out

    # ------------------------------------------------------------------
    # (b) Entity vector CRUD
    # ------------------------------------------------------------------
    #
    # Design: the entities_vec (vec0 virtual table) held only id + embedding;
    # metadata (computed_at / method / ref_event_count / domain / entity_name)
    # lives in the separate entity_embedding_meta table.  This keeps the
    # metadata lifecycle independent from the entities row (identity), because
    # new ingestion that changes an entity's participated_in events triggers
    # frequent centroid recomputation.
    #
    # ProjectionEngine cache-hit detection compares cached.ref_event_count
    # against the current participated_in count to determine staleness.

    def upsert_entity_vector(
        self,
        entity_id: str,
        vector: list[float],
        metadata: dict[str, Any],
    ) -> None:
        """Atomically UPSERT the vector and its metadata.

        Updates the entities.embedding inline column (same as PgVectorStore).
        The entity row is assumed to already exist from the projection phase.
        entity_embedding_meta is written via ON CONFLICT UPSERT.
        """
        import sqlite_vec  # type: ignore[import-not-found]

        vec_bytes = sqlite_vec.serialize_float32(list(vector))
        self._conn.execute(
            "UPDATE entities SET embedding = ? WHERE id = ?",
            (vec_bytes, entity_id),
        )
        # metadata UPSERT
        self._conn.execute(
            """
            INSERT INTO entity_embedding_meta
                (entity_id, computed_at, method, ref_event_count, domain,
                 entity_name, coherence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                computed_at     = excluded.computed_at,
                method          = excluded.method,
                ref_event_count = excluded.ref_event_count,
                domain          = excluded.domain,
                entity_name     = excluded.entity_name,
                coherence       = excluded.coherence,
                updated_at      = CURRENT_TIMESTAMP
            """,
            (
                entity_id,
                str(metadata.get("computed_at") or ""),
                str(metadata.get("method") or "mean"),
                int(metadata.get("ref_event_count") or 0),
                metadata.get("domain"),
                metadata.get("entity_name"),
                metadata.get("coherence"),
            ),
        )
        self._conn.commit()

    def get_entity_vector(self, entity_id: str) -> dict[str, Any] | None:
        """Return combined vector + metadata for ProjectionEngine cache-hit checks.

        Return shape::
            {
              "entity_id": str,
              "vector": list[float],
              "metadata": {
                  "computed_at": str,
                  "method": str,
                  "ref_event_count": int,
                  "domain": str | None,
                  "entity_name": str | None,
                  "updated_at": str,
                  "coherence": float | None,
              }
            }
        """
        import sqlite_vec  # type: ignore[import-not-found]

        cur = self._conn.cursor()
        cur.execute(
            "SELECT embedding FROM entities WHERE id = ?", (entity_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        blob = row["embedding"] if isinstance(row, dict) else row[0]
        if blob is None:
            return None
        deserialize = getattr(sqlite_vec, "deserialize_float32", None)
        if deserialize is not None:
            vector = list(deserialize(blob))
        else:
            import struct
            n = len(blob) // 4
            vector = list(struct.unpack(f"{n}f", blob))

        # metadata JOIN
        cur.execute(
            "SELECT computed_at, method, ref_event_count, domain, "
            "       entity_name, updated_at, coherence "
            "  FROM entity_embedding_meta WHERE entity_id = ?",
            (entity_id,),
        )
        meta_row = cur.fetchone()
        metadata: dict[str, Any] = {}
        if meta_row is not None:
            cols = [d[0] for d in cur.description]
            metadata = dict(zip(cols, meta_row))
            # Normalise numeric types.
            if metadata.get("ref_event_count") is not None:
                metadata["ref_event_count"] = int(metadata["ref_event_count"])
            if metadata.get("coherence") is not None:
                metadata["coherence"] = float(metadata["coherence"])

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
        """Return the top-N entities by cosine similarity.

        Uses the entities.embedding inline column + vec_distance_cosine +
        inline WHERE domain filter, matching PgVectorStore.search_similar_entities.
        score = 1 - distance (cosine similarity); score_threshold is a lower
        bound (0.0 means no filter).  domain may be a single string or a list
        for multi-domain queries (list uses an IN clause).
        """
        import sqlite_vec  # type: ignore[import-not-found]

        # Normalise domain — both empty list and None mean "no filter".
        if isinstance(domain, list) and not domain:
            domain = None

        qbytes = sqlite_vec.serialize_float32(list(query_vector))
        conds: list[str] = ["embedding IS NOT NULL"]
        params: list[Any] = []
        if domain is not None:
            if isinstance(domain, list):
                placeholders = ", ".join(["?"] * len(domain))
                conds.append(f"domain IN ({placeholders})")
                params.extend(domain)
            else:
                conds.append("domain = ?")
                params.append(domain)
        where_sql = " AND ".join(conds)

        cur = self._conn.cursor()
        cur.execute(
            f"""
            SELECT id, name, domain, type,
                   vec_distance_cosine(embedding, ?) AS distance
              FROM entities
             WHERE {where_sql}
             ORDER BY distance
             LIMIT ?
            """,
            (qbytes, *params, int(limit)),
        )
        results: list[dict[str, Any]] = []
        for r in cur.fetchall():
            dist = float(r["distance"] if isinstance(r, dict) else r[4])
            score = 1.0 - dist
            if score_threshold > 0.0 and score < score_threshold:
                continue
            results.append({
                "id": r["id"] if isinstance(r, dict) else r[0],
                "name": r["name"] if isinstance(r, dict) else r[1],
                "domain": r["domain"] if isinstance(r, dict) else r[2],
                "type": r["type"] if isinstance(r, dict) else r[3],
                "distance": dist,
                "score": score,
            })
        return results

    def delete_entity_vector(self, entity_id: str) -> None:
        """Delete the vector and its metadata together.

        Sets entities.embedding to NULL (keeping the entity row itself),
        matching PgVectorStore.delete_entity_vector.  The metadata row is
        explicitly deleted.
        """
        self._conn.execute(
            "UPDATE entities SET embedding = NULL WHERE id = ?", (entity_id,),
        )
        self._conn.execute(
            "DELETE FROM entity_embedding_meta WHERE entity_id = ?", (entity_id,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # (c) Lifecycle
    # ------------------------------------------------------------------

    def get_collection_info(self) -> dict[str, Any]:
        """Return collection statistics based on NOT NULL embedding counts."""
        cur = self._conn.cursor()
        try:
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
        finally:
            cur.close()

        return {
            "name": self._collection,
            "points_count": points_count,
            "entity_points_count": entity_count,
            "dim": self._dim,
            "backend": "sqlite+sqlite-vec",
        }

    def ping(self) -> bool:
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            return True
        except sqlite3.Error as exc:
            logger.error("SqliteVectorStore connection failed: %s", exc)
            return False

    def close(self) -> None:
        if hasattr(self, "_conn") and self._conn is not None:
            try:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]
                logger.info("SqliteVectorStore connection closed")
            except sqlite3.Error as exc:
                logger.warning("SqliteVectorStore close error: %s", exc)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
