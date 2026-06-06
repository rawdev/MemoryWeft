"""Search Explorer — recursive Entity/Event drill-down (Manager UI Page 3).

Read-only. Implements the 124-05 Page 3 model: a search yields an Entity-list or
Event-list; selecting an item drills into the next level under a *cumulatively
narrowing* filter (Entity → co-occurrence Entity list + Event list; Event →
participant Entity list). Per the spec's cost note, unconstrained single-entity
co-occurrence uses the prebuilt ``entity_connection``; otherwise a live
``participated_in`` self-join over the filtered event set is used.

Filter body (shared): ``{domain, tag_ids?, date_from?, date_to?, entity_ids?}``
    (internal SQL keeps the event_member_of.group_id column.)
where ``entity_ids`` is an AND constraint (an event must include all of them).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_db_dep, get_stores_dep, sanitize
from k2g.web.routes._sql import (  # _sqlq* alias: avoid clashing with the route's q(query) param
    not_deprecated,
    ph as _ph,
    q as _sqlq,
    q_all as _sqlq_all,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search-explorer"])


def _rows_to_dicts(cur, keys: list[str]) -> list[dict]:
    out: list[dict] = []
    try:
        for r in cur.fetchall():
            if isinstance(r, dict):
                out.append({k: r.get(k) for k in keys})
            elif hasattr(r, "keys"):
                out.append({k: r[k] for k in keys})
            else:
                out.append({k: r[i] for i, k in enumerate(keys)})
    finally:
        try:  # avoid request-scope cursor accumulation — fetch done, so close
            cur.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _event_filter(f: dict, conn: Any, alias: str = "e") -> tuple[str, list]:
    """Build an AND-joined WHERE fragment (no leading WHERE) + params for the
    filtered event set, applied to ``events`` aliased as ``alias``."""
    ph = _ph(conn)
    conds: list[str] = [not_deprecated(f"{alias}.deprecated", conn)]
    params: list[Any] = []

    dom = (f.get("domain") or "").strip()
    if dom:
        conds.append(f"{alias}.domain = {ph}")
        params.append(dom)

    df = (f.get("date_from") or "").strip()
    dt = (f.get("date_to") or "").strip()
    if df:
        conds.append(f"COALESCE({alias}.timestamp, {alias}.created_at) >= {ph}")
        params.append(df)
    if dt:
        conds.append(f"COALESCE({alias}.timestamp, {alias}.created_at) <= {ph}")
        params.append(dt)

    tids = [t for t in (f.get("tag_ids") or []) if t]
    if tids:
        marks = ",".join([ph] * len(tids))
        conds.append(
            f"EXISTS (SELECT 1 FROM event_member_of m "
            f"WHERE m.event_id = {alias}.id AND m.group_id IN ({marks}))"
        )
        params.extend(tids)

    eids = [e for e in (f.get("entity_ids") or []) if e]
    if eids:
        marks = ",".join([ph] * len(eids))
        match = str(f.get("entity_match") or "and").lower()
        if match == "or":
            # event includes ANY of the entities
            conds.append(
                f"{alias}.id IN (SELECT event_id FROM participated_in "
                f"WHERE entity_id IN ({marks}))"
            )
        else:
            # event includes ALL of the entities (default)
            conds.append(
                f"{alias}.id IN (SELECT event_id FROM participated_in "
                f"WHERE entity_id IN ({marks}) GROUP BY event_id "
                f"HAVING COUNT(DISTINCT entity_id) = {len(eids)})"
            )
        params.extend(eids)

    return " AND ".join(conds), params


@router.get("/explore/entities")
def explore_entities(
    domain: str = Query(...),
    q: str = Query("", description="name substring (case-insensitive)"),
    ev_min: int = Query(0, ge=0),
    ev_max: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=500),
    exclude_stopword: bool = Query(
        False, description="True excludes user_tag='stopword' entities (Analysis page)"),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Entity text search bounded by participation-count [ev_min, ev_max].

    ``exclude_stopword=True`` -> excludes background words marked user_tag='stopword'.
    """
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = _ph(conn)
    having = f"HAVING COUNT(p.event_id) >= {ph}"
    params: list[Any] = [domain, f"%{q.lower()}%", int(ev_min)]
    if ev_max is not None:
        having += f" AND COUNT(p.event_id) <= {ph}"
        params.append(int(ev_max))
    stopword_clause = (
        " AND (en.user_tag IS NULL OR en.user_tag != 'stopword')"
        if exclude_stopword else ""
    )
    sql = (
        f"SELECT en.id, en.name, en.type, COUNT(p.event_id) AS event_count "
        f"FROM entities en LEFT JOIN participated_in p ON p.entity_id = en.id "
        f"WHERE en.domain = {ph} AND {not_deprecated('en.deprecated', conn)} "
        f"AND LOWER(en.name) LIKE {ph}{stopword_clause} "
        f"GROUP BY en.id, en.name, en.type {having} "
        f"ORDER BY event_count DESC, en.name ASC LIMIT {ph}"
    )
    params.append(int(limit))
    cur = _sqlq(conn,sql, tuple(params))
    return sanitize({"kind": "entity", "rows": _rows_to_dicts(
        cur, ["id", "name", "type", "event_count"])})


def _enrich_event_rows(conn, ids: list[str]) -> dict[str, dict]:
    """timestamp/order_index/summary/entity_count for a set of event ids."""
    if not ids:
        return {}
    ph = _ph(conn)
    marks = ",".join([ph] * len(ids))
    cur = _sqlq(conn,
        f"SELECT e.id, e.timestamp, e.order_index, e.summary, "
        f"(SELECT COUNT(*) FROM participated_in p WHERE p.event_id = e.id) AS entity_count "
        f"FROM events e WHERE e.id IN ({marks})",
        tuple(ids),
    )
    out: dict[str, dict] = {}
    for r in _rows_to_dicts(cur, ["id", "timestamp", "order_index", "summary", "entity_count"]):
        out[r["id"]] = r
    return out


@router.post("/explore/events")
def explore_events(
    body: dict,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Filtered event list (timestamp + summary + entity_count).

    Body: ``{filter, page?, size?, query?}``. Blank ``query`` → structured list
    (filter + recency). Non-blank ``query`` → semantic search *within* the
    structured filter set (embed → vector search restricted to filtered events).
    """
    graph = stores["graph"]
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    f = body.get("filter") or {}
    page = max(1, int(body.get("page", 1)))
    size = min(200, max(1, int(body.get("size", 50))))
    query = (body.get("query") or "").strip()
    ph = _ph(conn)
    clause, params = _event_filter(f, conn)

    if query:
        emb = stores.get("embedding")
        vec = stores.get("vector")
        if emb is None or vec is None:
            return {"error": "semantic search unavailable (embedding/vector store missing)"}
        try:
            qvec = emb.embed(query)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"embedding failed: {exc}"}
        only_domain = not (
            f.get("tag_ids") or f.get("date_from") or f.get("date_to") or f.get("entity_ids")
        )
        if only_domain:
            hits = vec.search(qvec, filter_domain=f.get("domain") or None, limit=size)
        else:
            cand = [r[0] for r in _sqlq_all(conn,
                f"SELECT e.id FROM events e WHERE {clause}", tuple(params))]
            if not cand:
                return sanitize({"kind": "event", "rows": [], "semantic": True})
            hits = vec.search(
                qvec, filter_search_targets=[(f.get("domain"), cand)], limit=size)
        enrich = _enrich_event_rows(conn, [h["id"] for h in hits])
        rows = []
        for h in hits:
            base = enrich.get(h["id"], {"id": h["id"], "summary": h.get("summary")})
            base["distance"] = h.get("distance")
            rows.append(base)
        return sanitize({"kind": "event", "rows": rows, "semantic": True})

    sql = (
        f"SELECT e.id, e.timestamp, e.order_index, e.summary, "
        f"(SELECT COUNT(*) FROM participated_in p WHERE p.event_id = e.id) AS entity_count "
        f"FROM events e WHERE {clause} "
        f"ORDER BY COALESCE(e.timestamp, e.created_at) DESC "
        f"LIMIT {ph} OFFSET {ph}"
    )
    params = params + [size, (page - 1) * size]
    cur = _sqlq(conn,sql, tuple(params))
    rows = _rows_to_dicts(cur, ["id", "timestamp", "order_index", "summary", "entity_count"])
    return sanitize({"kind": "event", "rows": rows, "page": page, "size": size})


@router.post("/explore/cooccurrence")
def explore_cooccurrence(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Entities co-occurring across the filtered event set (count desc).

    Fast path: a single ``entity_ids`` with no group/date constraint uses the
    prebuilt ``entity_connection``. Otherwise a live self-join over the filtered
    events. The constraint entities are excluded from the result.
    """
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    f = body.get("filter") or {}
    limit = min(500, max(1, int(body.get("limit", 50))))
    ph = _ph(conn)
    eids = [e for e in (f.get("entity_ids") or []) if e]
    no_other = not (f.get("tag_ids") or f.get("date_from") or f.get("date_to"))

    if len(eids) == 1 and no_other:
        x = eids[0]
        sql = (
            f"SELECT en.id, en.name, en.type, ec.event_count AS cooc "
            f"FROM entity_connection ec "
            f"JOIN entities en ON en.id = CASE WHEN ec.a_id = {ph} THEN ec.b_id ELSE ec.a_id END "
            f"WHERE (ec.a_id = {ph} OR ec.b_id = {ph}) "
            f"AND {not_deprecated('en.deprecated', conn)} "
            f"ORDER BY cooc DESC LIMIT {ph}"
        )
        cur = _sqlq(conn,sql, (x, x, x, int(limit)))
        return sanitize({"kind": "entity", "rows": _rows_to_dicts(
            cur, ["id", "name", "type", "cooc"]), "via": "entity_connection"})

    clause, params = _event_filter(f, conn)
    exclude = ""
    if eids:
        marks = ",".join([ph] * len(eids))
        exclude = f"AND p.entity_id NOT IN ({marks})"
    sql = (
        f"SELECT en.id, en.name, en.type, COUNT(*) AS cooc "
        f"FROM participated_in p JOIN events e ON e.id = p.event_id "
        f"JOIN entities en ON en.id = p.entity_id "
        f"WHERE {clause} {exclude} AND {not_deprecated('en.deprecated', conn)} "
        f"GROUP BY en.id, en.name, en.type ORDER BY cooc DESC LIMIT {ph}"
    )
    cur = _sqlq(conn,sql, tuple(params + eids + [int(limit)]))
    return sanitize({"kind": "entity", "rows": _rows_to_dicts(
        cur, ["id", "name", "type", "cooc"]), "via": "self_join"})


@router.post("/explore/event-participants")
def explore_event_participants(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Participants of an event + influence (their count within the filter set).

    Body: ``{event_id, filter?}``.
    """
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    event_id = (body.get("event_id") or "").strip()
    if not event_id:
        return {"error": "event_id is required"}
    f = body.get("filter") or {}
    ph = _ph(conn)

    cur = _sqlq(conn,
        f"SELECT en.id, en.name, en.type FROM participated_in p "
        f"JOIN entities en ON en.id = p.entity_id WHERE p.event_id = {ph} "
        f"ORDER BY en.name ASC",
        (event_id,),
    )
    parts = _rows_to_dicts(cur, ["id", "name", "type"])
    pids = [p["id"] for p in parts]

    influence: dict[str, int] = {}
    if pids:
        clause, params = _event_filter(f, conn)
        marks = ",".join([ph] * len(pids))
        influence = {row[0]: int(row[1]) for row in _sqlq_all(conn,
            f"SELECT p.entity_id, COUNT(*) FROM participated_in p "
            f"JOIN events e ON e.id = p.event_id "
            f"WHERE {clause} AND p.entity_id IN ({marks}) "
            f"GROUP BY p.entity_id",
            tuple(params + pids),
        )}

    for p in parts:
        p["influence"] = influence.get(p["id"], 0)
    parts.sort(key=lambda x: (-x["influence"], x["name"] or ""))
    return sanitize({"kind": "entity", "event_id": event_id, "rows": parts})



@router.get("/explore/discovery")
def explore_discovery(
    domain: str = Query(...),
    kind: str = Query("new", description="new | recent_pairs | returning"),
    limit: int = Query(20, ge=1, le=100),
    recent_pool: int = Query(200, ge=10, le=2000),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Discovery lists ("misc"): new / recent co-occurring pairs /
    dormant-then-returning entities. Heuristic, on-demand (not a hot path).

    Exceptions are returned as ``{"error": ...}`` (HTTP 200) so the UI can
    render a meaningful message instead of a generic 500 page.
    """
    try:
        return _explore_discovery_impl(domain, kind, limit, recent_pool, db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("explore/discovery failed (kind=%s, domain=%s)", kind, domain)
        return {"error": f"discovery({kind}) failed: {type(exc).__name__}: {exc}"}


def _explore_discovery_impl(
    domain: str, kind: str, limit: int, recent_pool: int, db: Any,
) -> dict:
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = _ph(conn)

    if kind == "new":
        cur = _sqlq(conn,
            f"SELECT id, name, type, created_at FROM entities "
            f"WHERE domain = {ph} AND {not_deprecated('deprecated', conn)} "
            f"ORDER BY created_at DESC LIMIT {ph}",
            (domain, int(limit)),
        )
        return sanitize({"kind": "new", "rows": _rows_to_dicts(
            cur, ["id", "name", "type", "created_at"])})

    if kind == "recent_pairs":
        cur = _sqlq(conn,
            f"""
            WITH recent AS (
                SELECT id FROM events
                 WHERE domain = {ph} AND {not_deprecated('deprecated', conn)}
                 ORDER BY COALESCE(timestamp, created_at) DESC LIMIT {ph}
            )
            SELECT pa.entity_id AS a_id, ea.name AS a_name,
                   pb.entity_id AS b_id, eb.name AS b_name, COUNT(*) AS cnt
              FROM participated_in pa
              JOIN participated_in pb
                ON pa.event_id = pb.event_id AND pa.entity_id < pb.entity_id
              JOIN entities ea ON ea.id = pa.entity_id
              JOIN entities eb ON eb.id = pb.entity_id
             WHERE pa.event_id IN (SELECT id FROM recent)
             GROUP BY pa.entity_id, pb.entity_id, ea.name, eb.name
             ORDER BY cnt DESC LIMIT {ph}
            """,
            (domain, int(recent_pool), int(limit)),
        )
        return sanitize({"kind": "recent_pairs", "rows": _rows_to_dicts(
            cur, ["a_id", "a_name", "b_id", "b_name", "cnt"])})

    if kind == "returning":
        # window CTE: per-entity LAG(prev) + latest-row flag; take a recent pool,
        # then rank by the gap between last and previous activity in Python
        # (portable across sqlite/pg — no julianday/EXTRACT).
        cur = _sqlq(conn,
            f"""
            WITH acts AS (
                SELECT p.entity_id, p.created_at,
                       LAG(p.created_at) OVER (
                           PARTITION BY p.entity_id ORDER BY p.created_at) AS prev,
                       ROW_NUMBER() OVER (
                           PARTITION BY p.entity_id ORDER BY p.created_at DESC) AS rn
                  FROM participated_in p
                  JOIN entities e ON e.id = p.entity_id
                 WHERE e.domain = {ph} AND {not_deprecated('e.deprecated', conn)}
            )
            SELECT a.entity_id AS id, en.name,
                   a.created_at AS last_seen, a.prev AS prev_seen
              FROM acts a JOIN entities en ON en.id = a.entity_id
             WHERE a.rn = 1 AND a.prev IS NOT NULL
             ORDER BY a.created_at DESC LIMIT {ph}
            """,
            (domain, int(recent_pool)),
        )
        cand = _rows_to_dicts(cur, ["id", "name", "last_seen", "prev_seen"])

        from datetime import datetime

        def _parse(s: Any):
            try:
                return datetime.fromisoformat(str(s).replace("Z", "").strip()[:26])
            except Exception:  # noqa: BLE001
                return None

        for c in cand:
            a, b = _parse(c["last_seen"]), _parse(c["prev_seen"])
            c["gap_days"] = round((a - b).total_seconds() / 86400.0, 1) if (a and b) else None
        cand = [c for c in cand if c["gap_days"] is not None]
        cand.sort(key=lambda c: -c["gap_days"])
        return sanitize({"kind": "returning", "rows": cand[: int(limit)]})

    return {"error": f"kind must be new|recent_pairs|returning, got {kind!r}"}
