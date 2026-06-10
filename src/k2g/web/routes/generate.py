"""Generate API — branched event generation via EventBrancher.

EventBrancher has been rewritten on top of DbStore + LLM
(`src/k2g/adapters/event_brancher.py`).  This endpoint handles
**text generation only** — re-ingesting generated events back into
the graph is recommended via the Producer/Loader path; the
legacy IngestionPipeline is not used.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from k2g.web.deps import (
    get_db_dep,
    get_embedding_client_dep,
    get_event_brancher_dep,
)
from k2g.web.reingest import (
    ReingestRequest,
    reingest_branched_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["generate"])


def _require_brancher(brancher: Any) -> Any:
    if brancher is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_ready",
                "feature": "EventBrancher",
                "note": "EventBrancher failed to initialise. Check deps.startup() logs.",
            },
        )
    return brancher


@router.post("/generate")
def generate_event(
    body: dict,
    brancher: Any = Depends(get_event_brancher_dep),
) -> dict:
    """Generate the next event text conditionally from base_event_id.

    Body::

        {
          "base_event_id": "evt_...",
          "condition": "What if the protagonist had refused?",
          "entities": ["Alice", "Bob"],            // optional
          "domain": "novel",                        // optional
          "control_node_id": "cg_...",             // optional guardrail
          "temperature": 0.7, "max_tokens": 1024   // optional LLM params
        }
    """
    brancher = _require_brancher(brancher)
    base_event_id = str(body.get("base_event_id") or "").strip()
    condition = str(body.get("condition") or "").strip()
    entities = body.get("entities")
    domain = str(body.get("domain") or "")
    control_node_id = body.get("control_node_id") or None

    if not base_event_id or not condition:
        return {"error": "base_event_id and condition are required."}

    llm_params = {
        k: v for k, v in body.items()
        if k in ("temperature", "max_tokens")
    }
    try:
        branched = brancher.branch(
            base_event_id=base_event_id,
            condition=condition,
            entities=entities if isinstance(entities, list) else None,
            domain=domain,
            control_node_id=control_node_id,
            **llm_params,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("EventBrancher.branch failed: %s", exc)
        return {"error": f"branch failed: {exc}"}

    return {
        "text": branched.text,
        "base_event_id": branched.base_event_id,
        "condition": branched.condition,
        "entities": branched.entities,
        "domain": branched.domain,
        "metadata": branched.metadata,
    }


@router.post("/generate/search")
def generate_event_by_search(
    body: dict,
    brancher: Any = Depends(get_event_brancher_dep),
) -> dict:
    """Vector search → entity merge → generate new event text.

    Body: ``{query, condition, entities, domain?, search_limit?, score_threshold?}``
    """
    brancher = _require_brancher(brancher)
    query = str(body.get("query") or "").strip()
    condition = str(body.get("condition") or "").strip()
    entities = body.get("entities") or []
    domain = str(body.get("domain") or "")

    if not query or not condition:
        return {"error": "query and condition are required."}
    if not isinstance(entities, list):
        return {"error": "entities must be an array."}

    try:
        branched = brancher.branch_by_search(
            query=query, condition=condition,
            entities=[str(e) for e in entities],
            domain=domain,
            search_limit=int(body.get("search_limit") or 5),
            score_threshold=float(body.get("score_threshold") or 0.3),
            merge_searched_entities=bool(body.get("merge_searched_entities", True)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("EventBrancher.branch_by_search failed: %s", exc)
        return {"error": f"branch_by_search failed: {exc}"}

    return {
        "text": branched.text,
        "base_event_id": branched.base_event_id,
        "condition": branched.condition,
        "entities": branched.entities,
        "domain": branched.domain,
        "metadata": branched.metadata,
    }


@router.post("/generate/reingest")
def reingest_event(
    body: dict,
    db: Any = Depends(get_db_dep),
    embedding_client: Any = Depends(get_embedding_client_dep),
) -> dict:
    """Re-ingest a generated event into the graph and vector store.

    Body::

        {
          "text": "...",                       // required — BranchedEvent.text
          "domain": "novel",                   // required
          "summary": "optional, default first 80 chars",
          "timestamp": "optional ISO-8601",
          "entities": [{"name":"Alice","type":"person"}, ...],   // optional
          "groups": ["novel::scene/cafe"],     // optional
          "inline_meta": {...}                 // optional
        }

    NER is the caller's responsibility.  Passing the entity list already
    resolved by EventBrancher is the recommended flow.  A single-shot
    ingest: produce → load in one call via an in-memory writer, no
    staging file.
    """
    if embedding_client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_ready",
                "feature": "EmbeddingClient",
                "note": "Check deps.startup() logs.",
            },
        )

    text = str(body.get("text") or "")
    domain = str(body.get("domain") or "").strip()
    if not text.strip():
        raise HTTPException(status_code=422, detail={"error": "text is required."})
    if not domain:
        raise HTTPException(status_code=422, detail={"error": "domain is required."})

    raw_entities = body.get("entities") or []
    if not isinstance(raw_entities, list):
        raise HTTPException(
            status_code=422,
            detail={"error": "entities must be an array of {name, type} objects."},
        )
    entities: list[dict[str, str]] = []
    for ent in raw_entities:
        if isinstance(ent, dict) and ent.get("name"):
            entities.append({
                "name": str(ent.get("name")),
                "type": str(ent.get("type") or "unknown"),
            })
        elif isinstance(ent, str) and ent.strip():
            entities.append({"name": ent.strip(), "type": "unknown"})

    groups = body.get("groups") or []
    if not isinstance(groups, list):
        raise HTTPException(
            status_code=422,
            detail={"error": "groups must be a string array."},
        )

    inline_meta = body.get("inline_meta") or {}
    if not isinstance(inline_meta, dict):
        raise HTTPException(
            status_code=422,
            detail={"error": "inline_meta must be an object."},
        )

    request = ReingestRequest(
        text=text,
        domain=domain,
        summary=str(body.get("summary") or ""),
        timestamp=str(body.get("timestamp") or ""),
        entities=entities,
        groups=[str(g) for g in groups],
        inline_meta=dict(inline_meta),
    )

    try:
        result = reingest_branched_event(
            request,
            db=db,
            embedding_client=embedding_client,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("reingest failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": f"reingest failed: {exc}"},
        ) from exc

    if not result.success:
        raise HTTPException(
            status_code=500,
            detail={"error": result.error or "reingest failed"},
        )
    return {
        "success": True,
        "event_id": result.event_id,
        "vector_id": result.vector_id,
    }
