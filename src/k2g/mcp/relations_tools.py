"""Path-finding MCP tool between two graph nodes (k2g_relations).

Finds paths between nodes a and b using bidirectional BFS — both sides expand
simultaneously until they meet at a common node. Returns 1–3 hop paths.

WITH RECURSIVE CTEs are intentionally avoided — they put heavy load on the
Postgres planner and risk infinite traversal. Python BFS with per-hop LIMIT
and total visited cap is used instead.
"""

from __future__ import annotations

import logging
from typing import Any

from k2g.mcp.factory import Deps
from k2g.mcp.neighbors_tools import (
    MAX_HOP,
    NODE_KIND_ENUM,
    _expand_one_hop,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_HOP = 3
MAX_PATHS_RETURNED = 10
TOTAL_VISITED_CAP = 1000


def relations_tool(
    deps: Deps,
    *,
    node_a_id: str,
    node_a_kind: str,
    node_b_id: str,
    node_b_kind: str,
    max_hop: int = DEFAULT_MAX_HOP,
) -> dict[str, Any]:
    """Find paths between two graph nodes.

    Bidirectional BFS — expands from both a and b simultaneously to find
    meeting nodes. Actual paths are reconstructed from the BFS parent map.

    Args:
        deps: MCP Deps.
        node_a_id, node_a_kind: Starting node.
        node_b_id, node_b_kind: Destination node.
        max_hop: 1–3 (hard cap at 3).

    Returns:
        ``{paths: [[node_a, rel, mid1, rel, ..., node_b], ...], hops_searched,
        truncated, total_visited}``.
    """
    if node_a_kind not in NODE_KIND_ENUM:
        return {"error": f"unknown node_a_kind: {node_a_kind!r}"}
    if node_b_kind not in NODE_KIND_ENUM:
        return {"error": f"unknown node_b_kind: {node_b_kind!r}"}

    bounded_hop = max(1, min(int(max_hop), MAX_HOP))

    if node_a_id == node_b_id and node_a_kind == node_b_kind:
        return {
            "paths": [[{"id": node_a_id, "kind": node_a_kind}]],
            "hops_searched": 0,
            "truncated": False,
            "total_visited": 1,
        }

    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    conn = graph._conn

    # Bidirectional BFS — reconstruct paths from parent maps.
    # parents_a: (id, kind) -> (parent_id, parent_kind, rel)
    # Simplified: track only one parent per node (finds one shortest path).
    parents_a: dict[tuple[str, str], tuple[str, str, str] | None] = {
        (node_a_id, node_a_kind): None
    }
    parents_b: dict[tuple[str, str], tuple[str, str, str] | None] = {
        (node_b_id, node_b_kind): None
    }
    frontier_a: list[tuple[str, str]] = [(node_a_id, node_a_kind)]
    frontier_b: list[tuple[str, str]] = [(node_b_id, node_b_kind)]

    truncated = False
    meeting_points: list[tuple[str, str]] = []

    try:
        # Each iteration expands one side. Alternate sides up to max_hop.
        for current_hop in range(1, bounded_hop + 1):
            if len(parents_a) + len(parents_b) >= TOTAL_VISITED_CAP:
                truncated = True
                break

            # Expand the smaller frontier first (BFS efficiency)
            if len(frontier_a) <= len(frontier_b):
                expanding_side = "a"
                cur_frontier = frontier_a
                cur_parents = parents_a
                other_parents = parents_b
            else:
                expanding_side = "b"
                cur_frontier = frontier_b
                cur_parents = parents_b
                other_parents = parents_a

            next_frontier: list[tuple[str, str]] = []
            for fid, fkind in cur_frontier:
                hits = _expand_one_hop(
                    conn, backend, node_id=fid, node_kind=fkind, rel="all",
                )
                for h in hits:
                    key = (h["id"], h["kind"])
                    if key in cur_parents:
                        continue
                    cur_parents[key] = (fid, fkind, h["rel"])
                    next_frontier.append(key)
                    # meeting check
                    if key in other_parents:
                        meeting_points.append(key)
                    if len(parents_a) + len(parents_b) >= TOTAL_VISITED_CAP:
                        truncated = True
                        break
                if truncated:
                    break

            if expanding_side == "a":
                frontier_a = next_frontier
            else:
                frontier_b = next_frontier

            if meeting_points or truncated:
                break
    finally:
        if backend == "postgres":
            try:
                from psycopg2.extensions import (
                    TRANSACTION_STATUS_INERROR,
                    TRANSACTION_STATUS_INTRANS,
                )
                ts_state = conn.info.transaction_status
                if ts_state in (TRANSACTION_STATUS_INTRANS, TRANSACTION_STATUS_INERROR):
                    conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # Reconstruct paths — trace the a-side and b-side chains from each meeting point
    def trace_to_root(
        parents: dict[tuple[str, str], tuple[str, str, str] | None],
        leaf: tuple[str, str],
    ) -> list[dict[str, Any]]:
        """Trace from leaf back to root — returns [{id, kind, rel_to_next}, ...]."""
        chain: list[dict[str, Any]] = []
        cur: tuple[str, str] | None = leaf
        while cur is not None:
            p = parents.get(cur)
            chain.append({"id": cur[0], "kind": cur[1]})
            if p is None:
                break
            cur = (p[0], p[1])
            # Record which rel was used to reach the next chain entry
            if cur is not None:
                chain[-1]["rel_to_next"] = p[2]
        return chain  # leaf -> root order

    paths: list[list[dict[str, Any]]] = []
    seen_paths: set[tuple[tuple[str, str], ...]] = set()
    for mp in meeting_points:
        a_chain = trace_to_root(parents_a, mp)  # mp -> a_root
        b_chain = trace_to_root(parents_b, mp)  # mp -> b_root
        # Assemble: a_root -> ... -> mp -> ... -> b_root
        a_path = list(reversed(a_chain))
        # The first entry of b_chain (mp) duplicates the last entry of a_path -> skip
        full = a_path + b_chain[1:]
        key = tuple((n["id"], n["kind"]) for n in full)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        paths.append(full)
        if len(paths) >= MAX_PATHS_RETURNED:
            break

    response: dict[str, Any] = {
        "from": {"id": node_a_id, "kind": node_a_kind},
        "to": {"id": node_b_id, "kind": node_b_kind},
        "paths": paths,
        "hops_searched": min(bounded_hop, current_hop if "current_hop" in dir() else bounded_hop),
        "truncated": truncated,
        "total_visited": len(parents_a) + len(parents_b),
        "partial": truncated or len(paths) >= MAX_PATHS_RETURNED,
    }
    # Enrich response with contracts matching node_a/b_kind and edge semantics
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    active = match_for_tool(
        "mweft_relations",
        node_kind=node_a_kind,
        rel="all",
    )
    if active:
        response["context"] = {"active_contracts": active}
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev
    return response


__all__ = [
    "relations_tool",
    "DEFAULT_MAX_HOP",
    "MAX_PATHS_RETURNED",
    "TOTAL_VISITED_CAP",
]
