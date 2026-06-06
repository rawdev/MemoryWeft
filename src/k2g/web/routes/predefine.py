"""Predefine & Edit — domain/entity/group authoring (Manager UI Page 2).

Three new endpoints over existing primitives:
  - find events whose summary contains a (new) entity's name, for human review
  - link the entity to the reviewed events + recompute its jaccard
  - soft-delete (deprecate) an entity

Entity/group *create*, domain/group *list*, and domain *summary* reuse the
existing browse.py / event.py routes. Destructive domain-delete is out of scope
for this slice (needs cross-store delete primitives — see plan).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_db_dep, sanitize
from k2g.web.routes._sql import not_deprecated, ph as _ph, q_all, q_one

logger = logging.getLogger(__name__)

router = APIRouter(tags=["predefine"])


@router.get("/predefine/entity-link-candidates")
def entity_link_candidates(
    domain: str = Query(...),
    name: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=300),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Events whose summary contains ``name`` (case-insensitive) — for review.

    Substring match bypasses the NER hallucination guards, so the matches are
    presented for a human to confirm before linking (never auto-linked).
    """
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = _ph(conn)
    sql = (
        f"SELECT e.id, e.summary, e.timestamp FROM events e "
        f"WHERE e.domain = {ph} AND {not_deprecated('e.deprecated', conn)} "
        f"AND e.summary IS NOT NULL AND LOWER(e.summary) LIKE {ph} "
        f"ORDER BY COALESCE(e.timestamp, e.created_at) DESC LIMIT {ph}"
    )
    rows: list[dict] = []
    for r in q_all(conn, sql, (domain, f"%{name.lower()}%", int(limit))):
        if isinstance(r, dict):
            rows.append({k: r.get(k) for k in ("id", "summary", "timestamp")})
        elif hasattr(r, "keys"):
            rows.append({k: r[k] for k in ("id", "summary", "timestamp")})
        else:
            rows.append({"id": r[0], "summary": r[1], "timestamp": r[2]})
    return sanitize({"domain": domain, "name": name, "candidates": rows})


@router.post("/predefine/entity-link")
def entity_link(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Link an entity to the reviewed events. Body: ``{entity_id, event_ids[], recompute=true}``."""
    entity_id = (body.get("entity_id") or "").strip()
    event_ids = [e for e in (body.get("event_ids") or []) if e]
    if not entity_id:
        return {"error": "entity_id is required"}
    if not event_ids:
        return {"error": "event_ids is required"}
    if db.graph.get_entity_by_id(entity_id) is None:
        return {"error": f"entity not found: {entity_id}"}

    linked = 0
    for ev in event_ids:
        try:
            db.graph.link_participated_in(entity_id, ev)
            linked += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("entity-link %s↔%s failed: %s", entity_id, ev, exc)

    jaccard = 0
    if bool(body.get("recompute", True)):
        try:
            from k2g.curation.jaccard_recompute import recompute_jaccard_for_entity
            jaccard = recompute_jaccard_for_entity(entity_id, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("entity-link jaccard recompute failed: %s", exc)

    return sanitize({
        "entity_id": entity_id,
        "linked": linked,
        "requested": len(event_ids),
        "jaccard_recomputed": jaccard,
    })


@router.get("/predefine/tag-merge-preview")
def tag_merge_preview(
    tag_id: str = Query(...),
    db: Any = Depends(get_db_dep),
) -> dict:
    """Read-only counts of what a merge of ``tag_id`` (as alias) would move.

    Internal: groups table / event_member_of.group_id column — column names unchanged.
    """
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"error": "graph store does not expose _conn"}
    ph = _ph(conn)

    def _count(sql: str) -> int | str:
        try:
            row = q_one(conn, sql, (tag_id,))
            return int(row[0] or 0) if row else 0
        except Exception:  # noqa: BLE001
            return "n/a"

    return sanitize({
        "tag_id": tag_id,
        "memberships": _count(f"SELECT COUNT(*) FROM event_member_of WHERE group_id = {ph}"),
        "children": _count(f"SELECT COUNT(*) FROM groups WHERE parent_id = {ph}"),
    })


@router.post("/predefine/tag-merge")
def tag_merge(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Merge alias tag into canonical. Body: ``{alias_id, canonical_id}``.

    Re-points memberships + re-parents children to canonical, soft-deletes alias.
    Internal: graph.merge_groups(...).
    """
    alias_id = (body.get("alias_id") or "").strip()
    canonical_id = (body.get("canonical_id") or "").strip()
    if not alias_id or not canonical_id:
        return {"error": "alias_id and canonical_id are required"}
    try:
        result = db.graph.merge_groups(alias_id, canonical_id)
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error("tag-merge %s→%s failed: %s", alias_id, canonical_id, exc)
        return {"error": f"tag merge failed: {exc}"}
    return sanitize(result)


@router.post("/predefine/entity-deprecate")
def entity_deprecate(
    body: dict,
    db: Any = Depends(get_db_dep),
) -> dict:
    """Soft-delete an entity (``deprecated=1``). Body: ``{entity_id}``."""
    entity_id = (body.get("entity_id") or "").strip()
    if not entity_id:
        return {"error": "entity_id is required"}
    try:
        ok = db.graph.mark_deprecated("entity", entity_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("entity-deprecate %s failed: %s", entity_id, exc)
        return {"error": f"deprecate failed: {exc}"}
    return {"entity_id": entity_id, "deprecated": bool(ok)}
