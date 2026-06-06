"""Control Node API — backed by db_store Tier 2 + ControlNodePhase.

Removes the legacy TrainingLayerStore / ControlNodeBuilder dependency;
calls ``db.graph`` (Tier 2a) + trainer.ControlNodePhase directly.
LLM-based post-processing (narrative_summary / structural_summary) is
performed in subsequent phases.

Endpoints:
    GET  /control-nodes                — list CNs for a domain
    GET  /control-nodes/{node_id}      — CN detail + hierarchy
    GET  /control-nodes/{node_id}/events — Events linked to a CN
    POST /control-nodes                — manual creation (event_ids)
    POST /control-nodes/auto-build     — auto-cluster via ControlNodePhase
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_db_dep, get_graph_dep, sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["control"])


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/control-nodes")
def list_control_nodes(
    domain: str = Query(..., description="Domain name"),
    include_seed: bool = Query(False),
    graph: Any = Depends(get_graph_dep),
) -> dict:
    """List Control Nodes (training_method='structural_summary') for a domain."""
    nodes = graph.list_control_nodes(domain=domain, include_seed=include_seed)
    return sanitize({"control_nodes": nodes})


@router.get("/control-nodes/{node_id}")
def get_control_node(
    node_id: str,
    graph: Any = Depends(get_graph_dep),
) -> dict:
    """Retrieve Control Node detail + hierarchy (ancestors/children)."""
    cg = graph.get_context_group(node_id)
    if not cg:
        return {"error": "Control Node not found."}

    hierarchy = graph.get_cg_hierarchy(node_id)
    return sanitize({
        "control_node": {
            **cg,
            "children": hierarchy.get("children", []),
            "ancestors": hierarchy.get("ancestors", []),
        },
    })


@router.get("/control-nodes/{node_id}/events")
def get_control_node_events(
    node_id: str,
    graph: Any = Depends(get_graph_dep),
) -> dict:
    """List Events linked to a Control Node + narrative_summary."""
    cg = graph.get_context_group(node_id)
    if not cg:
        return {"error": "Control Node not found."}

    events = graph.get_cg_events(node_id)
    return sanitize({
        "control_node": {"id": cg["id"], "name": cg.get("name", "")},
        "events": events,
        "narrative_summary": cg.get("narrative_summary") or "",
    })


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _stage_for_count(count: int) -> str:
    """Same stage assignment rules as ControlNodePhase._stage_for_count."""
    if count <= 4:
        return "seed"
    if count <= 9:
        return "sprout"
    if count <= 19:
        return "established"
    return "core"


@router.post("/control-nodes")
def build_control_node(
    body: dict,
    graph: Any = Depends(get_graph_dep),
) -> dict:
    """Manually create a Control Node — register a set of event_ids as a cluster."""
    name = str(body.get("name") or "").strip()
    domain = str(body.get("domain") or "").strip()
    event_ids = body.get("event_ids") or []
    parent_id = body.get("parent_id")
    order_index = body.get("order_index")

    if not name or not domain or not event_ids:
        return {"error": "name, domain, and event_ids are required."}
    if not isinstance(event_ids, list):
        return {"error": "event_ids must be an array."}

    from k2g.trainer.models import new_cg_id

    cg_id = new_cg_id()
    stage = _stage_for_count(len(event_ids))
    try:
        graph.create_context_group({
            "id": cg_id,
            "name": name,
            "stage": stage,
            "cluster_source": "event",
            "training_method": "structural_summary",
            "confidence": 0.5,
            "member_count_own": len(event_ids),
            "member_count_total": len(event_ids),
            "order_index": int(order_index) if order_index is not None else 0,
            "domain": domain,
        })
        if stage != "seed":
            for eid in event_ids:
                graph.link_event_belongs_to_context(eid, cg_id, kind="member")
        for eid in event_ids:
            graph.link_cg_realized_as(cg_id, eid)
        if parent_id:
            graph.link_cg_child_of(cg_id, parent_id, depth=1)
    except Exception as exc:  # noqa: BLE001
        logger.error("Control Node manual creation failed: %s", exc)
        return {"error": f"creation failed: {exc}"}

    return {"id": cg_id, "name": name, "stage": stage,
            "message": "Control Node created successfully"}


@router.post("/control-nodes/auto-build")
def auto_build_control_nodes(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Auto-cluster a domain via ControlNodePhase — Union-Find(seq+jaccard)."""
    from k2g.trainer.control_node import ControlNodePhase

    domain = str(body.get("domain") or "").strip()
    if not domain:
        return {"error": "domain is required."}

    phase = ControlNodePhase(
        seq_weight=float(body.get("seq_weight", 0.5)),
        jac_weight=float(body.get("jac_weight", 0.5)),
        edge_threshold=float(body.get("edge_threshold", 0.3)),
        min_cluster_size=int(body.get("min_cluster_size", 2)),
    )
    result = phase.run(db, domain=domain)
    if not result.success:
        return {"error": result.error or "Control Node auto-build failed"}

    n = int(result.counts.get("cg_created", 0))
    return sanitize({
        "counts": result.counts,
        "created": n,
        "message": f"{n} Control Node(s) created automatically",
    })
