"""Hint system / Connection Map builder.

Enriches the flat top-k results from ``mweft_search`` with *connections* —
each hit's ``connections`` (sequential window, etc.) plus a ``HintBlock``
for the full result set (connection map).

Design principle: changes the *content* of results, not the LLM's *calling
pattern*. The LLM calls search once as usual and reacts to the embedded
connection map in-context afterwards — no need to opt into separate graph
tools.

Match rationale: each hit receives a fixed-vocabulary tag
(``SearchHit.reason``) explaining why/how it surfaced. Raw numeric distances
or scores are not exposed because, without a threshold, they are noise for
the LLM — categorical tags are used for interpretation instead. The
``HintBlock`` is fully structured (threads / entities / context_groups /
categories + reason tags) with no prose sentences.

channel-presence-agnostic: if a channel fetch returns an empty result that
channel is automatically omitted from hints / ``available_channels`` —
zero code branching on data presence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from k2g.mcp.schemas import HintBlock
from k2g.trainer.community_freshness import resolve_latest_run

if TYPE_CHECKING:
    from k2g.mcp.factory import Deps
    from k2g.mcp.schemas import SearchHit

logger = logging.getLogger(__name__)

# Hint block size limits — keep the "lightweight map" compact.
_MAX_HINT_ENTITIES = 10
_MAX_THREADS = 8
# Max similar/connected neighbours surfaced per entity hit.
_MAX_ENTITY_NEIGHBORS = 3

# Per-hit reason tags (fixed vocabulary). Each tag explains why/how a hit
# surfaced.
TAG_CONTENTS_CHAIN = "contents-chain"        # sequential/thread — adjacent chunk in the same document
TAG_SIMILAR_EMBEDDING = "similar-embedding"  # entity_similar — semantically close vector (rename/synonym)
TAG_CLOSED_JACCARD = "closed-jaccard"        # jaccard — shared entity/group footprint
TAG_CO_OCCURRENCE = "co-occurrence"          # entity_connection — 1-hop co-occurrence neighbour
TAG_SHARED_CONTEXT = "shared-context"        # cg — same ContextGroup
TAG_SHARED_CATEGORY = "shared-category"      # category — same user group
TAG_SPANNING_ENTITY = "spanning-entity"      # entity-span — entity common to 2+ hits
# Leiden community cluster channel. Uses intra-hit-subset edge strength to
# distinguish genuine signals from noise.
TAG_CLUSTER_STRONG = "shared-cluster-strong"    # cluster — strong intra-subset edges (genuine signal)
TAG_CLUSTER_MEDIUM = "shared-cluster-medium"    # cluster — medium strength
TAG_CLUSTER_WEAK = "shared-cluster-weak"        # cluster — same community but weak intra-subset edges (peripheral)

# Cluster thresholds based on average weight of edges within the hit subset.
# entity: average entity_connection.event_count.
# event:  average event_jaccard_connected.entity_jaccard.
# Initial values are heuristic — adjust after measuring real DB density
# distributions.
_CLUSTER_THRESHOLDS = {
    "entity": {"strong": 5.0, "medium": 1.0},   # event_count
    "event": {"strong": 0.4, "medium": 0.2},    # entity_jaccard
}


def _tag(hit: "SearchHit", tag: str) -> None:
    """Append a tag to hit.reason (deduplicated)."""
    if tag not in hit.reason:
        hit.reason.append(tag)


# ---------------------------------------------------------------------------
# Leiden cluster channel helpers
# ---------------------------------------------------------------------------


def _fetch_leiden_assignment(
    deps: "Deps", hits: "list[SearchHit]",
) -> dict[str, int]:
    """Return a hit-id → community_id mapping for all hits (entity + event).

    Performs one run_id lookup per kind (leiden_entity / leiden_event) and
    one community-assignment call per kind. Does not modify K2G core —
    uses existing graph methods. Failures on either side are isolated
    (channel-presence-agnostic).
    """
    out: dict[str, int] = {}
    entity_ids = [h.id for h in hits if h.kind == "entity"]
    event_ids = [h.id for h in hits if h.kind == "event"]
    if entity_ids:
        run = resolve_latest_run(deps.graph, "leiden_entity")
        if run and run.get("id"):
            mapping = _safe(
                deps.graph, "get_entity_community_for_run",
                run["id"], entity_ids,
            ) or {}
            out.update(mapping)
    if event_ids:
        run = resolve_latest_run(deps.graph, "leiden_event")
        if run and run.get("id"):
            mapping = _safe(
                deps.graph, "get_event_community_for_run",
                run["id"], event_ids,
            ) or {}
            out.update(mapping)
    return out


def _exec(conn, sql: str, params: tuple = ()):
    """backend-agnostic execute — psycopg2 conn has no ``.execute`` (sqlite3 does)
    and its default RealDictCursor breaks ``r[0]`` index access. PG uses DictCursor
    (index+key) and ``?`` placeholders are rewritten to ``%s``. Returns the cursor.
    Canonical detector: ``k2g.web.routes._sql._is_pg``."""
    if "psycopg2" in type(conn).__module__.lower():
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql.replace("?", "%s"), params)
    else:
        cur = conn.cursor()
        cur.execute(sql, params)
    return cur


def _compute_subgraph_density(
    deps: "Deps", member_ids: list[str], kind: str,
) -> dict[str, Any]:
    """Count edges, average weight, and top 3 edges within the hit subset.

    entity: ``entity_connection.event_count``.
    event:  ``event_jaccard_connected.entity_jaccard``.
    Uses the graph store's ``_conn`` directly (diagnostic SQL at the hint
    layer). Failures are isolated.
    """
    empty = {"inter_edges": 0, "avg_weight": 0.0, "top_edges": []}
    if len(member_ids) < 2:
        return empty
    conn = getattr(deps.graph, "_conn", None)
    if conn is None:
        return empty
    # ``?`` placeholders — _exec rewrites to ``%s`` on Postgres.
    marks = ",".join(["?"] * len(member_ids))
    if kind == "entity":
        sql = (
            f"SELECT a_id, b_id, event_count "
            f"FROM entity_connection "
            f"WHERE a_id IN ({marks}) AND b_id IN ({marks}) "
            f"ORDER BY event_count DESC LIMIT 100"
        )
    else:  # event
        # Filter out entity_jaccard=0 rows (group-only edges stored without
        # a filter) so they do not deflate the average weight.
        sql = (
            f"SELECT a_id, b_id, entity_jaccard "
            f"FROM event_jaccard_connected "
            f"WHERE a_id IN ({marks}) AND b_id IN ({marks}) "
            f"AND entity_jaccard > 0 "
            f"ORDER BY entity_jaccard DESC LIMIT 100"
        )
    params = (*member_ids, *member_ids)
    try:
        rows = _exec(conn, sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Subgraph density fetch failed (%s): %s", kind, exc)
        return empty
    if not rows:
        return empty
    edges = [(r[0], r[1], float(r[2] or 0)) for r in rows]
    inter = len(edges)
    avg = sum(w for _, _, w in edges) / inter
    # entity event_count is an integer — display as int.
    # event jaccard is a float — round to 3 dp.
    top = [
        {
            "a": a, "b": b,
            "weight": int(w) if kind == "entity" else round(w, 3),
        }
        for a, b, w in edges[:3]
    ]
    return {"inter_edges": inter, "avg_weight": avg, "top_edges": top}


def _classify_density(avg_weight: float, kind: str) -> str:
    """Map average weight to strong / medium / weak using configured thresholds.

    Note: ``cluster_id`` values are meaningful only within a single call
    (they may change across recomputations). Threshold values are initial
    hardcoded estimates — adjust based on real DB density measurements.
    """
    t = _CLUSTER_THRESHOLDS.get(kind)
    if t is None:
        return "weak"
    if avg_weight >= t["strong"]:
        return "strong"
    if avg_weight >= t["medium"]:
        return "medium"
    return "weak"


def _density_tag(density: str) -> str:
    """Map density category to the corresponding reason tag constant."""
    if density == "strong":
        return TAG_CLUSTER_STRONG
    if density == "medium":
        return TAG_CLUSTER_MEDIUM
    return TAG_CLUSTER_WEAK


def build_connection_map(
    deps: "Deps", hits: "list[SearchHit]",
) -> HintBlock | None:
    """Attach ``connections`` / ``reason`` to each hit in-place and return a
    ``HintBlock``.

    Each hit receives fixed-vocabulary reason tags; event hits receive a
    ``connections.sequential`` window; entity hits receive
    ``connections.similar/connected/profile``. The ``HintBlock`` is a
    structured connection map for the full result set (no prose).

    Returns None if no channel produced any data. The caller (search_tool)
    wraps this function in a try/except — hint generation failure must not
    break the search response.
    """
    if not hits:
        return None

    event_hits = [h for h in hits if h.kind == "event"]
    hit_by_id: dict[str, "SearchHit"] = {h.id: h for h in hits}
    hit_id_set = set(hit_by_id)

    # --- sequential (forced) — prev/next chunk of each event hit ----------
    seq: dict[str, dict[str, Any]] = {}
    for h in event_hits:
        adj = _safe(deps.graph, "get_adjacent_events", h.id)
        if adj and (adj.get("prev_id") or adj.get("next_id")):
            window = {"prev": adj.get("prev_id"), "next": adj.get("next_id")}
            h.connections["sequential"] = window
            seq[h.id] = window
            _tag(h, TAG_CONTENTS_CHAIN)

    # --- threads — sequential chains within the hit set -------------------
    threads = _build_threads(event_hits, seq, hit_id_set)

    # --- entity-span + category — one get_event_context call per hit ------
    ent_acc: dict[str, dict[str, Any]] = {}
    cat_acc: dict[str, dict[str, Any]] = {}
    for h in event_hits:
        ctx = _safe(deps.graph, "get_event_context", h.id) or {}
        for e in ctx.get("entities", []) or []:
            eid = e.get("id")
            if not eid:
                continue
            rec = ent_acc.setdefault(
                eid,
                {"id": eid, "name": e.get("name"), "hit_count": 0,
                 "members": set()},
            )
            rec["hit_count"] += 1
            rec["members"].add(h.id)
        for g in ctx.get("groups", []) or []:
            gid = g.get("id")
            if not gid:
                continue
            rec = cat_acc.setdefault(
                gid,
                {"id": gid, "name": g.get("name"), "hit_count": 0,
                 "members": set()},
            )
            rec["hit_count"] += 1
            rec["members"].add(h.id)
    span_entities = _span(
        ent_acc, hit_by_id, TAG_SPANNING_ENTITY, limit=_MAX_HINT_ENTITIES,
    )
    span_categories = _span(cat_acc, hit_by_id, TAG_SHARED_CATEGORY)

    # --- cg — ContextGroups shared by 2+ event hits -----------------------
    cg_acc: dict[str, dict[str, Any]] = {}
    for h in event_hits:
        for cg in _safe(deps.graph, "get_contexts_of_event", h.id) or []:
            cid = cg.get("id")
            if not cid:
                continue
            rec = cg_acc.setdefault(
                cid,
                {"id": cid, "name": cg.get("name"), "hit_count": 0,
                 "members": set()},
            )
            rec["hit_count"] += 1
            rec["members"].add(h.id)
    span_cgs = _span(cg_acc, hit_by_id, TAG_SHARED_CONTEXT)

    # --- jaccard — jaccard-similar pairs within the hit set ---------------
    jac_pairs: set[tuple[str, str]] = set()
    for h in event_hits:
        for nb in _safe(deps.graph, "get_jaccard_neighbors", h.id) or []:
            nbid = nb.get("event_id")
            if nbid and nbid != h.id and nbid in hit_id_set:
                jac_pairs.add(
                    (h.id, nbid) if h.id < nbid else (nbid, h.id),
                )
    for a, b in jac_pairs:
        _tag(hit_by_id[a], TAG_CLOSED_JACCARD)
        _tag(hit_by_id[b], TAG_CLOSED_JACCARD)

    # --- clusters — hits sharing a Leiden community -----------------------
    # Measures how strongly the subset is connected so the LLM can tell
    # genuine signals from coincidental co-membership.
    # cluster_id values are meaningful only within this call (volatile across
    # recomputations).
    clusters: list[dict[str, Any]] = []
    assignment = _fetch_leiden_assignment(deps, hits)
    if assignment:
        by_cid: dict[tuple[int, str], list[str]] = {}
        for h in hits:
            cid = assignment.get(h.id)
            if cid is None:
                continue
            by_cid.setdefault((int(cid), h.kind), []).append(h.id)
        for (cid, kind), members in by_cid.items():
            if len(members) < 2:
                continue
            density_info = _compute_subgraph_density(deps, members, kind)
            density = _classify_density(density_info["avg_weight"], kind)
            tag = _density_tag(density)
            for hid in members:
                h_hit = hit_by_id.get(hid)
                if h_hit is not None:
                    _tag(h_hit, tag)
            clusters.append({
                "cluster_id": cid,
                "kind": kind,
                "members": list(members),
                "inter_edges": density_info["inter_edges"],
                "density": density,
                "top_edges": density_info["top_edges"],
            })

    # --- entity expansion — similar / connected / profile per entity hit --
    vector = getattr(deps, "vector", None)
    entity_hits = [h for h in hits if h.kind == "entity"][:_MAX_HINT_ENTITIES]
    for h in entity_hits:
        ev = _safe(vector, "get_entity_vector", h.id)
        # entity_similar — vector-close entities (self excluded).
        # Score not exposed — categorical tag used for LLM interpretation.
        if ev and ev.get("vector"):
            sims = _safe(
                vector, "search_similar_entities",
                ev["vector"], h.domain, _MAX_ENTITY_NEIGHBORS + 1,
            ) or []
            sims = [s for s in sims if s.get("id") != h.id]
            if sims:
                h.connections["similar"] = [
                    {"id": s.get("id"), "name": s.get("name"),
                     "type": s.get("type")}
                    for s in sims[:_MAX_ENTITY_NEIGHBORS]
                ]
                _tag(h, TAG_SIMILAR_EMBEDDING)
        # entity_connection — 1-hop co-occurrence neighbours
        conn = _safe(deps.graph, "get_connected_entities", h.id, 1) or []
        if conn:
            h.connections["connected"] = [
                {"id": c.get("id"), "name": c.get("name"),
                 "type": c.get("type")}
                for c in conn[:_MAX_ENTITY_NEIGHBORS]
            ]
            _tag(h, TAG_CO_OCCURRENCE)
        # Compact profile — only when entity vector exists (established entity).
        # No extra queries — reuses get_entity_vector / get_connected_entities.
        if ev:
            meta = ev.get("metadata") or {}
            profile = {
                "type": h.metadata.get("type"),
                "coherence": meta.get("coherence"),
                "event_count": meta.get("ref_event_count"),
                "connection_count": len(conn),
            }
            h.connections["profile"] = {
                k: v for k, v in profile.items() if v is not None
            }

    # --- available_channels = union of all hit reason tags ----------------
    available = sorted({tag for h in hits for tag in h.reason})
    if not available:
        return None

    return HintBlock(
        threads=threads,
        entities=span_entities,
        context_groups=span_cgs,
        categories=span_categories,
        clusters=clusters,
        available_channels=available,
    )


def _span(
    acc: dict[str, dict[str, Any]],
    hit_by_id: dict[str, "SearchHit"],
    tag: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return records spanning 2+ hits, sorted by hit_count, with tag applied.

    The returned dicts contain only ``{id, name, hit_count}`` (members set
    is stripped for the HintBlock).
    """
    rows = sorted(
        (r for r in acc.values() if r["hit_count"] >= 2),
        key=lambda r: -r["hit_count"],
    )
    if limit is not None:
        rows = rows[:limit]
    for r in rows:
        for hid in r["members"]:
            h = hit_by_id.get(hid)
            if h is not None:
                _tag(h, tag)
    return [
        {"id": r["id"], "name": r["name"], "hit_count": r["hit_count"]}
        for r in rows
    ]


def _safe(graph: Any, method: str, *args: Any) -> Any:
    """Call a graph method with full isolation.

    Returns None if the method is absent (unimplemented backend) or if
    execution raises an exception. This provides the channel-presence-agnostic
    guarantee at the method level — a dead channel or missing backend method
    does not affect other channels or the search body.
    """
    fn = getattr(graph, method, None)
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hint channel fetch failed (%s): %s", method, exc)
        return None


def _build_threads(
    event_hits: "list[SearchHit]",
    seq: dict[str, dict[str, Any]],
    hit_id_set: set[str],
) -> list[dict[str, Any]]:
    """Group sequentially adjacent hits within the hit set into chains.

    If ``seq[hit]["next"]`` is the id of another hit in the set, they are
    linked. Chains are traversed from their start (no preceding hit in the
    set) following ``next`` pointers; only chains of length >= 2 are returned.
    """
    nexts = {
        hid: w["next"]
        for hid, w in seq.items()
        if w.get("next") and w["next"] in hit_id_set
    }
    pointed_to = set(nexts.values())   # Interior chain nodes — not chain starts
    threads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for h in event_hits:
        if h.id in pointed_to or h.id in seen:
            continue
        chain = [h.id]
        seen.add(h.id)
        cur = h.id
        while cur in nexts and nexts[cur] not in seen:
            cur = nexts[cur]
            chain.append(cur)
            seen.add(cur)
        if len(chain) >= 2:
            threads.append({"events": chain, "via": "sequential"})
        if len(threads) >= _MAX_THREADS:
            break
    return threads


__all__ = ["build_connection_map"]
