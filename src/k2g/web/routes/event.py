"""Event API -- Event detail, events by Entity/Group."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from k2g.web.deps import get_stores_dep, sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["event"])


@router.get("/events")
def list_events_timeline(
    domain: str = Query(..., description="Domain name"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    order: str = Query(
        "timestamp", pattern="^(timestamp|order_index|created_at)$",
    ),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Domain-wide timeline (paginated + sorted)."""
    graph = stores["graph"]
    content_store = stores["content"]

    events, total = graph.list_events_paged(
        domain=domain, order_by=order, page=page, size=size,
    )
    for ev in events:
        # ``events.summary`` column is already populated by both the build
        # and mweft_remember paths -- use it first. content_store
        # .inline_meta.event_summary is a build-pipeline-only fallback
        # for legacy data compatibility.
        summary = ev.get("summary") or ""
        if not summary:
            vid = ev.get("vector_id", "")
            if vid:
                try:
                    cr = content_store.get_by_vector_id(vid)
                    if cr:
                        summary = (
                            cr.inline_meta.get("event_summary")
                            or cr.inline_meta.get("content", "")[:200]
                            or ""
                        )
                except Exception as e:  # noqa: BLE001
                    # Prevent a single row's deserialize error from 500-ing the whole timeline.
                    logger.warning(
                        "Timeline: content lookup failed for vector_id=%s: %s",
                        vid, e,
                    )
        ev["summary"] = summary

    return sanitize({
        "domain": domain,
        "events": events,
        "total": total,
        "page": page,
        "size": size,
        "order": order,
    })


@router.get("/events/{event_id}")
def get_event_detail(
    event_id: str,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Event detail view.

    Returns summary + original text + participating Entity/Group +
    previous/next Event.
    """
    graph = stores["graph"]
    content_store = stores["content"]
    object_storage = stores["objects"]

    # 1. Event node properties
    event = graph.get_event_by_id(event_id)
    if not event:
        return {"error": f"Event not found: {event_id}"}

    # 2. Look up summary from Content Store
    vector_id = event.get("vector_id", "")
    summary = ""
    original_text = ""

    if vector_id:
        content_record = content_store.get_by_vector_id(vector_id)
        logger.info("Event %s: vector_id=%s, content_record=%s", event_id, vector_id, content_record is not None)
        if content_record:
            summary = content_record.inline_meta.get("event_summary", "")
            logger.info("Event %s: storage_uri=%s", event_id, content_record.storage_uri)
            # 3. Look up original text from Object Storage
            try:
                original_text = object_storage.get_text(content_record.storage_uri)
            except Exception as e:
                logger.warning("Original text lookup failed: %s (uri=%s)", e, content_record.storage_uri)
                original_text = ""
        else:
            logger.warning("Event %s: no content_record (vector_id=%s)", event_id, vector_id)

    # 4. Participating Entity + Group
    context = graph.get_event_context(event_id)

    # 5. Previous/next Event -- interface returns {prev_id, next_id}.
    adjacent = graph.get_adjacent_events(event_id)

    def _resolve_adj(adj_id: str | None) -> dict | None:
        if not adj_id:
            return None
        try:
            ev = graph.get_event_by_id(adj_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("get_event_by_id(%s) failed: %s", adj_id, e)
            return {"id": adj_id, "summary": ""}
        summary = ""
        vid = ev.get("vector_id", "") if ev else ""
        if vid:
            try:
                cr = content_store.get_by_vector_id(vid)
                if cr:
                    summary = cr.inline_meta.get("event_summary", "") or ""
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "content lookup failed (vector_id=%s): %s", vid, e,
                )
        return {"id": adj_id, "summary": summary}

    prev_event = _resolve_adj(adjacent.get("prev_id"))
    next_event = _resolve_adj(adjacent.get("next_id"))

    return sanitize({
        "event": {
            "id": event.get("id"),
            "timestamp": event.get("timestamp"),
            "order_index": event.get("order_index"),
            "domain": event.get("domain"),
        },
        "summary": summary,
        "original_text": original_text,
        "entities": context["entities"],
        "groups": context["groups"],
        "prev_event": prev_event,
        "next_event": next_event,
    })


@router.get("/entities/{entity_id}/events")
def get_entity_events(
    entity_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    include_deleted: bool = Query(False),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Events linked to an Entity."""
    graph = stores["graph"]
    content_store = stores["content"]

    # Entity info
    entity = graph.get_entity_by_id(entity_id)
    if not entity:
        return {"error": f"Entity not found: {entity_id}"}

    events, total = graph.get_entity_events(
        entity_id=entity_id,
        include_deleted=include_deleted,
        page=page,
        size=size,
    )

    # Add summary to each event
    for ev in events:
        vid = ev.get("vector_id", "")
        if vid:
            content_record = content_store.get_by_vector_id(vid)
            if content_record:
                ev["summary"] = content_record.inline_meta.get("event_summary", "")
            else:
                ev["summary"] = ""
        else:
            ev["summary"] = ""

    return sanitize({
        "entity": {"id": entity.get("id"), "name": entity.get("name")},
        "events": events,
        "total": total,
    })


@router.get("/tags/{tag_id}/events")
def get_tag_events(
    tag_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    include_deleted: bool = Query(False),
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    """Events participated in by Entities belonging to a Tag (internal: groups table)."""
    graph = stores["graph"]
    content_store = stores["content"]

    events, total = graph.get_group_events(
        group_id=tag_id,
        include_deleted=include_deleted,
        page=page,
        size=size,
    )

    # Add summary to each event
    for ev in events:
        vid = ev.get("vector_id", "")
        if vid:
            content_record = content_store.get_by_vector_id(vid)
            if content_record:
                ev["summary"] = content_record.inline_meta.get("event_summary", "")
            else:
                ev["summary"] = ""
        else:
            ev["summary"] = ""

    return sanitize({
        "tag_id": tag_id,
        "events": events,
        "total": total,
    })
