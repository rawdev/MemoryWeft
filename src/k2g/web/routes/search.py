"""Search API -- vector search + post-filter."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from k2g.web.deps import get_stores_dep, sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


@router.post("/search")
def vector_search(
    body: dict,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Vector search + Neo4j context enrichment + post-filter.

    Body: { query, domain, limit?, score_threshold?, include_deleted? }
    """
    query = body.get("query", "").strip()
    domain = body.get("domain", "")
    limit = body.get("limit", 10)
    score_threshold = body.get("score_threshold", 0.3)
    include_deleted = body.get("include_deleted", False)

    if not query:
        return {"error": "query is required.", "results": []}

    embedding_client = stores["embedding"]
    vector_store = stores["vector"]
    graph = stores["graph"]
    content_store = stores["content"]

    # 1. Query embedding
    try:
        query_vector = embedding_client.embed(query)
    except Exception as e:
        logger.error("Embedding failed: %s", e)
        return {"error": f"Embedding failed: {e}", "results": []}

    # 2. Vector search
    search_results = vector_store.search(
        query_vector=query_vector,
        filter_domain=domain or None,
        limit=limit,
        score_threshold=score_threshold,
    )

    # 3. Neo4j context enrichment
    results = []
    for hit in search_results:
        metadata = hit.get("metadata", {})
        event_id = metadata.get("event_id", "")
        content_id = metadata.get("content_id", "")

        # summary
        summary = ""
        if content_id:
            content_record = content_store.get(content_id)
            if content_record:
                summary = content_record.inline_meta.get("event_summary", "")

        # Event context (entities + tags) -- graph.get_event_context internal key 'groups'
        entities = []
        tags = []
        timestamp = None
        if event_id:
            context = graph.get_event_context(event_id)
            entities = context.get("entities", [])
            tags = context.get("groups", [])  # internal key 'groups' → surface key 'tags'

            event_data = graph.get_event_by_id(event_id)
            if event_data:
                timestamp = event_data.get("timestamp")

        # post-filter: when include_deleted=False, skip events
        # where every participating entity is user_deleted
        if not include_deleted and entities:
            all_deleted = all(
                e.get("user_tag") == "user_deleted" for e in entities
            )
            if all_deleted:
                continue

        tag_names = [t.get("name", "") for t in tags]

        results.append({
            "event_id": event_id,
            "score": hit.get("score", 0.0),
            "summary": summary,
            "entities": entities,
            "tags": tag_names,
            "timestamp": timestamp,
        })

    return sanitize({"results": results})
