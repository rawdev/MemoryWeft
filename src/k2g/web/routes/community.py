"""Auto-Tag Analysis API (Manager UI Page 4).

Thin HTTP layer over the committed community pipeline:
  - read: latest leiden run auto-tags + members (``mcp.community_tools``)
  - hub review: DF% / #area / type signals + stopword toggle (``curation.hub_candidates``)
  - recompute: synchronous leiden batch (``trainer.community_runner``)

The user-facing name is "auto-tag" (the system's cluster label). Internal identifiers
and DB column names (community_id / context_groups table) are kept as-is.

No new community math here — endpoints reuse the existing functions. Recompute
is synchronous in-request, matching ``/train/jaccard`` + ``/train/hdbscan``
(entity ≈2.7s / event ≈0.1s on the real DB).
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_db_dep, sanitize
from k2g.trainer.community_freshness import resolve_latest_run
from k2g.web.routes._sql import q_all, q_exec, q_one, q_rollback

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auto-tag"])

_KINDS = ("entity", "event")


def _shim(db: Any) -> SimpleNamespace:
    """community_tools handlers only touch ``deps.db.graph``."""
    return SimpleNamespace(db=db)


def _rename_community_to_auto_tag(payload: dict) -> dict:
    """Surface rename: community_* -> auto_tag_* in list / detail responses.

    Converts the keys produced internally by community_tools (communities,
    num_communities, community_id) into surface keys (auto_tags, num_auto_tags,
    auto_tag_id). Internal functions/DB stay as-is.
    """
    if "communities" in payload and isinstance(payload["communities"], list):
        for c in payload["communities"]:
            if isinstance(c, dict) and "community_id" in c:
                c["auto_tag_id"] = c.pop("community_id")
        payload["auto_tags"] = payload.pop("communities")
    if "num_communities" in payload:
        payload["num_auto_tags"] = payload.pop("num_communities")
    if "community_id" in payload:
        payload["auto_tag_id"] = payload.pop("community_id")
    if "community" in payload and isinstance(payload["community"], dict):
        c = payload["community"]
        if "community_id" in c:
            c["auto_tag_id"] = c.pop("community_id")
        payload["auto_tag"] = payload.pop("community")
    return payload


def _ensure_run(db: Any, kind: str) -> None:
    """Auto-compute one leiden run when none exists yet, so a Manager view loads
    instead of erroring with "no completed … run".

    Synchronous (sub-3s on the real DB) and once-only: a run already present
    short-circuits. Computed cross-domain (``domain=None``) so domain-scoped
    views fall back to it. Best-effort — on failure (empty graph, missing
    igraph/leidenalg, …) the caller still sees the original no-run response.
    Manager-only: the MCP ``community_*`` tools keep their explicit no-run
    behavior (no surprise heavy compute inside an AI tool call)."""
    leiden_kind = "leiden_entity" if kind == "entity" else "leiden_event"
    try:
        run = resolve_latest_run(db.graph, leiden_kind, None)
    except Exception:  # noqa: BLE001
        run = None
    if run and run.get("id"):
        return
    try:
        from k2g.trainer.community_runner import run_community
        run_community(db, kind, domain=None, triggered_by="auto-on-view")
        logger.info("auto-computed first %s leiden run (Manager view)", leiden_kind)
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto community run (%s) skipped: %s", kind, exc)


@router.get("/auto-tags")
def auto_tag_list(
    kind: str = Query("entity"),
    domain: str | None = Query(None),
    top_n: int = Query(20, ge=1, le=200),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Auto-tags of the latest completed run (size + top member names)."""
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    from k2g.mcp.community_tools import community_list_tool

    shim = _shim(db)
    _ensure_run(db, kind)  # first open with no run → compute one, then load
    res = community_list_tool(shim, kind=kind, domain=domain, top_n=top_n)
    # The batch trainer often runs cross-domain (domain=NULL); a domain-scoped
    # view should fall back to the latest global run instead of showing empty.
    if domain and not res.get("communities"):  # tool response still uses internal keys
        alt = community_list_tool(shim, kind=kind, domain=None, top_n=top_n)
        if alt.get("communities"):
            alt["note"] = f"no run specific to '{domain}' — showing the latest cross-domain run"
            res = alt
    return sanitize(_rename_community_to_auto_tag(res))


@router.get("/auto-tags/of/{node_id}")
def auto_tag_of(
    node_id: str,
    kind: str = Query("entity"),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Which auto-tag a given entity/event belongs to + sample peers."""
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    from k2g.mcp.community_tools import community_of_tool

    _ensure_run(db, kind)  # auto-compute on first view if no run yet
    return sanitize(_rename_community_to_auto_tag(
        community_of_tool(_shim(db), node_id=node_id, kind=kind)
    ))


@router.get("/auto-tags/hub-candidates")
def hub_candidates(
    domain: str | None = Query(None),
    top_n: int = Query(50, ge=1, le=500),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Top-N entities by event count with hub signals (DF% / #area / type).

    No auto-exclusion — data for human review. ``is_stopword`` reflects the
    current ``user_tag`` so the UI checkbox starts in the right state.
    """
    from k2g.curation.hub_candidates import compute_hub_candidates

    cands = compute_hub_candidates(db, domain, top_n=top_n)
    return sanitize({
        "domain": domain,
        "candidates": [dataclasses.asdict(c) for c in cands],
    })


@router.post("/auto-tags/stopword")
def set_stopword(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Mark/unmark entities as community stopwords (analysis exclusion only).

    Two body shapes:
      - single: ``{"entity_id": str, "on": bool}``
      - batch: ``{"entity_ids": [str, ...], "on": bool}``  (checkbox batch apply)
    Persists ``user_tag='stopword'``; takes effect on the next recompute.
    """
    from k2g.curation.hub_candidates import set_entity_stopword

    on = bool(body.get("on", True))
    raw_ids = body.get("entity_ids")
    if raw_ids is None:
        raw = (body.get("entity_id") or "").strip()
        if not raw:
            return {"error": "entity_id or entity_ids is required"}
        ids = [raw]
    else:
        if not isinstance(raw_ids, list):
            return {"error": "entity_ids must be a list of strings"}
        ids = [str(x).strip() for x in raw_ids if str(x).strip()]
        if not ids:
            return {"error": "entity_ids must not be empty"}

    updated: list[str] = []
    for eid in ids:
        set_entity_stopword(db, eid, on)
        updated.append(eid)
    return {"updated": updated, "count": len(updated), "is_stopword": on}


# ---------------------------------------------------------------------------
# Analysis param storage — save the Leiden parameters (resolution/seed) as a
# per-domain history. It's a lightweight Manager-side settings table, so it is
# lazily created here in the router (not in the core schema) via
# CREATE TABLE IF NOT EXISTS, on the same SQLite/Postgres DB.

_ANALYSIS_PARAM_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS analysis_param (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT,
    params_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_ANALYSIS_PARAM_DDL_PG = """
CREATE TABLE IF NOT EXISTS analysis_param (
    id          BIGSERIAL PRIMARY KEY,
    domain      TEXT,
    params_json TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _is_postgres(conn: Any) -> bool:
    return "psycopg2" in type(conn).__module__.lower()


def _ph(conn: Any) -> str:
    return "%s" if _is_postgres(conn) else "?"


def _ensure_analysis_param_table(conn: Any) -> None:
    # Self-heal: a shared psycopg2 connection may have been left in an aborted
    # transaction by an earlier failed statement, which would make this DDL raise
    # InFailedSqlTransaction. Roll that back first (PG-only, no-op on SQLite).
    q_rollback(conn)
    ddl = _ANALYSIS_PARAM_DDL_PG if _is_postgres(conn) else _ANALYSIS_PARAM_DDL_SQLITE
    q_exec(conn, ddl)
    if hasattr(conn, "commit"):
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass


@router.get("/analysis/params")
def get_analysis_params(
    domain: str | None = Query(None),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Return the latest saved Leiden parameters; defaults if none.

    Response: ``{"domain": str|None, "params": {resolution, seed},
                 "saved_at": str|None, "is_default": bool}``.
    """
    DEFAULTS = {"resolution": 1.0, "seed": 42, "theta_e": 0.4}
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {
            "domain": domain, "params": DEFAULTS,
            "saved_at": None, "is_default": True,
            "note": "graph store does not expose _conn — returning defaults",
        }
    try:
        _ensure_analysis_param_table(conn)
        ph = _ph(conn)
        if domain:
            sql = (
                "SELECT params_json, created_at FROM analysis_param "
                f"WHERE domain = {ph} "
                "ORDER BY id DESC LIMIT 1"
            )
            row = q_one(conn, sql, (domain,))
        else:
            sql = (
                "SELECT params_json, created_at FROM analysis_param "
                "WHERE domain IS NULL "
                "ORDER BY id DESC LIMIT 1"
            )
            row = q_one(conn, sql)
    except Exception as exc:  # noqa: BLE001
        logger.exception("analysis/params GET failed: %s", exc)
        q_rollback(conn)  # don't leave the shared connection poisoned for the next request
        return {
            "domain": domain, "params": DEFAULTS,
            "saved_at": None, "is_default": True,
            "error": str(exc),
        }
    if row is None:
        return {
            "domain": domain, "params": DEFAULTS,
            "saved_at": None, "is_default": True,
        }
    import json as _json
    raw = row[0] if not isinstance(row, dict) else row.get("params_json")
    saved_at = row[1] if not isinstance(row, dict) else row.get("created_at")
    try:
        loaded = _json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        loaded = {}
    merged = {**DEFAULTS, **(loaded if isinstance(loaded, dict) else {})}
    return sanitize({
        "domain": domain, "params": merged,
        "saved_at": saved_at, "is_default": False,
    })


@router.post("/analysis/params")
def save_analysis_params(
    body: dict,
    domain: str | None = Query(None),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Save the Leiden parameters as append-only history.

    Body: ``{"resolution": float, "seed": int}`` (both optional, defaults used).
    Query: ``domain`` — per-domain separation (NULL = all).
    """
    import json as _json
    DEFAULTS = {"resolution": 1.0, "seed": 42, "theta_e": 0.4}
    body = body or {}
    try:
        res = float(body.get("resolution", DEFAULTS["resolution"]))
        if res <= 0:
            return {"error": "resolution must be > 0"}
    except (TypeError, ValueError):
        return {"error": "resolution must be a positive number"}
    try:
        seed = int(body.get("seed", DEFAULTS["seed"]))
    except (TypeError, ValueError):
        return {"error": "seed must be an integer"}
    try:
        theta_e = float(body.get("theta_e", DEFAULTS["theta_e"]))
        if not (0.0 <= theta_e <= 1.0):
            return {"error": "theta_e must be in [0, 1]"}
    except (TypeError, ValueError):
        return {"error": "theta_e must be a number in [0, 1]"}
    params = {"resolution": res, "seed": seed, "theta_e": theta_e}

    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    try:
        _ensure_analysis_param_table(conn)
        ph = _ph(conn)
        sql = (
            "INSERT INTO analysis_param (domain, params_json) "
            f"VALUES ({ph}, {ph})"
        )
        q_exec(conn, sql, (domain, _json.dumps(params)))
        if hasattr(conn, "commit"):
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("analysis/params POST failed: %s", exc)
        q_rollback(conn)  # don't leave the shared connection poisoned for the next request
        return {"error": str(exc)}
    return sanitize({"domain": domain, "params": params, "saved": True})


@router.post("/auto-tags/recompute")
def recompute(
    target: str = Query("both"),
    domain: str | None = Query(None),
    seed: int = Query(42),
    resolution: float = Query(1.0, gt=0.0),
    theta_e: float = Query(0.4, ge=0.0, le=1.0),
    dry_run: bool = Query(False),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Run leiden community detection now (synchronous).

    ``target`` ∈ {entity, event, both}. Returns a per-target result list. Stays
    in-request like the other train triggers — measured sub-3s on the real DB.
    """
    if target not in (*_KINDS, "both"):
        return {"error": f"target must be entity|event|both, got {target!r}"}
    from k2g.trainer.community_runner import run_community

    targets = list(_KINDS) if target == "both" else [target]
    results: list[dict] = []
    for t in targets:
        try:
            results.append(
                run_community(
                    db, t, domain=domain, seed=seed,
                    resolution=resolution, theta_e=theta_e, dry_run=dry_run,
                    triggered_by="manual",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("recompute failed target=%s: %s", t, exc)
            results.append({"target": t, "error": str(exc)})
    return sanitize({"target": target, "domain": domain, "runs": results})


# ---------------------------------------------------------------------------
# ego graph (visual debugging on the Analysis page + hub-decision aid)
# ---------------------------------------------------------------------------


def _community_color(community_id: int | None) -> str:
    """deterministic hash -> hex color. The same community_id always maps to the same color.

    cluster_id is volatile across each leiden recompute -> so is the color
    (consistent only within a call).
    """
    if community_id is None:
        return "#9ca3af"  # neutral gray
    # simple hash -> distribute around the HSL color wheel -> hex
    h = (int(community_id) * 137 + 23) % 360
    # HSL(h, 65%, 55%) -> RGB conversion (simple formula)
    import colorsys
    r, g, b = colorsys.hls_to_rgb(h / 360.0, 0.55, 0.65)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def _fetch_community(graph: Any, kind: str, node_ids: list[str]) -> dict[str, int]:
    """ego node ids -> community_id mapping (based on the latest leiden run).

    Uses the same graph method as ``_fetch_leiden_assignment`` in hint.py — consistent.
    """
    if not node_ids:
        return {}
    run_kind = "leiden_entity" if kind == "entity" else "leiden_event"
    try:
        run = resolve_latest_run(graph, run_kind)
    except Exception:  # noqa: BLE001
        return {}
    if not run or not run.get("id"):
        return {}
    method = (
        "get_entity_community_for_run" if kind == "entity"
        else "get_event_community_for_run"
    )
    try:
        return getattr(graph, method)(run["id"], node_ids) or {}
    except Exception:  # noqa: BLE001
        return {}


def _ego_entity(db: Any, node_id: str, max_n: int) -> dict:
    """entity_connection 1-hop + community + degree cap.

    Core is untouched — direct SQL on the graph store ``_conn``.
    """
    graph = db.graph
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = "%s" if "psycopg2" in type(conn).__module__.lower() else "?"
    # center info
    row = q_one(conn,
        f"SELECT id, name, type, domain FROM entities WHERE id = {ph}",
        (node_id,),
    )
    if not row:
        return {"error": f"entity not found: {node_id}"}
    if isinstance(row, dict):
        cid, cname, ctype, cdom = row["id"], row["name"], row["type"], row["domain"]
    else:
        cid, cname, ctype, cdom = row[0], row[1], row[2], row[3]
    # 1-hop neighbors (entity_connection a<b normalized, bidirectional union)
    rows = q_all(conn,
        f"SELECT b_id AS nb, event_count FROM entity_connection WHERE a_id = {ph} "
        f"UNION ALL "
        f"SELECT a_id AS nb, event_count FROM entity_connection WHERE b_id = {ph} "
        f"ORDER BY event_count DESC LIMIT {ph}",
        (node_id, node_id, int(max_n)),
    )
    neighbors: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for r in rows:
        nb_id = r[0] if not isinstance(r, dict) else r["nb"]
        weight = r[1] if not isinstance(r, dict) else r["event_count"]
        neighbors.append({"id": nb_id, "weight": int(weight or 0)})
        edges.append({"a": node_id, "b": nb_id, "weight": int(weight or 0)})
    nb_ids = [n["id"] for n in neighbors]
    # name lookup
    name_map: dict[str, dict] = {}
    if nb_ids:
        marks = ",".join([ph] * len(nb_ids))
        for r in q_all(conn,
            f"SELECT id, name, type FROM entities WHERE id IN ({marks})",
            nb_ids,
        ):
            rid = r[0] if not isinstance(r, dict) else r["id"]
            name_map[rid] = {
                "name": r[1] if not isinstance(r, dict) else r["name"],
                "type": r[2] if not isinstance(r, dict) else r["type"],
            }
    # community
    all_ids = [node_id] + nb_ids
    comm = _fetch_community(graph, "entity", all_ids)
    center_cid = comm.get(node_id)
    nodes_out: list[dict[str, Any]] = []
    for nb in neighbors:
        nb_cid = comm.get(nb["id"])
        info = name_map.get(nb["id"], {})
        nodes_out.append({
            "id": nb["id"],
            "name": info.get("name") or nb["id"],
            "type": info.get("type"),
            "community_id": nb_cid,
            "weight": nb["weight"],
            "kind": "entity",
        })
    color_map: dict[str, str] = {}
    for cid_seen in {center_cid, *comm.values()}:
        if cid_seen is not None:
            color_map[str(cid_seen)] = _community_color(cid_seen)
    return {
        "kind": "entity",
        "center": {
            "id": cid, "name": cname, "type": ctype, "domain": cdom,
            "community_id": center_cid,
            "metrics": {"degree": len(neighbors)},
        },
        "nodes": nodes_out,
        "edges": edges,
        "community_colors": color_map,
        "shared_entities": [],
    }


def _ego_event(db: Any, node_id: str, max_n: int) -> dict:
    """event_jaccard_connected 1-hop + community + shared_entities.

    shared_entities: the participated_in entities common to the ego nodes (center +
    neighbor events), sorted by hit_count. Event->entity closed-loop data.
    """
    graph = db.graph
    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = "%s" if "psycopg2" in type(conn).__module__.lower() else "?"
    row = q_one(conn,
        f"SELECT id, domain, summary FROM events WHERE id = {ph}",
        (node_id,),
    )
    if not row:
        return {"error": f"event not found: {node_id}"}
    if isinstance(row, dict):
        cid, cdom, csum = row["id"], row["domain"], row["summary"]
    else:
        cid, cdom, csum = row[0], row[1], row[2]
    # 1-hop event_jaccard_connected neighbors
    rows = q_all(conn,
        f"SELECT b_id AS nb, entity_jaccard FROM event_jaccard_connected "
        f"WHERE a_id = {ph} AND entity_jaccard > 0 "
        f"UNION ALL "
        f"SELECT a_id AS nb, entity_jaccard FROM event_jaccard_connected "
        f"WHERE b_id = {ph} AND entity_jaccard > 0 "
        f"ORDER BY entity_jaccard DESC LIMIT {ph}",
        (node_id, node_id, int(max_n)),
    )
    neighbors: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for r in rows:
        nb_id = r[0] if not isinstance(r, dict) else r["nb"]
        jac = r[1] if not isinstance(r, dict) else r["entity_jaccard"]
        neighbors.append({"id": nb_id, "weight": float(jac or 0)})
        edges.append({"a": node_id, "b": nb_id, "weight": round(float(jac or 0), 3)})
    nb_ids = [n["id"] for n in neighbors]
    # summary lookup
    summary_map: dict[str, str] = {}
    if nb_ids:
        marks = ",".join([ph] * len(nb_ids))
        for r in q_all(conn,
            f"SELECT id, summary FROM events WHERE id IN ({marks})",
            nb_ids,
        ):
            rid = r[0] if not isinstance(r, dict) else r["id"]
            summ = r[1] if not isinstance(r, dict) else r["summary"]
            summary_map[rid] = (summ or "")[:80]
    # community
    all_ids = [node_id] + nb_ids
    comm = _fetch_community(graph, "event", all_ids)
    center_cid = comm.get(node_id)
    nodes_out: list[dict[str, Any]] = []
    for nb in neighbors:
        nb_cid = comm.get(nb["id"])
        nodes_out.append({
            "id": nb["id"],
            "summary_short": summary_map.get(nb["id"], ""),
            "community_id": nb_cid,
            "weight": round(nb["weight"], 3),
            "kind": "event",
        })
    color_map: dict[str, str] = {}
    for cid_seen in {center_cid, *comm.values()}:
        if cid_seen is not None:
            color_map[str(cid_seen)] = _community_color(cid_seen)
    # shared_entities — participated_in entities common to center + neighbors
    shared: list[dict[str, Any]] = []
    if nb_ids:
        marks = ",".join([ph] * len(all_ids))
        try:
            rows2 = q_all(conn,
                f"SELECT p.entity_id, e.name, e.user_tag, COUNT(*) AS hits "
                f"FROM participated_in p JOIN entities e ON e.id = p.entity_id "
                f"WHERE p.event_id IN ({marks}) "
                f"GROUP BY p.entity_id "
                f"HAVING COUNT(*) >= 2 "
                f"ORDER BY hits DESC LIMIT 20",
                all_ids,
            )
            for r in rows2:
                if isinstance(r, dict):
                    eid, ename, etag, hcnt = r["entity_id"], r["name"], r["user_tag"], r["hits"]
                else:
                    eid, ename, etag, hcnt = r[0], r[1], r[2], r[3]
                shared.append({
                    "id": eid,
                    "name": ename,
                    "hit_count": int(hcnt or 0),
                    "is_stopword": etag == "stopword",
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("shared_entities computation failed: %s", exc)
    return {
        "kind": "event",
        "center": {
            "id": cid, "domain": cdom,
            "summary_short": (csum or "")[:80],
            "community_id": center_cid,
            "metrics": {"degree": len(neighbors)},
        },
        "nodes": nodes_out,
        "edges": edges,
        "community_colors": color_map,
        "shared_entities": shared,
    }


@router.get("/auto-tags/ego")
def auto_tag_ego(
    kind: str = Query("entity"),
    node_id: str = Query(..., min_length=1),
    max_neighbors: int = Query(20, ge=1, le=50),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Analysis ego graph (entity / event 1-hop + community color).

    Entity: an action tool (aids the stopword decision). Event: an insight tool
    (jump to Entity Hub review via `shared_entities` — a closed loop).
    Core is untouched — SQL only.
    """
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    fn = _ego_entity if kind == "entity" else _ego_event
    return sanitize(fn(db, node_id, int(max_neighbors)))


# ---------------------------------------------------------------------------
# scoped ephemeral exploration (an unsaved lens). A read-only path separate from the
# canonical run (/auto-tags, /auto-tags/recompute). Runs leiden on the fly within a
# tag boundary + resolution to view communities, without writing to train_run
# (in-process cache only).
# ---------------------------------------------------------------------------


@router.get("/auto-tags/scope-tags")
def scope_tags(
    domain: str | None = Query(None),
    kind: str = Query("event"),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Source for the scoped-exploration dropdown — exposes only discovery-axis tags.

    Excludes autotag/container/build (thousands of path groups) and returns only
    human/system ground-truth tags (forced/user + legacy mweft_save_tag). The single
    source of truth for inclusion is ``discoverable=True`` in
    ``source_axis.SOURCE_AXIS``. Each tag's event_member_of member count is included
    so empty boundaries can be filtered out.
    """
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    from k2g.memory.source_axis import SOURCE_AXIS, resolve_axis

    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"tags": [], "note": "graph store does not expose _conn"}
    sources = [s for s, p in SOURCE_AXIS.items() if s is not None and p.discoverable]
    if not sources:
        return {"domain": domain, "kind": kind, "tags": []}
    try:
        ph = _ph(conn)
        marks = ",".join([ph] * len(sources))
        params: list[Any] = list(sources)
        dom_clause = ""
        if domain:
            dom_clause = f" AND g.domain = {ph}"
            params.append(domain)
        sql = (
            "SELECT g.id, g.name, g.source, "
            "  (SELECT COUNT(DISTINCT em.event_id) FROM event_member_of em "
            "   WHERE em.group_id = g.id) AS n "
            "FROM groups g "
            f"WHERE g.source IN ({marks}){dom_clause} "
            "ORDER BY n DESC LIMIT 200"
        )
        rows = q_all(conn, sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scope-tags failed: %s", exc)
        return {"domain": domain, "kind": kind, "tags": [], "error": str(exc)}
    tags: list[dict] = []
    for r in rows:
        if isinstance(r, dict):
            gid, name, src, n = r["id"], r["name"], r["source"], r["n"]
        else:
            gid, name, src, n = r[0], r[1], r[2], r[3]
        tags.append({
            "id": gid,
            "name": name,
            "source": src,
            "axis": resolve_axis(src).axis,
            "member_count": int(n or 0),
        })
    return sanitize({"domain": domain, "kind": kind, "tags": tags})


@router.get("/auto-tags/explore")
def auto_tag_explore(
    scope: str = Query(..., min_length=1),
    kind: str = Query("event"),
    resolution: float = Query(1.5, gt=0.0),
    theta_e: float = Query(0.4, ge=0.0, le=1.0),
    domain: str | None = Query(None),
    top_n: int = Query(30, ge=1, le=200),
    members_preview: int = Query(8, ge=1, le=50),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Scoped ephemeral community exploration (unsaved).

    Wraps ``community_explore_tool`` as-is — zero persistence, no effect on the
    canonical run (in-process cache only). The response's community_* keys get the
    same surface rename to auto_tag_* as list/detail.
    """
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    from k2g.mcp.community_tools import community_explore_tool

    res = community_explore_tool(
        _shim(db), scope=scope, kind=kind, resolution=resolution,
        theta_e=theta_e, domain=domain, top_n=top_n,
        members_preview=members_preview,
    )
    return sanitize(_rename_community_to_auto_tag(res))


@router.get("/auto-tags/explore/members")
def auto_tag_explore_members(
    scope: str = Query(..., min_length=1),
    community_id: int = Query(..., ge=0),
    kind: str = Query("event"),
    resolution: float = Query(1.5, gt=0.0),
    theta_e: float = Query(0.4, ge=0.0, le=1.0),
    domain: str | None = Query(None),
    max_members: int = Query(50, ge=1, le=500),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Member drill-down for a single scoped community (unsaved).

    Reuses the member cache with the same (scope, kind, resolution) as explore.
    Returns id+name so a click can lead into event detail / entity exploration.
    """
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    from k2g.mcp.community_tools import community_explore_members_tool

    res = community_explore_members_tool(
        _shim(db), scope=scope, community_id=community_id, kind=kind,
        resolution=resolution, theta_e=theta_e, domain=domain,
        max_members=max_members,
    )
    return sanitize(res)


@router.get("/auto-tags/{auto_tag_id}")
def auto_tag_members(
    auto_tag_id: int,
    kind: str = Query("entity"),
    run_id: str | None = Query(None),
    max_members: int = Query(50, ge=1, le=1000),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Full member list of one auto-tag (ranked by prominence)."""
    if kind not in _KINDS:
        return {"error": f"kind must be 'entity' or 'event', got {kind!r}"}
    from k2g.mcp.community_tools import community_detail_tool

    return sanitize(_rename_community_to_auto_tag(
        community_detail_tool(
            _shim(db), community_id=auto_tag_id, kind=kind,
            run_id=run_id, max_members=max_members,
        )
    ))
