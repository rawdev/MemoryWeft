"""Train API — standalone triggers for domain-scoped jaccard / hdbscan."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_stores_dep, get_template_miner_dep, sanitize

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
