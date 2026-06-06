"""Persona / Vibe / Tension API — persona extraction via ProjectionEngine.

ProjectionEngine combines DbStore + embedding + LLM to compute entity
vector centroids, semantic attention, and tension scores.
`web/deps.startup()` constructs and injects it as a singleton.

Stateless: no writes to K2G Store.  State is owned by browser localStorage.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from k2g.web.deps import get_projection_engine_dep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["persona"])


def _require_engine(engine: Any) -> Any:
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_ready",
                "feature": "ProjectionEngine",
                "note": "ProjectionEngine failed to initialise. Check deps.startup() logs.",
            },
        )
    return engine


@router.post("/persona/extract")
def extract_persona(
    body: dict,
    engine: Any = Depends(get_projection_engine_dep),
) -> dict:
    """Extract an entity persona (centroid + representative events + keywords).

    The ``time_decay`` body field is ignored: the entity vector uses a
    single mean+L2norm algorithm.  Any ``time_decay`` sent by the web UI
    is silently discarded.
    """
    engine = _require_engine(engine)
    entity_id = str(body.get("entity_id") or "").strip()
    domain = str(body.get("domain") or "").strip()
    top_k = int(body.get("top_k") or 3)

    if not entity_id:
        return {"error": "entity_id is required."}
    if not domain:
        return {"error": "domain is required."}

    return engine.extract_persona(
        entity_id=entity_id, domain=domain, top_k=top_k,
    )


@router.post("/vibe/extract")
def extract_vibe(
    body: dict,
    engine: Any = Depends(get_projection_engine_dep),
) -> dict:
    """Extract the vibe of a narrative segment — centroid + keywords of event_ids."""
    engine = _require_engine(engine)
    event_ids = body.get("event_ids") or []
    domain = str(body.get("domain") or "").strip()
    segment_key = str(body.get("segment_key") or "")
    top_k = int(body.get("top_k") or 3)
    narrative_summary = str(body.get("narrative_summary") or "")

    if not event_ids:
        return {"error": "event_ids is required."}
    if not domain:
        return {"error": "domain is required."}
    if not isinstance(event_ids, list):
        return {"error": "event_ids must be an array."}

    return engine.extract_vibe(
        event_ids=[str(e) for e in event_ids],
        domain=domain,
        segment_key=segment_key,
        top_k=top_k,
        narrative_summary=narrative_summary,
    )


@router.get("/persona/tension")
def compute_tension(
    entity_a: str,
    entity_b: str,
    engine: Any = Depends(get_projection_engine_dep),
) -> dict:
    """Inverse cosine similarity between two entity centroids as a tension score."""
    engine = _require_engine(engine)
    if not entity_a or not entity_b:
        return {"error": "entity_a and entity_b are both required."}
    return engine.compute_tension_score(
        entity_id_a=entity_a, entity_id_b=entity_b,
    )
