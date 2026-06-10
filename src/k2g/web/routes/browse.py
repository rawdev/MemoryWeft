"""
Browse API — list Entity/Tag/Domain and add user-created ones.

The surface name "Tag" is the user-facing vocabulary. Internal DB / code identifiers
(the graph store's `list_groups`, `link_or_create_group`, `groups` table) stay `group`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from k2g.web.deps import get_stores_dep, sanitize
from k2g.trainer.community_freshness import resolve_latest_run
from k2g.web.routes._sql import (  # alias: avoid clashing with the search route's q(query) param
    cursor,
    q_all as _sqlq_all,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["browse"])


def _backend_of(graph: Any) -> str:
    """``"postgres"`` or ``"sqlite"`` from the graph store's module.

    Detected on the *store* (``k2g.db_store.{postgres,sqlite}.graph``) — NOT on
    its connection: a psycopg2 connection's module is ``psycopg2.extensions``,
    which has no ``postgres`` substring and would misdetect as sqlite.
    """
    return "postgres" if "postgres" in type(graph).__module__.lower() else "sqlite"


def _not_deprecated(col: str, backend: str) -> str:
    """SQL predicate selecting rows that are NOT deprecated, across backends.

    Postgres stores ``deprecated`` as a real boolean → ``= 0`` / ``= 'f'`` raise
    ``operator does not exist: boolean = integer``; use ``IS NOT TRUE`` (covers
    FALSE and NULL). SQLite stores it as 0/1 or legacy ``'f'``/``'t'`` strings, so
    enumerate the falsy forms (``IS NOT TRUE`` would wrongly include ``'t'``).
    """
    if backend == "postgres":
        return f"({col} IS NOT TRUE)"
    return f"({col} IS NULL OR {col} IN (0, 'f', 'false', 'FALSE'))"


@router.get("/entities")
def list_entities(
    domain: str = Query(..., description="domain name"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    include_deleted: bool = Query(False),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """List entities (paginated)."""
    graph = stores["graph"]
    entities, total = graph.list_entities(
        domain=domain,
        include_deleted=include_deleted,
        page=page,
        size=size,
    )
    return sanitize({"entities": entities, "total": total, "page": page})


@router.get("/entities/search")
def search_entities(
    domain: str = Query(..., description="domain name"),
    q: str = Query(..., min_length=1, description="name substring (case-insensitive)"),
    limit: int = Query(20, ge=1, le=100),
    exclude_stopword: bool = Query(
        False, description="True excludes user_tag='stopword' entities (Analysis page)"),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Case-insensitive substring search over Entity + Tag names.

    For autocomplete after typing in the UI's 'Find entity' box — matches
    ``LOWER(name) LIKE '%<q>%'`` within the domain + LIMIT for a fast return.
    Identical on SQLite/Postgres regardless of sqlite-vec / pgvector.

    Returns both `entities` and `tags` when the same substring matches both. The
    existing `entities` key is kept for autocomplete compatibility; the new `tags`
    key is for a later UI stage.
    """
    graph = stores["graph"]
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    # paramstyle: sqlite '?', psycopg2 '%s'
    backend = _backend_of(graph)
    placeholder = "%s" if backend == "postgres" else "?"
    needle = f"%{q.lower()}%"

    # 1) entities
    stopword_clause = (
        " AND (user_tag IS NULL OR user_tag != 'stopword')"
        if exclude_stopword else ""
    )
    ent_sql = (
        f"SELECT id, name, type, domain "
        f"FROM entities WHERE domain = {placeholder} "
        f"AND {_not_deprecated('deprecated', backend)} "
        f"AND LOWER(name) LIKE {placeholder}{stopword_clause} "
        f"ORDER BY LENGTH(name) ASC, name ASC "
        f"LIMIT {placeholder}"
    )
    rows = _sqlq_all(conn, ent_sql, (domain, needle, int(limit)))
    ent_out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            ent_out.append({k: r.get(k) for k in ("id", "name", "type", "domain")})
        elif hasattr(r, "keys"):
            ent_out.append({k: r[k] for k in ("id", "name", "type", "domain")})
        else:
            ent_out.append({"id": r[0], "name": r[1], "type": r[2], "domain": r[3]})

    # 2) tags — groups table. deprecated has a per-backend form difference, so
    # _not_deprecated covers both postgres (boolean) and sqlite ('f'/'t' strings).
    tag_sql = (
        f"SELECT id, name, domain, parent_id "
        f"FROM groups WHERE domain = {placeholder} "
        f"AND {_not_deprecated('deprecated', backend)} "
        f"AND LOWER(name) LIKE {placeholder} "
        f"ORDER BY LENGTH(name) ASC, name ASC "
        f"LIMIT {placeholder}"
    )
    tag_out: list[dict[str, Any]] = []
    try:
        rows = _sqlq_all(conn, tag_sql, (domain, needle, int(limit)))
        for r in rows:
            if isinstance(r, dict):
                tag_out.append({k: r.get(k) for k in ("id", "name", "domain", "parent_id")})
            elif hasattr(r, "keys"):
                tag_out.append({k: r[k] for k in ("id", "name", "domain", "parent_id")})
            else:
                tag_out.append({"id": r[0], "name": r[1], "domain": r[2], "parent_id": r[3]})
    except Exception:  # noqa: BLE001
        # isolate failures — a tag-side error must not break entity autocomplete.
        tag_out = []

    return sanitize({
        "entities": ent_out,
        "tags": tag_out,
        "query": q,
        "limit": int(limit),
    })


@router.get("/tags")
def list_tags(
    domain: str = Query(..., description="domain name"),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """List the tag tree (internal: groups table).

    Each tag carries a ``tag_type`` (provenance bucket: forced/autotag/
    container/build/unknown) derived from ``groups.source`` so the Manager
    tag editor can filter by type. See ``memory.source_axis.display_type``.
    """
    from k2g.memory.source_axis import display_type

    graph = stores["graph"]
    tags = graph.list_groups(domain=domain)
    for tg in tags:
        if isinstance(tg, dict):
            tg["tag_type"] = display_type(tg.get("source"))
    return sanitize({"tags": tags})


@router.get("/domains")
def list_domains(
    include_empty: bool = Query(
        default=False,
        description="True → registry ∪ data-domains (empty/registered domains "
                    "included, for the UI picker). False → events-only (default).",
    ),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """List domains. events-only by default; ``include_empty`` returns the managed union."""
    graph = stores["graph"]
    domains = graph.list_managed_domains() if include_empty else graph.list_domains()
    return {"domains": domains}


# --- Domain soft-registry admin (domain management in the Edit tab) --------
# list_domains() above stays events-only (the global picker). These manage the
# soft registry: the *managed* list is the union registry ∪ data-domains, so an
# empty/registered domain shows up here to be renamed or deleted.


class DomainNameIn(BaseModel):
    name: str


class DomainRenameIn(BaseModel):
    new: str


@router.get("/domains/managed")
def list_managed_domains(
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Union (registry ∪ events/entities/groups) + per-domain data counts."""
    graph = stores["graph"]
    registered = set(graph.list_registered_domains())
    out = []
    for name in graph.list_managed_domains():
        counts = graph.domain_data_counts(name)
        out.append({
            "name": name,
            "registered": name in registered,
            "deletable": counts["events"] == 0 and counts["entities"] == 0,
            **counts,
        })
    return sanitize({"domains": out})


@router.post("/domains/register")
def register_domain(
    body: DomainNameIn = Body(...),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Add an (empty) domain to the registry."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="domain name is empty")
    added = stores["graph"].register_domain(name)
    return {"ok": True, "name": name, "added": added}


@router.delete("/domains/{name}")
def delete_domain(
    name: str,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Delete a domain — refused (``ok=False, reason='has_data'``) if it has data."""
    return sanitize(stores["graph"].delete_domain(name))


@router.post("/domains/{name}/rename")
def rename_domain(
    name: str,
    body: DomainRenameIn = Body(...),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Relabel a domain across all tables (refused if the target already exists)."""
    return sanitize(stores["graph"].rename_domain(name, (body.new or "").strip()))


@router.post("/entities")
def create_entity(
    body: dict,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Add a user entity (link_or_create + user_tag='user_created')."""
    graph = stores["graph"]
    name = body.get("name", "").strip()
    domain = body.get("domain", "").strip()
    if not name:
        return {"error": "name is required."}
    if not domain:
        return {"error": "domain is required."}

    entity_id = graph.link_or_create_entity(name=name, domain=domain)
    graph.set_user_tag("entity", entity_id, "user_created")

    return {"id": entity_id, "name": name, "lifecycle": "user_created"}


@router.post("/tags")
def create_tag(
    body: dict,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Add a user tag (internal: link_or_create_group + lifecycle='user_created')."""
    graph = stores["graph"]
    name = body.get("name", "").strip()
    domain = body.get("domain", "").strip()
    parent_tag_id = body.get("parent_tag_id")

    if not name or not domain:
        return {"error": "name and domain are required."}

    summary = body.get("summary", name)
    tag_id = graph.link_or_create_group(
        name=name,
        level=0,
        domain=domain,
        parent_id=parent_tag_id,
        summary=summary,
        # Manager-created tags are human-assigned forced tags → the Discovery
        # axis, so they participate in tag-scope Leiden community computation
        # (BP-96). Matches the live save path (save_context: mweft_save_tag).
        source="mweft_save_tag",
    )
    graph.set_user_tag("group", tag_id, "user_created")
    # link_or_create_group's ON CONFLICT(name) leaves a pre-existing tag's source
    # untouched — promote it so re-adding a Manager save-tag always lands on the
    # Discovery axis (BP-96), not just on first creation.
    if hasattr(graph, "set_group_source"):
        graph.set_group_source(tag_id, "mweft_save_tag")

    return {"id": tag_id, "name": name, "lifecycle": "user_created"}


@router.get("/entities/{entity_id}/graph")
def get_entity_graph(
    entity_id: str,
    max_hops: int = Query(2, ge=1, le=5),
    community_only: bool = Query(
        False,
        description=(
            "True returns only neighbors in the same community as the center in "
            "the latest leiden_entity run (simplifies hub-entity visualization)."
        ),
    ),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Ego graph centered on a single entity (BFS, max_hops).

    Returns center entity, BFS-reachable neighbors, **and** the induced
    subgraph of entity_connection edges restricted to {center} ∪ neighbors.
    Exceptions are returned as ``{"error": ...}`` (HTTP 200) so the UI can
    render a meaningful message instead of a generic 500 page.
    """
    try:
        return _get_entity_graph_impl(entity_id, max_hops, community_only, stores)
    except Exception as exc:  # noqa: BLE001
        logger.exception("entity ego graph failed (entity_id=%s, hops=%s)",
                         entity_id, max_hops)
        return {"error": f"ego graph failed: {type(exc).__name__}: {exc}"}


# SQLite default SQLITE_MAX_VARIABLE_NUMBER was 999 before 3.32; we batch
# the IN-clause to stay well below that even on old drivers.
_EGO_PARAM_BATCH = 400


def _get_entity_graph_impl(
    entity_id: str, max_hops: int, community_only: bool, stores: dict[str, Any],
) -> dict:
    graph = stores["graph"]
    entity = graph.get_entity_by_id(entity_id)
    if not entity:
        return {"error": f"Entity not found: {entity_id}"}
    neighbors = graph.get_connected_entities(entity_id, max_hops=max_hops)

    community_id: int | None = None
    community_note: str | None = None
    if community_only:
        run = resolve_latest_run(graph, "leiden_entity", None)
        if run is None:
            community_note = (
                "No community info — run the Leiden computation on the Analysis page first. "
                "Showing the full ego graph for now."
            )
        else:
            run_id = run.get("id") or run.get("run_id")
            cand_ids = [entity_id] + [n.get("id") for n in neighbors if n.get("id")]
            assign = graph.get_entity_community_for_run(run_id, cand_ids)
            community_id = assign.get(entity_id)
            if community_id is None:
                community_note = (
                    "This entity is not part of the latest leiden_entity run. "
                    "Try recomputing on the Analysis page. Showing the full ego graph for now."
                )
            else:
                neighbors = [n for n in neighbors if assign.get(n.get("id")) == community_id]

    slice_ids = {entity_id} | {n.get("id") for n in neighbors if n.get("id")}
    edges: list[dict[str, Any]] = []
    conn = getattr(graph, "_conn", None)
    if conn is not None and slice_ids:
        placeholder = "%s" if "postgres" in type(graph).__module__.lower() else "?"
        ids_list = list(slice_ids)
        # Batch a_id side to avoid IN(...) parameter limits on big ego graphs;
        # b_id still gets the full set so we don't miss edges.
        for i in range(0, len(ids_list), _EGO_PARAM_BATCH):
            chunk = ids_list[i:i + _EGO_PARAM_BATCH]
            marks_a = ",".join([placeholder] * len(chunk))
            marks_b = ",".join([placeholder] * len(ids_list))
            sql = (
                f"SELECT a_id, b_id, event_count FROM entity_connection "
                f"WHERE a_id IN ({marks_a}) AND b_id IN ({marks_b})"
            )
            params = tuple(chunk) + tuple(ids_list)
            for r in _sqlq_all(conn, sql, params):
                if isinstance(r, dict):
                    edges.append({"a_id": r.get("a_id"), "b_id": r.get("b_id"),
                                  "event_count": r.get("event_count")})
                elif hasattr(r, "keys"):
                    edges.append({"a_id": r["a_id"], "b_id": r["b_id"],
                                  "event_count": r["event_count"]})
                else:
                    edges.append({"a_id": r[0], "b_id": r[1], "event_count": r[2]})

    return sanitize({
        "entity": {
            "id": entity.get("id"),
            "name": entity.get("name"),
            "domain": entity.get("domain"),
        },
        "neighbors": neighbors,
        "edges": edges,
        "max_hops": max_hops,
        "community_only": community_only,
        "community_id": community_id,
        "community_note": community_note,
    })


@router.get("/entities/{entity_id}/event-communities")
def get_entity_event_communities(
    entity_id: str,
    sample_size: int = Query(3, ge=1, le=10),
    summary_chars: int = Query(40, ge=10, le=200),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Return the events an entity appears in, grouped by *Leiden event community*.

    For each community it returns `event_count` (how many times this entity appears
    in that community) + `sample_events` (the top-N summary prefixes by timestamp DESC).
    """
    try:
        return _entity_event_communities_impl(
            entity_id, sample_size, summary_chars, stores,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("entity event-communities failed (entity_id=%s)", entity_id)
        return {"error": f"event-communities failed: {type(exc).__name__}: {exc}"}


def _entity_event_communities_impl(
    entity_id: str, sample_size: int, summary_chars: int,
    stores: dict[str, Any],
) -> dict:
    graph = stores["graph"]
    entity = graph.get_entity_by_id(entity_id)
    if not entity:
        return {"error": f"Entity not found: {entity_id}"}
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    run = resolve_latest_run(graph, "leiden_event", None)
    if run is None:
        return {
            "entity": {"id": entity.get("id"), "name": entity.get("name")},
            "run_id": None,
            "communities": [],
            "note": (
                "No leiden_event run. Run the Leiden computation in the "
                "Analysis -> Events tab first."
            ),
        }
    run_id = run.get("id") or run.get("run_id")
    ph = "%s" if "postgres" in type(graph).__module__.lower() else "?"

    # 1) Community summary: count + a few sample summaries per community.
    # ROW_NUMBER() partitioned by community gives us top-N per group in one query.
    sql = f"""
        WITH ev AS (
            SELECT p.event_id FROM participated_in p WHERE p.entity_id = {ph}
        ),
        joined AS (
            SELECT eca.community_id, e.id, e.summary, e.timestamp,
                   ROW_NUMBER() OVER (
                       PARTITION BY eca.community_id
                       ORDER BY COALESCE(e.timestamp, e.created_at) DESC
                   ) AS rn
              FROM event_community_assignment eca
              JOIN ev ON ev.event_id = eca.event_id
              JOIN events e ON e.id = eca.event_id
             WHERE eca.run_id = {ph}
        )
        SELECT community_id, id, summary, timestamp, rn FROM joined
    """
    rows = _sqlq_all(conn, sql, (entity_id, run_id))
    if not rows:
        return {
            "entity": {"id": entity.get("id"), "name": entity.get("name")},
            "run_id": run_id,
            "communities": [],
            "note": "This entity has no events in the latest leiden_event run.",
        }

    def _r(r, key, idx):
        if isinstance(r, dict):
            return r.get(key)
        if hasattr(r, "keys"):
            return r[key]
        return r[idx]

    by_community: dict[int, dict[str, Any]] = {}
    for r in rows:
        cid = int(_r(r, "community_id", 0))
        eid = _r(r, "id", 1)
        summ = _r(r, "summary", 2) or ""
        ts = _r(r, "timestamp", 3)
        rn = int(_r(r, "rn", 4))
        bucket = by_community.setdefault(cid, {"community_id": cid, "event_count": 0,
                                               "sample_events": []})
        bucket["event_count"] += 1
        if rn <= sample_size:
            summ_short = str(summ)[:summary_chars]
            if len(str(summ)) > summary_chars:
                summ_short += "…"
            bucket["sample_events"].append({
                "id": eid, "summary": summ_short, "timestamp": ts,
            })

    communities = sorted(by_community.values(), key=lambda c: -c["event_count"])
    return sanitize({
        "entity": {"id": entity.get("id"), "name": entity.get("name")},
        "run_id": run_id,
        "communities": communities,
    })


@router.get("/entities/{entity_id}/event-communities/{community_id}/events")
def get_entity_events_in_community(
    entity_id: str,
    community_id: int,
    limit: int = Query(100, ge=1, le=500),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """List of events in entity ∩ community (timestamp DESC)."""
    try:
        return _entity_events_in_community_impl(
            entity_id, community_id, limit, stores,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "entity events-in-community failed (entity_id=%s, cid=%s)",
            entity_id, community_id,
        )
        return {"error": f"events-in-community failed: {type(exc).__name__}: {exc}"}


def _entity_events_in_community_impl(
    entity_id: str, community_id: int, limit: int, stores: dict[str, Any],
) -> dict:
    graph = stores["graph"]
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    run = resolve_latest_run(graph, "leiden_event", None)
    if run is None:
        return {"events": [], "note": "No leiden_event run."}
    run_id = run.get("id") or run.get("run_id")
    ph = "%s" if "postgres" in type(graph).__module__.lower() else "?"
    sql = f"""
        SELECT e.id, e.summary, e.timestamp,
               (SELECT COUNT(*) FROM participated_in pp WHERE pp.event_id = e.id) AS entity_count
          FROM events e
          JOIN event_community_assignment eca ON eca.event_id = e.id
          JOIN participated_in p ON p.event_id = e.id
         WHERE eca.run_id = {ph}
           AND eca.community_id = {ph}
           AND p.entity_id = {ph}
         ORDER BY COALESCE(e.timestamp, e.created_at) DESC
         LIMIT {ph}
    """
    rows = _sqlq_all(conn, sql, (run_id, int(community_id), entity_id, int(limit)))

    def _r(r, key, idx):
        if isinstance(r, dict):
            return r.get(key)
        if hasattr(r, "keys"):
            return r[key]
        return r[idx]

    events = [{
        "id": _r(r, "id", 0),
        "summary": _r(r, "summary", 1),
        "timestamp": _r(r, "timestamp", 2),
        "entity_count": _r(r, "entity_count", 3),
    } for r in rows]
    return sanitize({"community_id": community_id, "events": events})


@router.get("/graph")
def get_domain_graph(
    domain: str = Query(..., description="domain name"),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Full entity-connection dump for a domain (a_id, b_id, event_count)."""
    graph = stores["graph"]
    connections = graph.list_entity_connections(domain=domain)
    return sanitize({"domain": domain, "connections": connections})


@router.get("/domains/{domain}/summary")
def get_domain_summary(
    domain: str,
    top_n: int = Query(5, ge=1, le=50),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Domain summary — entity / event / connection counts + top hub entities.

    Used instead of a whole-graph dump. SQL aggregation avoids sending 5MB+ of JSON.
    Top hub = sum of incident connections' event_count.
    """
    graph = stores["graph"]
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    backend = "postgres" if "postgres" in type(graph).__module__.lower() else "sqlite"
    ph = "%s" if backend == "postgres" else "?"

    cur = cursor(conn)
    cur.execute(
        f"SELECT COUNT(*) FROM entities WHERE domain = {ph} "
        f"AND {_not_deprecated('deprecated', backend)}",
        (domain,),
    )
    entities_count = int(cur.fetchone()[0])

    cur.execute(
        f"SELECT COUNT(*) FROM events WHERE domain = {ph} "
        f"AND {_not_deprecated('deprecated', backend)}",
        (domain,),
    )
    events_count = int(cur.fetchone()[0])

    cur.execute(
        f"SELECT COUNT(*) FROM entity_connection ec "
        f"JOIN entities ea ON ea.id = ec.a_id "
        f"WHERE ea.domain = {ph}",
        (domain,),
    )
    edges_count = int(cur.fetchone()[0])

    # Top hub = incident-connection event_count sum. The naive form
    # ``JOIN entities e ON e.id = ec.a_id OR e.id = ec.b_id`` cannot use an index
    # (the OR-join), so on a large graph SQLite scans all connections once *per
    # in-domain entity* (e.g. 29k entities × 491k edges ≈ 14B ops) and the page
    # hangs. Split into a UNION ALL of two equality joins so each side is
    # index-driven (entities.domain → entity_connection.a_id/b_id). Same result.
    cur.execute(
        f"""
        SELECT eid AS id, name, type, SUM(w) AS hub_weight
          FROM (
            SELECT ec.a_id AS eid, ea.name AS name, ea.type AS type,
                   ec.event_count AS w
              FROM entity_connection ec
              JOIN entities ea ON ea.id = ec.a_id
             WHERE ea.domain = {ph} AND {_not_deprecated('ea.deprecated', backend)}
            UNION ALL
            SELECT ec.b_id AS eid, eb.name AS name, eb.type AS type,
                   ec.event_count AS w
              FROM entity_connection ec
              JOIN entities eb ON eb.id = ec.b_id
             WHERE eb.domain = {ph} AND {_not_deprecated('eb.deprecated', backend)}
          ) x
        GROUP BY eid, name, type
        ORDER BY hub_weight DESC
        LIMIT {ph}
        """,
        (domain, domain, int(top_n)),
    )
    def _to_dicts(c: Any) -> list[dict[str, Any]]:
        cols = [d[0] for d in c.description]
        out: list[dict[str, Any]] = []
        for row in c.fetchall():
            if isinstance(row, dict):
                out.append(row)
            elif hasattr(row, "keys"):
                out.append({k: row[k] for k in row.keys()})
            else:
                out.append(dict(zip(cols, row)))
        return out

    top_hubs = [
        {k: r.get(k) for k in ("id", "name", "type", "hub_weight")}
        for r in _to_dicts(cur)
    ]

    # deprecated filter (events alias e) — backend-correct (postgres boolean vs sqlite).
    dep_ok = _not_deprecated("e.deprecated", backend)

    # Most-entity events — top 5 by participated_in count
    cur.execute(
        f"""
        SELECT e.id AS id, e.summary AS summary,
               e.timestamp AS timestamp, e.created_at AS created_at,
               COUNT(pi.entity_id) AS entity_count
          FROM events e
          JOIN participated_in pi ON pi.event_id = e.id
         WHERE e.domain = {ph} AND {dep_ok}
        GROUP BY e.id, e.summary, e.timestamp, e.created_at
        ORDER BY entity_count DESC, e.created_at DESC
        LIMIT 5
        """,
        (domain,),
    )
    top_entity_events = _to_dicts(cur)

    # Recent events — top 5 by created_at (ingestion time)
    cur.execute(
        f"""
        SELECT e.id AS id, e.summary AS summary,
               e.timestamp AS timestamp, e.created_at AS created_at
          FROM events e
         WHERE e.domain = {ph} AND {dep_ok}
        ORDER BY e.created_at DESC
        LIMIT 5
        """,
        (domain,),
    )
    recent_events = _to_dicts(cur)

    # Attach tags (groups, kind=contains) — for the event_id set of both lists at once.
    ev_ids = [r["id"] for r in (top_entity_events + recent_events)]
    tag_map: dict[str, list[str]] = {}
    uniq_ids = list(dict.fromkeys(ev_ids))
    if uniq_ids:
        marks = ",".join([ph] * len(uniq_ids))
        cur.execute(
            f"""
            SELECT emo.event_id AS eid, g.name AS name
              FROM event_member_of emo
              JOIN groups g ON g.id = emo.group_id
             WHERE emo.kind = 'contains' AND emo.event_id IN ({marks})
            ORDER BY g.level ASC, g.name ASC
            """,
            tuple(uniq_ids),
        )
        for r in _to_dicts(cur):
            bucket = tag_map.setdefault(r["eid"], [])
            if len(bucket) < 5:
                bucket.append(r["name"])
    for r in top_entity_events:
        r["tags"] = tag_map.get(r["id"], [])
    for r in recent_events:
        r["tags"] = tag_map.get(r["id"], [])

    # Top tags — top 5 by event count
    cur.execute(
        f"""
        SELECT g.name AS name, COUNT(*) AS event_count
          FROM event_member_of emo
          JOIN groups g ON g.id = emo.group_id
          JOIN events e ON e.id = emo.event_id
         WHERE emo.kind = 'contains' AND e.domain = {ph}
        GROUP BY g.id, g.name
        ORDER BY event_count DESC
        LIMIT 5
        """,
        (domain,),
    )
    top_tags = _to_dicts(cur)

    # Leiden community count — latest run (kind=leiden_event / leiden_entity).
    # Tolerant of a missing community table: on a DB where communities were
    # never computed (or whose schema predates these tables) the table may not
    # exist — that must yield "no communities" (None), not a 500 that takes the
    # whole summary down. The failed statement aborts the Postgres transaction,
    # so roll back before the summary's remaining queries run.
    def _community_count(kind: str, table: str) -> int | None:
        try:
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT a.community_id) AS n
                  FROM {table} a
                 WHERE a.run_id = (
                     SELECT id FROM train_run
                      WHERE kind = {ph} AND domain = {ph}
                      ORDER BY COALESCE(finished_at, started_at) DESC
                      LIMIT 1)
                """,
                (kind, domain),
            )
        except Exception as exc:  # noqa: BLE001 — missing table / aborted txn
            logger.debug("community count skipped for %s: %s", table, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        rows = _to_dicts(cur)
        if not rows:
            return None
        v = rows[0].get("n")
        return int(v) if v is not None else 0

    event_communities = _community_count("leiden_event", "event_community_assignment")
    entity_communities = _community_count("leiden_entity", "entity_community_assignment")

    # Data span (created_at min/max)
    cur.execute(
        f"SELECT MIN(e.created_at) AS first_at, MAX(e.created_at) AS last_at "
        f"FROM events e WHERE e.domain = {ph} AND {dep_ok}",
        (domain,),
    )
    span_rows = _to_dicts(cur)
    first_at = span_rows[0].get("first_at") if span_rows else None
    last_at = span_rows[0].get("last_at") if span_rows else None

    result = sanitize({
        "domain": domain,
        "entities": entities_count,
        "events": events_count,
        "edges": edges_count,
        "top_hubs": top_hubs,
        "top_entity_events": top_entity_events,
        "recent_events": recent_events,
        "top_tags": top_tags,
        "event_communities": event_communities,
        "entity_communities": entity_communities,
        "first_at": first_at,
        "last_at": last_at,
    })
    try:  # close the single cursor reused throughout this function, just before returning
        cur.close()
    except Exception:  # noqa: BLE001
        pass
    return result


@router.get("/entities/{a_id}/connections/{b_id}/events")
def get_connection_events(
    a_id: str,
    b_id: str,
    limit: int = Query(50, ge=1, le=500),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """List of events where two entities co-occur (participated_in JOIN on both sides)."""
    graph = stores["graph"]
    content_store = stores["content"]
    events = graph.list_entity_connection_events(a_id=a_id, b_id=b_id, limit=limit)
    for ev in events:
        vid = ev.get("vector_id", "")
        if vid:
            cr = content_store.get_by_vector_id(vid)
            ev["summary"] = cr.inline_meta.get("event_summary", "") if cr else ""
        else:
            ev["summary"] = ""
    return sanitize({"a_id": a_id, "b_id": b_id, "events": events})


@router.post("/entities/{entity_id}/events/{event_id}/link")
def link_entity_event(
    entity_id: str,
    event_id: str,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Add an Entity <-> Event participation edge (participated_in)."""
    graph = stores["graph"]
    entity = graph.get_entity_by_id(entity_id)
    if not entity:
        return {"error": f"Entity not found: {entity_id}"}
    event = graph.get_event_by_id(event_id)
    if not event:
        return {"error": f"Event not found: {event_id}"}
    graph.link_participated_in(entity_id, event_id)
    return {"entity_id": entity_id, "event_id": event_id, "linked": True}
