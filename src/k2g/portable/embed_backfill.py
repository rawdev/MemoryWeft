"""Embedding backfill — recompute vectors for rows that arrived without them.

A portable archive exported with ``include_vectors=False`` carries no
embeddings, so `entities.embedding` / `events.embedding` land NULL.  That is
not a cosmetic gap: search is entirely vector-driven (the SQL filters
``embedding IS NOT NULL`` before anything else), so an un-backfilled import
returns **zero hits for every query, with no error** — indistinguishable from
"there is nothing about that in memory".

Recomputing locally is preferable to shipping vectors in the archive:

- Vectors dominate archive size and exporter memory (~32 KB per 1024-dim
  vector once decoded to ``list[float]``, all materialized at once).
- The target re-embeds with **its own** configured model, so a source/target
  model or dimension mismatch cannot arise.

Order matters: entity vectors are the L2-normalized mean of the vectors of the
events the entity participated in (``ProjectionEngine.compute_entity_centroid``),
so events must be embedded first or every centroid comes back ``None``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Embedding backends accept a list; batching amortizes model-call overhead.
# Kept modest so a progress callback stays responsive and a failure costs
# little work.
DEFAULT_BATCH = 32


def _rows(conn: Any, sql: str, params: tuple) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        out = []
        for r in cur.fetchall():
            out.append(tuple(r.values()) if isinstance(r, dict) else tuple(r))
        return out
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def count_missing(graph: Any, domain: str, placeholder: str = "?") -> dict[str, int]:
    """How many rows still need a vector.  Cheap enough to call before asking
    the user whether to run the backfill."""
    conn = graph._conn
    p = placeholder
    ev = _rows(
        conn,
        f"SELECT COUNT(*) FROM events WHERE domain = {p} "
        "AND embedding IS NULL AND summary IS NOT NULL AND summary <> ''",
        (domain,),
    )
    en = _rows(
        conn,
        f"SELECT COUNT(*) FROM entities WHERE domain = {p} AND embedding IS NULL",
        (domain,),
    )
    return {"events": int(ev[0][0]), "entities": int(en[0][0])}


def backfill_embeddings(
    graph: Any,
    vector: Any,
    embedding: Any,
    *,
    domain: str,
    projection: Any = None,
    batch: int = DEFAULT_BATCH,
    placeholder: str = "?",
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Embed events missing a vector, then derive entity centroids.

    ``projection`` is a ``ProjectionEngine``; when omitted the entity phase is
    skipped (events still get vectors, so event search works).  ``progress`` is
    called as ``(phase, done, total)``.

    Returns counts plus a ``failed`` tally — a batch that fails to embed is
    logged and skipped rather than aborting the run, so a single bad row cannot
    strand the whole import.
    """
    conn = graph._conn
    p = placeholder
    result: dict[str, Any] = {
        "domain": domain, "events": 0, "entities": 0, "failed": 0,
    }

    # --- events: embed the summary (the same text mweft_remember embeds) ---
    pending = _rows(
        conn,
        f"SELECT vector_id, summary FROM events WHERE domain = {p} "
        "AND embedding IS NULL AND summary IS NOT NULL AND summary <> '' "
        "AND vector_id IS NOT NULL",
        (domain,),
    )
    total = len(pending)
    if progress:
        progress("events", 0, total)
    for start in range(0, total, batch):
        chunk = pending[start:start + batch]
        try:
            vecs = embedding.embed_batch([str(s) for _, s in chunk])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "embed_batch failed (%d rows at offset %d): %s",
                len(chunk), start, exc,
            )
            result["failed"] += len(chunk)
            continue
        for (vector_id, _summary), vec in zip(chunk, vecs):
            if not vec:
                result["failed"] += 1
                continue
            try:
                # metadata is already inline on the events row; the SQLite
                # backend ignores it and the PG backend re-stamps it.
                vector.upsert(vector_id, list(vec), {})
                result["events"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("vector upsert failed (%s): %s", vector_id, exc)
                result["failed"] += 1
        if progress:
            progress("events", min(start + batch, total), total)

    # --- entities: centroid of the participating events' vectors ----------
    if projection is None:
        logger.info("no ProjectionEngine — entity centroids skipped")
        return result

    ents = _rows(
        conn,
        f"SELECT id FROM entities WHERE domain = {p} AND embedding IS NULL",
        (domain,),
    )
    etotal = len(ents)
    if progress:
        progress("entities", 0, etotal)
    for i, (entity_id,) in enumerate(ents, start=1):
        try:
            vec = projection.compute_entity_centroid(entity_id, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("centroid failed (%s): %s", entity_id, exc)
            result["failed"] += 1
            continue
        # None = the entity has no embedded events yet (or a zero-norm mean).
        # Not an error: nothing to store.
        if not vec:
            continue
        try:
            vector.upsert_entity_vector(
                entity_id, list(vec),
                {"method": getattr(
                    type(projection), "ENTITY_VECTOR_METHOD", "mean_l2norm",
                )},
            )
            result["entities"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("entity vector upsert failed (%s): %s", entity_id, exc)
            result["failed"] += 1
        if progress and (i % batch == 0 or i == etotal):
            progress("entities", i, etotal)

    return result
