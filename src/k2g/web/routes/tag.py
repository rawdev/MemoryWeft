"""Lifecycle API -- Entity/Tag lifecycle state management (soft delete/restore).

In the current naming, "tag" is the user classification label (formerly
group). The ``user_tag`` column handled by this route is a *lifecycle
state* (e.g. ``'user_deleted'``), which is semantically different --
endpoint paths use ``/lifecycle`` and body key uses ``state``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from k2g.web.deps import get_stores_dep

router = APIRouter(tags=["lifecycle"])


@router.post("/entities/{entity_id}/lifecycle")
def set_entity_lifecycle(
    entity_id: str,
    body: dict,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Set Entity lifecycle state (delete: 'user_deleted', restore: null)."""
    graph = stores["graph"]
    state = body.get("state")  # None or string (internal column: user_tag)

    entity = graph.get_entity_by_id(entity_id)
    if not entity:
        return {"error": f"Entity not found: {entity_id}"}

    graph.set_user_tag("entity", entity_id, state)
    return {"success": True}


@router.post("/tags/{tag_id}/lifecycle")
def set_tag_lifecycle(
    tag_id: str,
    body: dict,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Set Tag lifecycle state (delete: 'user_deleted', restore: null).

    Internal: graph.set_user_tag('group', tag_id, state) -- DB groups table.
    """
    graph = stores["graph"]
    state = body.get("state")

    graph.set_user_tag("group", tag_id, state)
    return {"success": True}
