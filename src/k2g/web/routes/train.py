"""Train API — standalone triggers for domain-scoped jaccard / hdbscan."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import (
    get_projection_engine_dep,
    get_stores_dep,
    get_template_miner_dep,
    sanitize,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["train"])


@router.post("/train/jaccard")
def train_jaccard(
    domain: str = Query(..., description="Domain name"),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Re-run domain-scoped jaccard similarity."""
    graph = stores["graph"]
    try:
        count = graph.compute_jaccard_connected(domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.error("jaccard failed for domain=%s: %s", domain, exc)
        return {"error": f"jaccard failed: {exc}"}
    return sanitize({"domain": domain, "edges_added": int(count)})


@router.get("/train/embeddings/missing")
def train_embeddings_missing(
    domain: str = Query(..., description="Domain name"),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """How many rows in ``domain`` still have no vector.

    Lets the UI ask "backfill?" only when there is something to backfill —
    an archive imported with ``include_vectors=False`` reports every row here.
    """
    from k2g.portable.embed_backfill import count_missing

    graph = stores["graph"]
    try:
        missing = count_missing(graph, domain, _placeholder(graph))
    except Exception as exc:  # noqa: BLE001
        logger.error("missing-embedding probe failed for %s: %s", domain, exc)
        return {"error": f"probe failed: {exc}"}
    return sanitize({"domain": domain, "missing": missing})


@router.post("/train/embeddings")
def train_embeddings(
    domain: str = Query(..., description="Domain name"),
    stores: dict[str, Any] = Depends(get_stores_dep),
    projection: Any = Depends(get_projection_engine_dep),
) -> dict:
    """Recompute missing embeddings for ``domain`` (events, then entity centroids).

    Needed after importing an archive exported without vectors: search filters
    ``embedding IS NOT NULL``, so those rows are invisible to every query until
    this runs. Synchronous — the caller (Manager) shows a progress state.
    """
    from k2g.portable.embed_backfill import backfill_embeddings

    embedding = stores.get("embedding")
    if embedding is None:
        return {"error": "embedding backend not configured"}
    graph = stores["graph"]
    try:
        res = backfill_embeddings(
            graph, stores["vector"], embedding,
            domain=domain, projection=projection,
            placeholder=_placeholder(graph),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("embedding backfill failed for %s: %s", domain, exc)
        return {"error": f"backfill failed: {exc}"}
    return sanitize(res)


def _placeholder(graph: Any) -> str:
    """``?`` for SQLite, ``%s`` for Postgres — the backfill builds its own SQL."""
    return "%s" if type(graph).__name__.lower().startswith("postgres") else "?"


@router.post("/train/hdbscan")
def train_hdbscan(
    domain: str = Query(..., description="Domain name"),
    miner: Any = Depends(get_template_miner_dep),
) -> dict:
    """Re-run domain-scoped HDBSCAN (cluster results only, no ETG generation)."""
    if miner is None:
        return {"error": "TemplateMiner failed to initialise (embedding/LLM may not be configured)."}
    try:
        result = miner.cluster_domain(domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.error("hdbscan failed for domain=%s: %s", domain, exc)
        return {"error": f"hdbscan failed: {exc}"}
    return sanitize(result)
