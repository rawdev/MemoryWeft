"""Neighbor traversal MCP tool (k2g_neighbors).

1-N hop neighbor exploration for nodes (entity / event / cg / etg / plan).
Each hop uses a SQL JOIN; multi-hop traversal is handled in Python BFS.
RECURSIVE CTEs are intentionally avoided — they put heavy load on the
Postgres planner and risk infinite traversal.

Supported node kinds:
- entity, event, cg, etg, plan, direction

Supported relationship (rel) types:
- ``participated_in``         — entity <-> event (bidirectional)
- ``entity_connection``       — entity <-> entity
- ``event_member_of``         — event <-> group
- ``event_belongs_to_context`` — event <-> (CG | ETG)
- ``event_sequential_next``   — event -> event (directed)
- ``plan_from``               — event <-> (CG | PLN)
- ``plan_next``               — (CG | PLN | DIR) <-> (CG | PLN | DIR)
- ``realized_as``             — (CG | PLN | DIR) <-> event
- ``all``                     — all of the above (controlled via cap)

Limits: hop <= 3, per-hop LIMIT 200, total visited cap 1000. Results are
truncated when any cap is reached.
"""

from __future__ import annotations

import logging
from typing import Any

from k2g.mcp.factory import Deps

logger = logging.getLogger(__name__)

MAX_HOP = 3
PER_HOP_LIMIT = 200
TOTAL_VISITED_CAP = 1000

NODE_KIND_ENUM = ("entity", "event", "cg", "etg", "group", "plan", "direction")
REL_ENUM = (
    "all",
    "participated_in",
    "entity_connection",
    "event_member_of",
    "event_belongs_to_context",
    "event_sequential_next",
    "plan_from",
    "plan_next",
    "realized_as",
)


def _expand_one_hop(
    conn: Any, backend: str, *, node_id: str, node_kind: str, rel: str,
) -> list[dict[str, Any]]:
    """Expand one hop — return direct neighbors of the given node.

    Each neighbor dict contains ``{id, kind, rel, direction}``.
    ``direction`` is ``out`` | ``in`` | ``both`` (undirected edges such as
    entity_connection use ``both``).
    """
    ph = "%s" if backend == "postgres" else "?"
    cur = conn.cursor()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(nid: str, kind: str, used_rel: str, direction: str) -> None:
        if not nid:
            return
        key = (nid, kind)
        if key in seen:
            return
        seen.add(key)
        out.append({"id": nid, "kind": kind, "rel": used_rel, "direction": direction})

    try:
        # When rel='all', try every applicable relationship; otherwise use the one given.
        rels_to_try = [rel] if rel != "all" else list(REL_ENUM[1:])

        for r in rels_to_try:
            try:
                if r == "participated_in":
                    if node_kind == "entity":
                        cur.execute(
                            f"SELECT event_id FROM participated_in "
                            f"WHERE entity_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            eid = row[0] if not hasattr(row, "keys") else row["event_id"]
                            add(eid, "event", "participated_in", "out")
                    elif node_kind == "event":
                        cur.execute(
                            f"SELECT entity_id FROM participated_in "
                            f"WHERE event_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            ent = row[0] if not hasattr(row, "keys") else row["entity_id"]
                            add(ent, "entity", "participated_in", "in")

                elif r == "entity_connection":
                    if node_kind == "entity":
                        cur.execute(
                            f"SELECT a_id, b_id FROM entity_connection "
                            f"WHERE a_id = {ph} OR b_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id, node_id),
                        )
                        for row in cur.fetchall():
                            a, b = (
                                (row["a_id"], row["b_id"])
                                if hasattr(row, "keys")
                                else (row[0], row[1])
                            )
                            other = b if a == node_id else a
                            add(other, "entity", "entity_connection", "both")

                elif r == "event_member_of":
                    if node_kind == "event":
                        cur.execute(
                            f"SELECT group_id FROM event_member_of "
                            f"WHERE event_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            gid = row[0] if not hasattr(row, "keys") else row["group_id"]
                            add(gid, "group", "event_member_of", "out")
                    elif node_kind == "group":
                        # group → event (reverse) + group → parent (hierarchy)
                        cur.execute(
                            f"SELECT event_id FROM event_member_of "
                            f"WHERE group_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            eid = row[0] if not hasattr(row, "keys") else row["event_id"]
                            add(eid, "event", "event_member_of", "in")
                        # group hierarchy — parent / children
                        cur.execute(
                            f"SELECT parent_id FROM groups WHERE id = {ph}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            pid = row[0] if not hasattr(row, "keys") else row["parent_id"]
                            if pid:
                                add(pid, "group", "group_hierarchy", "out")
                        cur.execute(
                            f"SELECT id FROM groups WHERE parent_id = {ph} "
                            f"LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            cid = row[0] if not hasattr(row, "keys") else row["id"]
                            add(cid, "group", "group_hierarchy", "in")

                elif r == "event_belongs_to_context":
                    if node_kind == "event":
                        cur.execute(
                            f"SELECT target_id, target_kind FROM event_belongs_to_context "
                            f"WHERE event_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            tid, tk = (
                                (row["target_id"], row["target_kind"])
                                if hasattr(row, "keys")
                                else (row[0], row[1])
                            )
                            kind_norm = "cg" if str(tk).strip() == "CG" else "etg"
                            add(tid, kind_norm, "event_belongs_to_context", "out")
                    elif node_kind in ("cg", "etg"):
                        tk_lit = "CG " if node_kind == "cg" else "ETG"
                        cur.execute(
                            f"SELECT event_id FROM event_belongs_to_context "
                            f"WHERE target_id = {ph} AND target_kind = {ph} "
                            f"LIMIT {PER_HOP_LIMIT}",
                            (node_id, tk_lit),
                        )
                        for row in cur.fetchall():
                            eid = row[0] if not hasattr(row, "keys") else row["event_id"]
                            add(eid, "event", "event_belongs_to_context", "in")

                elif r == "event_sequential_next":
                    if node_kind == "event":
                        cur.execute(
                            f"SELECT next_id FROM event_sequential_next "
                            f"WHERE prev_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            nxt = row[0] if not hasattr(row, "keys") else row["next_id"]
                            add(nxt, "event", "event_sequential_next", "out")
                        cur.execute(
                            f"SELECT prev_id FROM event_sequential_next "
                            f"WHERE next_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            prv = row[0] if not hasattr(row, "keys") else row["prev_id"]
                            add(prv, "event", "event_sequential_next", "in")

                elif r == "plan_from":
                    if node_kind == "event":
                        cur.execute(
                            f"SELECT target_id, target_kind FROM plan_from "
                            f"WHERE event_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            tid, tk = (
                                (row["target_id"], row["target_kind"])
                                if hasattr(row, "keys")
                                else (row[0], row[1])
                            )
                            kind_norm = "cg" if str(tk).strip() == "CG" else "plan"
                            add(tid, kind_norm, "plan_from", "out")

                elif r == "plan_next":
                    cur.execute(
                        f"SELECT to_id, to_kind FROM plan_next "
                        f"WHERE from_id = {ph} LIMIT {PER_HOP_LIMIT}",
                        (node_id,),
                    )
                    for row in cur.fetchall():
                        tid, tk = (
                            (row["to_id"], row["to_kind"])
                            if hasattr(row, "keys")
                            else (row[0], row[1])
                        )
                        kind_norm = {"CG ": "cg", "PLN": "plan", "DIR": "direction"}.get(
                            str(tk), "plan"
                        )
                        add(tid, kind_norm, "plan_next", "out")

                elif r == "realized_as":
                    if node_kind in ("cg", "plan", "direction"):
                        fk_lit = {"cg": "CG ", "plan": "PLN", "direction": "DIR"}[node_kind]
                        cur.execute(
                            f"SELECT event_id FROM realized_as "
                            f"WHERE from_id = {ph} AND from_kind = {ph} "
                            f"LIMIT {PER_HOP_LIMIT}",
                            (node_id, fk_lit),
                        )
                        for row in cur.fetchall():
                            eid = row[0] if not hasattr(row, "keys") else row["event_id"]
                            add(eid, "event", "realized_as", "out")
                    elif node_kind == "event":
                        cur.execute(
                            f"SELECT from_id, from_kind FROM realized_as "
                            f"WHERE event_id = {ph} LIMIT {PER_HOP_LIMIT}",
                            (node_id,),
                        )
                        for row in cur.fetchall():
                            fid, fk = (
                                (row["from_id"], row["from_kind"])
                                if hasattr(row, "keys")
                                else (row[0], row[1])
                            )
                            kind_norm = {"CG ": "cg", "PLN": "plan", "DIR": "direction"}.get(
                                str(fk), "plan"
                            )
                            add(fid, kind_norm, "realized_as", "in")
            except Exception:  # noqa: BLE001
                # Isolate failures so one rel's error does not block others
                logger.debug("expand_one_hop failed for rel=%s", r, exc_info=True)
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass

    return out


def neighbors_tool(
    deps: Deps,
    *,
    node_id: str,
    node_kind: str = "entity",
    rel: str = "all",
    hop: int = 1,
) -> dict[str, Any]:
    """Traverse 1-N hop neighbors of a graph node.

    Args:
        deps: MCP Deps.
        node_id: Starting node id.
        node_kind: ``entity | event | cg | etg | plan | direction``.
        rel: Relationship type from REL_ENUM. ``all`` tries every rel.
        hop: 1–3 (hard cap at 3).

    Returns:
        ``{neighbors: [{id, kind, rel, direction, hop}], truncated,
        total_visited}``.
    """
    if node_kind not in NODE_KIND_ENUM:
        return {
            "error": f"unknown node_kind: {node_kind!r}",
            "available_kinds": list(NODE_KIND_ENUM),
        }
    if rel not in REL_ENUM:
        return {
            "error": f"unknown rel: {rel!r}",
            "available_rels": list(REL_ENUM),
        }
    bounded_hop = max(1, min(int(hop), MAX_HOP))

    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    conn = graph._conn

    # BFS — advance hop by hop through frontier sets
    visited: set[tuple[str, str]] = {(node_id, node_kind)}
    frontier: list[tuple[str, str]] = [(node_id, node_kind)]
    neighbors: list[dict[str, Any]] = []
    truncated = False

    try:
        for current_hop in range(1, bounded_hop + 1):
            next_frontier: list[tuple[str, str]] = []
            for fid, fkind in frontier:
                if len(visited) >= TOTAL_VISITED_CAP:
                    truncated = True
                    break
                hits = _expand_one_hop(
                    conn, backend, node_id=fid, node_kind=fkind, rel=rel,
                )
                for h in hits:
                    key = (h["id"], h["kind"])
                    if key in visited:
                        continue
                    visited.add(key)
                    neighbors.append({**h, "hop": current_hop})
                    next_frontier.append(key)
                    if len(visited) >= TOTAL_VISITED_CAP:
                        truncated = True
                        break
            if truncated or not next_frontier:
                break
            frontier = next_frontier
    finally:
        # Clean up any open transaction
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

    response: dict[str, Any] = {
        "node": {"id": node_id, "kind": node_kind},
        "rel": rel,
        "hop": bounded_hop,
        "neighbors": neighbors,
        "total_visited": len(visited),
        "truncated": truncated,
    }
    # Enrich response with matching contracts based on node_kind and rel
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    active = match_for_tool("mweft_neighbors", node_kind=node_kind, rel=rel)
    if active:
        response["context"] = {"active_contracts": active}
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev
    return response


__all__ = [
    "neighbors_tool",
    "NODE_KIND_ENUM",
    "REL_ENUM",
    "MAX_HOP",
    "PER_HOP_LIMIT",
    "TOTAL_VISITED_CAP",
]
