"""Temporal flow MCP tool (k2g_temporal_flow).

Query events in chronological order scoped to an entity, a ContextGroup, or
an entire domain. Sorted by events.timestamp + order_index. Useful for
understanding which source produced a new event and for reasoning about the
causal sequence of events.
"""

from __future__ import annotations

import logging
from typing import Any

from k2g.mcp.factory import Deps
from k2g.mcp.scope import resolve_search_scope, scope_to_str

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def temporal_flow_tool(
    deps: Deps,
    *,
    entity_id: str | None = None,
    cg_id: str | None = None,
    days: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return events in chronological order.

    Argument priority:
    1. If ``entity_id`` is given, return events via PARTICIPATED_IN.
    2. If ``cg_id`` is given, return events via event_belongs_to_context.
    3. If neither is given, return domain-wide events (limit enforced).

    The search scope (domain and group) is determined by the server via
    ``resolve_search_scope(deps)``. As a single-timeline tool,
    temporal_flow uses only the *first target* from the scope for
    domain-wide queries. See ``searched_scope`` in the response for the
    actual scope used.

    Args:
        deps: MCP Deps.
        entity_id: Return events for this entity.
        cg_id: Return events for this ContextGroup.
        days: Restrict to the last N days.
        limit: 1–200.

    Returns:
        ``{events: [...], total, mode, searched_scope}``.
    """
    bounded_limit = max(1, min(int(limit), MAX_LIMIT))

    # The search scope is determined by the server. For domain-wide queries
    # (no entity_id or cg_id), only the first scope target is used because
    # temporal_flow is a single-timeline tool.
    scope = resolve_search_scope(deps)
    domain: str | None = None
    group: str | None = None
    if not entity_id and not cg_id and scope:
        first = scope[0]
        domain = first.domain or None
        group = first.group or None

    # Group filter — build event_id allowlist for post-filtering
    filter_event_ids: list[str] | None = None
    if group:
        filter_event_ids = deps.graph.list_event_ids_by_group(group)
        if not filter_event_ids:
            return {
                "mode": "group",
                "anchor": {"group": group},
                "events": [],
                "total": 0,
                "truncated": False,
                "searched_scope": scope_to_str(scope),
            }

    if entity_id:
        mode = "entity"
    elif cg_id:
        mode = "cg"
    elif domain or filter_event_ids is not None:
        mode = "domain"
    else:
        return {
            "error": (
                "entity_id or cg_id is required "
                "(search scope is empty, domain-wide flow unavailable)"
            )
        }

    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    conn = graph._conn
    ph = "%s" if backend == "postgres" else "?"

    # Build SQL query
    if mode == "entity":
        base_sql = (
            f"SELECT e.id, e.domain, e.timestamp, e.order_index, e.summary, "
            f"  e.vector_id, e.influence_score "
            f"FROM events e "
            f"JOIN participated_in p ON p.event_id = e.id "
            f"WHERE p.entity_id = {ph}"
        )
        params: list[Any] = [entity_id]
    elif mode == "cg":
        base_sql = (
            f"SELECT e.id, e.domain, e.timestamp, e.order_index, e.summary, "
            f"  e.vector_id, e.influence_score "
            f"FROM events e "
            f"JOIN event_belongs_to_context ebc ON ebc.event_id = e.id "
            f"WHERE ebc.target_id = {ph} AND ebc.target_kind = {ph}"
        )
        params = [cg_id, "CG "]
    else:  # domain
        base_sql = (
            f"SELECT id, domain, timestamp, order_index, summary, "
            f"  vector_id, influence_score "
            f"FROM events "
            f"WHERE 1=1"
        )
        params = []
        # domain can be a single string or a list
        if domain:
            if isinstance(domain, list):
                placeholders = ", ".join([ph] * len(domain))
                base_sql += f" AND domain IN ({placeholders})"
                params.extend(domain)
            else:
                base_sql += f" AND domain = {ph}"
                params.append(domain)
        # Group filter: restrict to allowed event ids
        if filter_event_ids is not None:
            placeholders = ", ".join([ph] * len(filter_event_ids))
            base_sql += f" AND id IN ({placeholders})"
            params.extend(filter_event_ids)

    # Apply days filter
    if days is not None and days >= 0:
        if backend == "postgres":
            base_sql += f" AND e.timestamp >= NOW() - INTERVAL '{int(days)} days'" \
                if mode in ("entity", "cg") \
                else f" AND timestamp >= NOW() - INTERVAL '{int(days)} days'"
        else:
            ts_col = "e.timestamp" if mode in ("entity", "cg") else "timestamp"
            base_sql += f" AND {ts_col} >= datetime('now', '-{int(days)} days')"

    # Sort order
    if mode in ("entity", "cg"):
        order = "e.timestamp NULLS LAST, e.order_index, e.id"
    else:
        order = "timestamp NULLS LAST, order_index, id"

    sql = f"{base_sql} ORDER BY {order} LIMIT {bounded_limit}"

    cur = conn.cursor()
    try:
        if backend == "postgres":
            cur.execute(f"SET LOCAL statement_timeout = 10000")
        cur.execute(sql, params)
        rows = cur.fetchall()
        if cur.description:
            columns = [d[0] for d in cur.description]
        else:
            columns = []
        out_rows: list[dict[str, Any]] = []
        for r in rows:
            if hasattr(r, "keys"):
                d = dict(r)
            elif isinstance(r, dict):
                d = r
            else:
                d = dict(zip(columns, r))
            # Serialize timestamp to ISO string
            ts = d.get("timestamp")
            if hasattr(ts, "isoformat"):
                d["timestamp"] = ts.isoformat()
            out_rows.append(d)
        response: dict[str, Any] = {
            "mode": mode,
            "anchor": {
                "entity_id": entity_id,
                "cg_id": cg_id,
                "domain": domain,
            },
            "events": out_rows,
            "total": len(out_rows),
            "truncated": len(out_rows) >= bounded_limit,
        }
        # Enrich response with contracts keyed on the anchor's node_kind (mode is the anchor kind)
        from k2g.mcp.contracts import match_for_tool, extract_evidence
        active = match_for_tool(
            "mweft_temporal_flow",
            node_kind=mode if mode in ("entity", "cg") else None,
            rows=out_rows,
        )
        if active:
            response["context"] = {"active_contracts": active}
        ev = extract_evidence(response)
        if ev:
            response["evidence"] = ev
        response["searched_scope"] = scope_to_str(scope)
        return response
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "executed_sql": sql}
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
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


__all__ = ["temporal_flow_tool", "DEFAULT_LIMIT", "MAX_LIMIT"]
