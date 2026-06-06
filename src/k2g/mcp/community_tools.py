"""Read-only MCP community tools.

Expose graph clustering results so an LLM can interpret and label them at
call time. Reads the latest completed ``train_run`` (kind=leiden_entity /
leiden_event) and its assignment table. No write, no label persistence —
the LLM does the interpretation in its response.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from typing import Any

from k2g.memory.source_axis import resolve_axis
from k2g.trainer.community_freshness import read_leiden_params, resolve_latest_run

_KIND = {"entity": "leiden_entity", "event": "leiden_event"}

_DEFAULT_LEIDEN = {"resolution": 1.0, "seed": 42}

# ── explore_hints engine parameters ──────────────────────────────────────────
_HINT_TOP_K = 3                 # Fixed Top-K returned in response
_MIN_COMMUNITY_SIZE = 5         # Communities smaller than this are excluded from signals
_RESIDUAL_DOMINANT_MAX = 0.70   # Discovery dominant-tag share below this → label_residual
_QC_DOMINANT_MAX = 0.60         # QC(autotag) dominant-tag share below this → suggest review
_GIANT_SKEW = 3.0               # size >= median * this ratio → giant_community
_LOW_MODULARITY = 0.30          # modularity below this → low_modularity
_AXIS_MIN_DISTINCT = 2          # Axis discriminability pre-check: skip if distinct tags < 2


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else float(x)


def _event_tag_crosstab(graph: Any, run_id: str) -> list[tuple]:
    """Single-pass cross-tab: (community, group, source, n).

    Joins ``event_community_assignment``, ``event_member_of``, and ``groups``
    in one query. All aggregation criteria operate on top of this result, so
    additional criteria do not add extra JOINs.
    """
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    ph = "%s" if backend == "postgres" else "?"
    sql = (
        "SELECT eca.community_id, em.group_id, g.name, g.source, COUNT(*) AS n "
        "FROM event_community_assignment eca "
        "JOIN event_member_of em ON em.event_id = eca.event_id "
        "JOIN groups g ON g.id = em.group_id "
        f"WHERE eca.run_id = {ph} "
        "GROUP BY eca.community_id, em.group_id"
    )
    cur = graph._conn.cursor()
    try:
        cur.execute(sql, (run_id,))
        return [
            (int(r[0]), r[1], r[2], r[3], int(r[4])) for r in cur.fetchall()
        ]
    finally:
        cur.close()


def _compute_explore_hints(
    graph: Any,
    *,
    kind: str,
    run_id: str,
    sizes_by_comm: dict[int, int],
    largest: int,
    median_size: float,
    modularity: float | None,
    resolution: float,
) -> tuple[float, list[dict[str, Any]], dict[str, Any]]:
    """Compute cheap discovery signals to attach to a community summary.

    Returns ``(explore_worth, explore_hints[Top-K], axes_summary)``. No
    re-clustering, no extra MCP calls. Failures are best-effort (empty
    signals) — the summary is always returned intact.
    """
    candidates: list[dict[str, Any]] = []
    axes_summary: dict[str, Any] = {}

    def _view(cid: int) -> dict[str, Any]:
        return {"node": kind, "scope": f"community:{cid}",
                "resolution": round(resolution * 1.5, 2)}

    # ── Structural signals (axis-independent, cheap) ──────────────────────
    if modularity is not None and modularity < _LOW_MODULARITY:
        candidates.append({
            "reason": "low_modularity", "community_id": None,
            "score": _clamp01((_LOW_MODULARITY - modularity) / _LOW_MODULARITY),
            "axis": None, "role": None,
            "evidence": {"modularity": round(modularity, 4)},
            "suggested_view": None,
            "hint": (
                f"Overall modularity {modularity:.2f} is low — "
                "community structure is weak; treat flat summary with caution"
            ),
        })
    if median_size > 0:
        for cid, size in sizes_by_comm.items():
            if size < _MIN_COMMUNITY_SIZE:
                continue
            ratio = size / median_size
            if ratio >= _GIANT_SKEW:
                candidates.append({
                    "reason": "giant_community", "community_id": cid,
                    "score": _clamp01(1 - _GIANT_SKEW / ratio),
                    "axis": None, "role": None,
                    "evidence": {"size": size, "median": median_size,
                                 "ratio": round(ratio, 1)},
                    "suggested_view": _view(cid),
                    "hint": (
                        f"Community {cid} size {size} = {ratio:.1f}x the median "
                        "— may split at higher resolution"
                    ),
                })

    # ── Tag cross-signals (event kind only — tags attach to events) ───────
    if kind == "event":
        try:
            rows = _event_tag_crosstab(graph, run_id)
        except Exception:  # noqa: BLE001 — best-effort
            rows = []
        # per (community, axis): {group_id: (name, n)} + global axis distinct
        per: dict[int, dict[str, dict[str, tuple]]] = defaultdict(
            lambda: defaultdict(dict))
        axis_groups: dict[str, set] = defaultdict(set)
        axis_role: dict[str, str] = {}
        axis_auth: dict[str, float] = {}
        for cid, gid, gname, source, n in rows:
            prof = resolve_axis(source)
            ax = prof.axis
            if prof.role in ("container", "unknown"):
                continue  # Non-topical or unknown origin — skip axis
            per[cid][ax][gid] = (gname, n)
            axis_groups[ax].add(gid)
            axis_role[ax] = prof.role
            axis_auth[ax] = prof.authority

        # Skip axes with fewer than the minimum distinct tags
        active_axes = {
            ax for ax, gids in axis_groups.items()
            if len(gids) >= _AXIS_MIN_DISTINCT
        }
        axes_summary = {
            ax: {"distinct_tags": len(axis_groups[ax]), "role": axis_role[ax],
                 "active": ax in active_axes}
            for ax in axis_groups
        }

        for cid, size in sizes_by_comm.items():
            if size < _MIN_COMMUNITY_SIZE:
                continue
            for ax in active_axes:
                role = axis_role[ax]
                auth = axis_auth[ax]
                dist = per.get(cid, {}).get(ax, {})
                if dist:
                    dom_name, dom_n = max(dist.values(), key=lambda t: t[1])
                    dom_share = min(1.0, dom_n / size)
                else:
                    dom_name, dom_share = None, 0.0

                if role == "discovery":
                    if not dist:
                        # Emergent — community with zero discovery labels
                        # (candidate for a new tag)
                        candidates.append({
                            "reason": "emergent_concept", "community_id": cid,
                            "score": _clamp01(size / max(largest, 1)) * auth,
                            "axis": ax, "role": role,
                            "evidence": {"size": size, "discovery_coverage": 0},
                            "suggested_view": _view(cid),
                            "hint": (
                                f"Community {cid}: no '{ax}' label assigned "
                                "— latent concept with no tag (new tag candidate)"
                            ),
                        })
                    elif dom_share < _RESIDUAL_DOMINANT_MAX:
                        candidates.append({
                            "reason": "label_residual", "community_id": cid,
                            "score": _clamp01(
                                (_RESIDUAL_DOMINANT_MAX - dom_share)
                                / _RESIDUAL_DOMINANT_MAX) * auth,
                            "axis": ax, "role": role,
                            "evidence": {"size": size,
                                         "dominant_tag": dom_name,
                                         "dominant_tag_share": round(dom_share, 2)},
                            "suggested_view": _view(cid),
                            "hint": (
                                f"Community {cid}: dominant '{ax}' tag '{dom_name}' "
                                f"explains only {dom_share:.0%} "
                                "— possible hidden sub-structure"
                            ),
                        })
                elif role == "qc" and dist and dom_share < _QC_DOMINANT_MAX:
                    # QC axis (autotag/llm_build): AI classification does not
                    # align with structure — suggest review (lower authority)
                    candidates.append({
                        "reason": "qc_incoherent", "community_id": cid,
                        "score": _clamp01(
                            (_QC_DOMINANT_MAX - dom_share)
                            / _QC_DOMINANT_MAX) * auth,
                        "axis": ax, "role": role,
                        "evidence": {"size": size, "dominant_tag": dom_name,
                                     "dominant_tag_share": round(dom_share, 2)},
                        "suggested_view": _view(cid),
                        "hint": (
                            f"Community {cid}: AI tag ('{ax}') is dispersed "
                            f"({dom_share:.0%}) "
                            "— suggest reviewing classification (low authority)"
                        ),
                    })

    # ── Dedup (community, reason) + Top-K ────────────────────────────────
    candidates.sort(key=lambda h: -h["score"])
    seen: set = set()
    hints: list[dict[str, Any]] = []
    for h in candidates:
        key = (h["community_id"], h["reason"])
        if key in seen:
            continue
        seen.add(key)
        h["score"] = round(h["score"], 3)
        hints.append(h)
        if len(hints) >= _HINT_TOP_K:
            break

    explore_worth = round(max((h["score"] for h in hints), default=0.0), 3)
    return explore_worth, hints, axes_summary


def _resolve_run(deps: Any, kind: str, domain: str | None, run_id: str | None):
    if run_id:
        return run_id
    run = resolve_latest_run(deps.db.graph, _KIND[kind], domain)
    return run["id"] if run else None


def _json_field(run: dict | None, key: str) -> dict:
    """Parse a JSON column (metrics_json/params_json) from a train_run row.

    Postgres JSONB already returns a dict; SQLite TEXT returns a string —
    both cases are handled.
    """
    if not run:
        return {}
    val = run.get(key)
    if isinstance(val, dict):
        return val
    if val:
        try:
            return json.loads(val)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _metrics(run: dict | None) -> dict:
    return _json_field(run, "metrics_json")


def _read_configured_params(deps: Any, domain: str | None) -> dict:
    """Read the clustering configuration (resolution/seed) stored by the manager.

    Delegates to ``community_freshness.read_leiden_params`` as the single
    source of truth (domain → global → default cascade). The values shown
    in the summary are identical to those used during recomputation.
    Read-only.
    """
    conn = getattr(deps.db.graph, "_conn", None)
    if conn is None:
        return {**_DEFAULT_LEIDEN, "saved_at": None,
                "is_default": True, "scope": "default"}
    return read_leiden_params(conn, domain)


def community_list_tool(
    deps: Any,
    *,
    kind: str = "entity",
    domain: str | None = None,
    top_n: int = 20,
    members_preview: int = 8,
) -> dict[str, Any]:
    """List communities of the latest run (size + top member names)."""
    if kind not in _KIND:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    run = resolve_latest_run(deps.db.graph, _KIND[kind], domain)
    if run is None:
        return {
            "kind": kind, "domain": domain, "communities": [],
            "note": f"no completed {_KIND[kind]} run — run scripts/train_community.py",
        }
    run_id = run["id"]
    members = deps.db.graph.list_community_members(run_id, kind)
    groups: dict[int, list[dict]] = defaultdict(list)
    for m in members:
        groups[m["community_id"]].append(m)

    communities = []
    for cid, mem in groups.items():
        mem.sort(key=lambda x: -x["weight"])
        communities.append({
            "community_id": cid,
            "size": len(mem),
            "top_members": [x["name"] for x in mem[:members_preview]],
        })
    communities.sort(key=lambda c: -c["size"])

    return {
        "kind": kind,
        "domain": domain,
        "run_id": run_id,
        "num_communities": len(groups),
        "total_members": len(members),
        "metrics": _metrics(run),
        "communities": communities[: int(top_n)],
    }


def community_detail_tool(
    deps: Any,
    *,
    community_id: int,
    kind: str = "entity",
    run_id: str | None = None,
    max_members: int = 50,
) -> dict[str, Any]:
    """Full member list of one community (ranked by prominence)."""
    if kind not in _KIND:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    rid = _resolve_run(deps, kind, None, run_id)
    if rid is None:
        return {"error": f"no completed {_KIND[kind]} run"}
    members = deps.db.graph.list_community_members(rid, kind)
    mem = [m for m in members if m["community_id"] == int(community_id)]
    if not mem:
        return {"error": f"community {community_id} not found in run {rid}"}
    mem.sort(key=lambda x: -x["weight"])
    return {
        "kind": kind,
        "run_id": rid,
        "community_id": int(community_id),
        "size": len(mem),
        "members": [
            {"id": m["node_id"], "name": m["name"], "weight": m["weight"]}
            for m in mem[: int(max_members)]
        ],
        "truncated": len(mem) > int(max_members),
    }


def community_of_tool(
    deps: Any,
    *,
    node_id: str,
    kind: str = "entity",
    peers_preview: int = 10,
) -> dict[str, Any]:
    """Which community a given entity/event belongs to + sample peers."""
    if kind not in _KIND:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    run = resolve_latest_run(deps.db.graph, _KIND[kind], None)
    if run is None:
        return {"error": f"no completed {_KIND[kind]} run"}
    run_id = run["id"]
    members = deps.db.graph.list_community_members(run_id, kind)
    target = next((m for m in members if m["node_id"] == node_id), None)
    if target is None:
        return {
            "node_id": node_id, "kind": kind, "run_id": run_id,
            "community_id": None,
            "note": "node not in any community for this run",
        }
    cid = target["community_id"]
    peers = sorted(
        (m for m in members if m["community_id"] == cid and m["node_id"] != node_id),
        key=lambda x: -x["weight"],
    )
    return {
        "node_id": node_id,
        "kind": kind,
        "run_id": run_id,
        "community_id": cid,
        "community_size": len(peers) + 1,
        "peers": [p["name"] for p in peers[: int(peers_preview)]],
    }


def _exec(conn: Any, sql: str, params: tuple | list = ()):
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


def _node_names(conn: Any, kind: str, ids: list[str]) -> dict[str, str]:
    """Map node_id to display name (entity=name, event=summary prefix)."""
    out: dict[str, str] = {}
    if not ids:
        return out
    col = "name" if kind == "entity" else "summary"
    tbl = "entities" if kind == "entity" else "events"
    CH = 400
    for i in range(0, len(ids), CH):
        chunk = ids[i:i + CH]
        q = f"SELECT id, {col} FROM {tbl} WHERE id IN ({','.join('?' * len(chunk))})"
        for r in _exec(conn, q, chunk):
            v = r[1] or ""
            out[r[0]] = v[:80] if kind == "event" else v
    return out


# In-process cache for parameterized explore views
# key: (kind, scope, resolution, theta_e, data_version_count, data_version_ts)
_EXPLORE_CACHE: dict[tuple, dict[str, Any]] = {}
_EXPLORE_CACHE_MAX = 64
# Separate member cache: same key → {community_id: [node_id]}
# The list response only includes top_members (name preview); full node ids
# are stored here for drill-down without re-running detection.
_EXPLORE_MEMBERS_CACHE: dict[tuple, dict[int, list[str]]] = {}


def _resolve_scope_ids(
    deps: Any, kind: str, scope: str, domain: str | None,
) -> tuple[set[str] | None, str]:
    """Resolve a scope string to a (node-id set, normalized scope key).

    Supported forms: ``tag:<group_id>`` | ``community:<cid>`` |
    bare group_id | tag name.
    """
    from k2g.graph import leiden_community as lc

    graph = deps.db.graph
    conn = graph._conn

    if scope.startswith("community:"):
        cid = int(scope.split(":", 1)[1])
        run = resolve_latest_run(graph, _KIND[kind], domain)
        if run is None:
            return set(), f"community:{cid}(no-run)"
        members = graph.list_community_members(run["id"], kind)
        ids = {m["node_id"] for m in members if m["community_id"] == cid}
        return ids, f"community:{cid}"

    gid = scope[4:] if scope.startswith("tag:") else scope
    # If the bare value is not a group_id, try resolving it as a tag name
    row = _exec(
        conn, "SELECT id FROM groups WHERE id = ? OR name = ? LIMIT 1", (gid, gid),
    ).fetchone()
    if row is None:
        return set(), f"tag:{gid}(not-found)"
    gid = row[0]
    ids = (
        lc._scope_entity_ids(conn, gid) if kind == "entity"
        else lc._scope_event_ids(conn, gid)
    )
    return ids, f"tag:{gid}"


def community_explore_tool(
    deps: Any,
    *,
    scope: str,
    kind: str = "event",
    resolution: float = 1.5,
    theta_e: float = 0.4,
    domain: str | None = None,
    top_n: int = 10,
    members_preview: int = 5,
) -> dict[str, Any]:
    """Parameterized explore view: run on-demand clustering within a scope.

    Independent of the canonical global run, this accepts a subset boundary
    (scope) and resolution, runs clustering in-process, and returns the
    result without persisting it (cached in-process only). This is the
    backend that executes the ``suggested_view`` from explore_hints.

    scope: ``tag:<gid>`` (tag boundary) | ``community:<cid>`` (re-split an
           existing community at higher resolution) | bare group_id | tag name.
    """
    from k2g.graph import leiden_community as lc
    from k2g.trainer import community_freshness as cf

    if kind not in _KIND:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    graph = deps.db.graph
    conn = graph._conn

    scope_ids, scope_key = _resolve_scope_ids(deps, kind, scope, domain)
    if not scope_ids:
        return {"kind": kind, "scope": scope_key, "resolution": resolution,
                "num_communities": 0, "communities": [],
                "note": f"No nodes found for scope '{scope}'"}

    dv = cf.derive_version(conn, domain)
    ckey = (kind, scope_key, round(float(resolution), 3),
            round(float(theta_e), 3), dv[0], dv[1])
    if ckey in _EXPLORE_CACHE:
        cached = dict(_EXPLORE_CACHE[ckey])
        cached["cached"] = True
        return cached

    db = deps.db
    result = lc.detect(db, kind, domain=domain, resolution=float(resolution),
                       scope_ids=set(scope_ids), theta_e=float(theta_e))
    # Group members by community
    by_comm: dict[int, list[str]] = defaultdict(list)
    for node_id, cid in result.assignments:
        by_comm[cid].append(node_id)
    names = _node_names(conn, kind, [nid for nid, _ in result.assignments])

    total = result.node_count
    sizes = sorted((len(v) for v in by_comm.values()), reverse=True)
    median = statistics.median(sizes) if sizes else 0
    communities = []
    for cid, mem in sorted(by_comm.items(), key=lambda kv: -len(kv[1])):
        communities.append({
            "community_id": cid,
            "size": len(mem),
            "share": round(len(mem) / total, 4) if total else 0.0,
            "top_members": [names.get(n, n) for n in mem[:members_preview]],
        })

    # Structural signals only (axis-independent) for scoped views
    hints = []
    if median and sizes and sizes[0] / median >= _GIANT_SKEW:
        hints.append({
            "reason": "giant_community",
            "community_id": communities[0]["community_id"],
            "score": round(_clamp01(1 - _GIANT_SKEW / (sizes[0] / median)), 3),
            "suggested_view": {
                "node": kind,
                "scope": f"community:{communities[0]['community_id']}",
                "resolution": round(resolution * 1.5, 2),
            },
            "hint": (
                f"Largest community within scope is still "
                f"{sizes[0] / median:.1f}x the median — can be split further"
            ),
        })
    if result.modularity < _LOW_MODULARITY:
        hints.append({
            "reason": "low_modularity", "community_id": None,
            "score": round(
                _clamp01((_LOW_MODULARITY - result.modularity) / _LOW_MODULARITY),
                3,
            ),
            "hint": (
                f"Scope modularity {result.modularity:.2f} is low "
                "— weak structure within this boundary"
            ),
        })

    out = {
        "kind": kind,
        "scope": scope_key,
        "resolution": float(resolution),
        "theta_e": float(theta_e),
        "data_version": {"count": dv[0], "max_ts": dv[1]},
        "num_communities": result.num_communities,
        "total_members": total,
        "edge_count": result.edge_count,
        "metrics": {"modularity": round(result.modularity, 4)},
        "size_stats": {"largest": sizes[0] if sizes else 0,
                       "median": median,
                       "singletons": sum(1 for s in sizes if s == 1)},
        "communities": communities[: int(top_n)],
        "explore_hints": hints,
        "cached": False,
    }
    if len(_EXPLORE_CACHE) >= _EXPLORE_CACHE_MAX:
        _EXPLORE_CACHE.clear()
        _EXPLORE_MEMBERS_CACHE.clear()
    _EXPLORE_CACHE[ckey] = out
    _EXPLORE_MEMBERS_CACHE[ckey] = {c: list(m) for c, m in by_comm.items()}
    return out


def community_explore_members_tool(
    deps: Any,
    *,
    scope: str,
    community_id: int,
    kind: str = "event",
    resolution: float = 1.5,
    theta_e: float = 0.4,
    domain: str | None = None,
    max_members: int = 50,
) -> dict[str, Any]:
    """Drill down into the full member list (id + name) of one scoped community.

    Reuses the in-process member cache under the same
    (scope, kind, resolution, data_version) key as ``community_explore_tool``.
    When the cache is absent (separate call or eviction), the clustering
    algorithm is deterministic (seed=42), so the same partition and community
    IDs are reproduced consistently.
    """
    from k2g.graph import leiden_community as lc
    from k2g.trainer import community_freshness as cf

    if kind not in _KIND:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    graph = deps.db.graph
    conn = graph._conn

    scope_ids, scope_key = _resolve_scope_ids(deps, kind, scope, domain)
    if not scope_ids:
        return {"kind": kind, "scope": scope_key, "community_id": int(community_id),
                "members": [], "note": f"No nodes found for scope '{scope}'"}

    dv = cf.derive_version(conn, domain)
    ckey = (kind, scope_key, round(float(resolution), 3),
            round(float(theta_e), 3), dv[0], dv[1])
    by_comm = _EXPLORE_MEMBERS_CACHE.get(ckey)
    if by_comm is None:
        result = lc.detect(deps.db, kind, domain=domain,
                           resolution=float(resolution), scope_ids=set(scope_ids),
                           theta_e=float(theta_e))
        rebuilt: dict[int, list[str]] = defaultdict(list)
        for nid, cid in result.assignments:
            rebuilt[cid].append(nid)
        by_comm = dict(rebuilt)
        if len(_EXPLORE_MEMBERS_CACHE) >= _EXPLORE_CACHE_MAX:
            _EXPLORE_MEMBERS_CACHE.clear()
        _EXPLORE_MEMBERS_CACHE[ckey] = by_comm

    mem = by_comm.get(int(community_id))
    if not mem:
        return {"error": f"community {community_id} not found in scope {scope_key}"}
    capped = mem[: int(max_members)]
    names = _node_names(conn, kind, capped)
    return {
        "kind": kind,
        "scope": scope_key,
        "resolution": float(resolution),
        "community_id": int(community_id),
        "size": len(mem),
        "members": [{"id": nid, "name": names.get(nid, nid)} for nid in capped],
        "truncated": len(mem) > int(max_members),
    }


def _nmi_from_contingency(
    cont: dict[int, dict[str, float]],
) -> tuple[float, float, float]:
    """Compute (NMI, residual=1-NMI, H(community|tag)) from a contingency table.

    NMI = 2*I(C;T) / (H(C)+H(T)) (arithmetic-mean normalisation). Assumes
    single-label events (one forced tag per event) — multiple tags enter as
    fractional counts (approximation). H(C|T)=H(C)-I: higher values mean the
    labels explain less of the structure (more residual). Uses natural log (nats).
    """
    import math

    n = sum(sum(t.values()) for t in cont.values())
    if n <= 0:
        return 0.0, 0.0, 0.0
    comm_tot: dict[int, float] = {c: sum(t.values()) for c, t in cont.items()}
    tag_tot: dict[str, float] = defaultdict(float)
    for t in cont.values():
        for tag, cnt in t.items():
            tag_tot[tag] += cnt

    def _H(tots: Any) -> float:
        h = 0.0
        for v in tots.values():
            if v > 0:
                p = v / n
                h -= p * math.log(p)
        return h

    h_c, h_t = _H(comm_tot), _H(tag_tot)
    info = 0.0
    for c, t in cont.items():
        for tag, cnt in t.items():
            if cnt > 0:
                p = cnt / n
                info += p * math.log(p / ((comm_tot[c] / n) * (tag_tot[tag] / n)))
    nmi = (2 * info / (h_c + h_t)) if (h_c + h_t) > 0 else 0.0
    return round(nmi, 4), round(1 - nmi, 4), round(h_c - info, 4)


# Residual discovery parameters
_RESIDUAL_MIN_SIZE = 5
_BRIDGE_MIN_SHARE = 0.15      # 2nd tag at this share or above → bridge
_ANOMALY_MIN_FRACTION = 0.30  # Fraction of non-dominant forced tags threshold


def community_residual_tool(
    deps: Any,
    *,
    kind: str = "event",
    domain: str | None = None,
    top_n: int = 10,
    members_preview: int = 5,
) -> dict[str, Any]:
    """Residual discovery: latent structure not explained by explicit labels.

    Compares communities from the standard run against Discovery-axis
    (forced/user) tags, surfacing hidden knowledge that labels do not cover.
    Quantified via NMI / H(community|tag) / 1-NMI (residual).

    Prerequisite: comparison requires labels that *partition* the data
    (at least 2 distinct forced tags). If this condition is not met the
    tool returns ``applicable=false`` rather than producing misleading results.

    Four modes:
    - ``emergent`` — community with no dominant forced tag = unlabelled latent
      concept (new tag candidate).
    - ``cross_label_bridge`` — one community spans 2+ forced tags (undeclared
      cross-dependency).
    - ``label_anomaly`` — community dominant tag differs from constituent
      events' forced tags (mis-classification or hidden connection).
    - Scoped sub-structure (mode 1) is handled by
      ``mweft_community_explore(scope=tag:...)``.
    """
    if kind != "event":
        return {"applicable": False,
                "reason": "Residual discovery is limited to event communities "
                          "(tags attach to events)",
                "kind": kind}
    graph = deps.db.graph
    run = resolve_latest_run(graph, _KIND[kind], domain)
    if run is None:
        return {"applicable": False, "reason": f"no completed {_KIND[kind]} run",
                "kind": kind, "domain": domain}
    run_id = run["id"]

    # Community sizes + Discovery-axis contingency table
    members = graph.list_community_members(run_id, kind)
    sizes: dict[int, int] = defaultdict(int)
    for m in members:
        sizes[m["community_id"]] += 1

    try:
        rows = _event_tag_crosstab(graph, run_id)
    except Exception:  # noqa: BLE001
        rows = []
    cont: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    tag_name: dict[str, str] = {}
    forced_tags: set[str] = set()
    for cid, gid, gname, source, n in rows:
        if resolve_axis(source).role != "discovery":  # forced/user tags only
            continue
        cont[cid][gid] += n
        tag_name[gid] = gname
        forced_tags.add(gid)

    distinct = len(forced_tags)
    if distinct < 2:
        return {
            "applicable": False,
            "reason": (
                "Fewer than 2 distinct forced/user tags — "
                "comparison-based discovery is not applicable. "
                "Add 2 or more distinct project/topic tags via the Manager UI "
                "or CLI curated '--tag' to activate this feature."
            ),
            "kind": kind, "domain": domain, "run_id": run_id,
            "distinct_forced_tags": distinct,
        }

    nmi, residual, h_c_given_t = _nmi_from_contingency(cont)

    # ── Four modes ──────────────────────────────────────────────────────────
    conn = graph._conn
    emergent, bridges, anomalies = [], [], []
    for cid, size in sizes.items():
        if size < _RESIDUAL_MIN_SIZE:
            continue
        dist = cont.get(cid, {})
        cov = sum(dist.values())
        if cov == 0:
            emergent.append({"community_id": cid, "size": size})
            continue
        ranked = sorted(dist.items(), key=lambda kv: -kv[1])
        dom_gid, dom_n = ranked[0]
        dom_share = dom_n / cov
        # cross_label_bridge — 2nd tag has meaningful share
        if len(ranked) >= 2 and (ranked[1][1] / cov) >= _BRIDGE_MIN_SHARE:
            bridges.append({
                "community_id": cid, "size": size,
                "tags": [tag_name.get(g, g) for g, _ in ranked[:3]],
                "score": round(1 - dom_share, 3),
            })
        # label_anomaly — high fraction of non-dominant forced tags
        if (1 - dom_share) >= _ANOMALY_MIN_FRACTION:
            anomalies.append({
                "community_id": cid, "size": size,
                "dominant_tag": tag_name.get(dom_gid, dom_gid),
                "anomaly_fraction": round(1 - dom_share, 3),
            })

    # Resolve top_member names for emergent communities
    if emergent:
        em_ids = [m["node_id"] for m in members
                  if m["community_id"] in {e["community_id"] for e in emergent}]
        names = _node_names(conn, kind, em_ids)
        by_comm: dict[int, list[str]] = defaultdict(list)
        for m in members:
            if m["community_id"] in {e["community_id"] for e in emergent}:
                by_comm[m["community_id"]].append(m["node_id"])
        for e in emergent:
            mem = by_comm.get(e["community_id"], [])
            e["top_members"] = [names.get(x, x) for x in mem[:members_preview]]

    emergent.sort(key=lambda e: -e["size"])
    bridges.sort(key=lambda b: -b["score"])
    anomalies.sort(key=lambda a: -a["anomaly_fraction"])

    return {
        "applicable": True,
        "kind": kind, "domain": domain, "run_id": run_id,
        "distinct_forced_tags": distinct,
        "axis": "discovery(forced/user)",
        "quantification": {
            "nmi": nmi, "residual": residual,
            "h_community_given_tag": h_c_given_t,
            "interpretation": (
                "NMI~1 means labels match structure (little hidden knowledge); "
                "higher residual means more to discover"
            ),
        },
        "modes": {
            "emergent": emergent[: int(top_n)],
            "cross_label_bridge": bridges[: int(top_n)],
            "label_anomaly": anomalies[: int(top_n)],
        },
        "note": "Scoped sub-structure (mode 1): use mweft_community_explore(scope=tag:<gid>).",
    }


def community_summarize_tool(
    deps: Any,
    *,
    kind: str = "entity",
    domain: str | None = None,
    top_n: int = 10,
    members_preview: int = 5,
) -> dict[str, Any]:
    """Full community summary — configured parameters + distribution + top communities.

    Returns the clustering configuration stored by the manager
    (resolution/seed) together with the community structure of the latest
    completed run (count / size distribution / top members), so that an LLM
    can narrate and label the overall picture. Read-only.

    Key fields returned:
      - ``configured_params``  — manager-stored config {resolution, seed,
        saved_at, is_default}
      - ``run_params`` / ``metrics`` — parameters and metrics (e.g. modularity)
        from the actual run
      - ``size_stats``  — {largest, smallest, mean, median, singletons}
      - ``communities`` — top_n communities sorted by size descending, each
        with {community_id, size, share, top_members}
    """
    if kind not in _KIND:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    configured = _read_configured_params(deps, domain)
    run = resolve_latest_run(deps.db.graph, _KIND[kind], domain)
    if run is None:
        return {
            "kind": kind,
            "domain": domain,
            "configured_params": configured,
            "run_id": None,
            "num_communities": 0,
            "total_members": 0,
            "size_stats": {},
            "communities": [],
            "note": f"no completed {_KIND[kind]} run — run scripts/train_community.py",
        }
    run_id = run["id"]
    members = deps.db.graph.list_community_members(run_id, kind)
    groups: dict[int, list[dict]] = defaultdict(list)
    for m in members:
        groups[m["community_id"]].append(m)

    total = len(members)
    sizes = sorted((len(v) for v in groups.values()), reverse=True)
    size_stats = {
        "largest": sizes[0] if sizes else 0,
        "smallest": sizes[-1] if sizes else 0,
        "mean": round(total / len(sizes), 2) if sizes else 0,
        "median": statistics.median(sizes) if sizes else 0,
        "singletons": sum(1 for s in sizes if s == 1),
    }

    communities = []
    sizes_by_comm: dict[int, int] = {}
    for cid, mem in groups.items():
        mem.sort(key=lambda x: -x["weight"])
        sizes_by_comm[cid] = len(mem)
        communities.append({
            "community_id": cid,
            "size": len(mem),
            "share": round(len(mem) / total, 4) if total else 0.0,
            "top_members": [x["name"] for x in mem[:members_preview]],
        })
    communities.sort(key=lambda c: -c["size"])

    # Compute cheap explore_hints (bundled with summary, opt-in exploration gate)
    metrics = _metrics(run)
    modularity = metrics.get("modularity")
    resolution = float(
        _json_field(run, "params_json").get("resolution")
        or configured.get("resolution") or 1.0
    )
    try:
        explore_worth, explore_hints, axes_summary = _compute_explore_hints(
            deps.db.graph, kind=kind, run_id=run_id,
            sizes_by_comm=sizes_by_comm,
            largest=size_stats["largest"],
            median_size=float(size_stats["median"] or 0),
            modularity=(float(modularity) if modularity is not None else None),
            resolution=resolution,
        )
    except Exception:  # noqa: BLE001 — best-effort; always return the summary
        explore_worth, explore_hints, axes_summary = 0.0, [], {}

    return {
        "kind": kind,
        "domain": domain,
        "run_id": run_id,
        "finished_at": str(run["finished_at"]) if run.get("finished_at") else None,
        "triggered_by": run.get("triggered_by"),
        "configured_params": configured,
        "run_params": _json_field(run, "params_json"),
        "metrics": metrics,
        "num_communities": len(groups),
        "total_members": total,
        "size_stats": size_stats,
        "communities": communities[: int(top_n)],
        # Discovery signals (source-axis x criterion, fixed Top-K)
        "explore_worth": explore_worth,
        "explore_hints": explore_hints,
        "explore_axes": axes_summary,
    }
