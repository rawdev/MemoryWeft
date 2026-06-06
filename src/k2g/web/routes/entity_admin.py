"""Entity admin — merge & dedup (Manager UI Page 2-2).

Thin HTTP layer over the BP-87 curation track:
  - find similar (normalize + fuzzy) candidates
  - read-only merge preview (affected row counts)
  - merge (replace links + soft-delete alias + k2g_entity_alias audit + recompute)
  - unmerge (reactivate alias + drop audit row; link-level restore is impossible)

No new merge math/schema — endpoints wrap ``k2g.curation``. Manual entity pick
reuses the existing ``GET /api/entities/search`` (browse.py).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_db_dep, sanitize
from k2g.web.routes._sql import q_one

logger = logging.getLogger(__name__)

router = APIRouter(tags=["entity-admin"])


def _ph(conn: Any) -> str:
    return "%s" if "psycopg2" in type(conn).__module__.lower() else "?"


_FUZZY_ENTITY_CAP = 3000  # fuzzy layer is O(buckets^2) — guard large domains


@router.get("/entities/merge-candidates")
def merge_candidates(
    domain: str | None = Query(None),
    threshold: float = Query(0.85, ge=0.0, le=1.0),
    max_results: int = Query(50, ge=1, le=500),
    fuzzy: bool = Query(False),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Suggest (alias → canonical) pairs.

    normalize layer (case/space, confidence 1.0) is always on. The fuzzy/typo
    layer is opt-in (``fuzzy=true``) and auto-disabled on large domains because
    it is O(buckets^2) — matches the spec, which defers typo matching.
    """
    from k2g.curation import find_candidates

    note: str | None = None
    if fuzzy:
        conn = getattr(db.graph, "_conn", None)
        n = 0
        if conn is not None:
            ph = _ph(conn)
            try:
                if domain:
                    n = int(q_one(conn,
                        f"SELECT COUNT(*) FROM entities WHERE domain = {ph}",
                        (domain,))[0] or 0)
                else:
                    n = int(q_one(conn, "SELECT COUNT(*) FROM entities")[0] or 0)
            except Exception:  # noqa: BLE001
                n = 0
        if n > _FUZZY_ENTITY_CAP:
            fuzzy = False
            note = (f"fuzzy disabled — {n} entities (normalize-only). "
                    f"Narrow the domain to enable typo matching.")

    cands = find_candidates(
        db, domain=domain, similarity_threshold=threshold,
        max_results=max_results, fuzzy=fuzzy,
    )
    out: dict[str, Any] = {
        "domain": domain,
        "fuzzy": fuzzy,
        "candidates": [dataclasses.asdict(c) for c in cands],
    }
    if note:
        out["note"] = note
    return sanitize(out)


@router.get("/entities/merge-preview")
def merge_preview(
    alias_id: str = Query(...),
    canonical_id: str = Query(...),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Read-only counts of rows the merge would move (alias side)."""
    if alias_id == canonical_id:
        return {"error": "alias and canonical must differ"}
    graph = db.graph
    alias = graph.get_entity_by_id(alias_id)
    canonical = graph.get_entity_by_id(canonical_id)
    if alias is None:
        return {"error": f"alias not found: {alias_id}"}
    if canonical is None:
        return {"error": f"canonical not found: {canonical_id}"}

    conn = getattr(graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = _ph(conn)

    def _count(sql: str, params: tuple) -> int | str:
        try:
            row = q_one(conn, sql, params)
            return int(row[0] or 0) if row else 0
        except Exception:  # noqa: BLE001
            return "n/a"

    participations = _count(
        f"SELECT COUNT(*) FROM participated_in WHERE entity_id = {ph}",
        (alias_id,),
    )
    connections = _count(
        f"SELECT COUNT(*) FROM entity_connection WHERE a_id = {ph} OR b_id = {ph}",
        (alias_id, alias_id),
    )
    return sanitize({
        "alias": {"id": alias_id, "name": alias.get("name")},
        "canonical": {"id": canonical_id, "name": canonical.get("name")},
        "alias_participations": participations,
        "alias_connections": connections,
    })


@router.post("/entities/merge")
def merge(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Merge alias into canonical. Body: ``{alias_id, canonical_id, recompute=true}``.

    Soft-deletes the alias (``deprecated``), writes a ``k2g_entity_alias`` audit
    row, and (default) recomputes the canonical's jaccard + vector.
    """
    from k2g.curation import merge_entities

    alias_id = (body.get("alias_id") or "").strip()
    canonical_id = (body.get("canonical_id") or "").strip()
    if not alias_id or not canonical_id:
        return {"error": "alias_id and canonical_id are required"}
    recompute = bool(body.get("recompute", True))
    try:
        result = merge_entities(
            alias_id, canonical_id, db,
            asserted_by="manager_ui", recompute=recompute,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error("merge failed %s→%s: %s", alias_id, canonical_id, exc)
        return {"error": f"merge failed: {exc}"}
    return sanitize(dataclasses.asdict(result))


@router.post("/entities/unmerge")
def unmerge(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Reactivate a previously-merged alias. Body: ``{alias_id}``.

    Only un-deprecates the alias + removes its audit row — link-level restore
    is impossible (the original participated_in/entity_connection rows were
    rewritten to the canonical at merge time).
    """
    from k2g.curation import unmerge_entity

    alias_id = (body.get("alias_id") or "").strip()
    if not alias_id:
        return {"error": "alias_id is required"}
    try:
        ok = unmerge_entity(alias_id, db)
    except Exception as exc:  # noqa: BLE001
        logger.error("unmerge failed %s: %s", alias_id, exc)
        return {"error": f"unmerge failed: {exc}"}
    if not ok:
        return {"alias_id": alias_id, "reactivated": False,
                "note": "no alias record found for this id"}
    return {"alias_id": alias_id, "reactivated": True}
