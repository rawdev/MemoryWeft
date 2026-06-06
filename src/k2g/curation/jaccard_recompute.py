"""Re-run BP-82 jaccard for events touched by an entity merge (BP-87).

`db.graph.compute_jaccard_for_event(domain, event_id, ...)` is idempotent
(deletes existing event_jaccard_connected rows for that event_id, then
re-inserts). We loop over each event the canonical now participates in.

Scope: only events directly participated_in by the canonical (which, post-
merge, includes all alias events). The wider degree-cap behavior is
preserved by the underlying BP-82 implementation.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.db_store import DbStore

logger = logging.getLogger(__name__)


def recompute_jaccard_for_entity(
    canonical_id: str,
    db: "DbStore",
    *,
    page_size: int = 500,
) -> int:
    """Return the number of events whose jaccard was recomputed."""
    if not hasattr(db.graph, "compute_jaccard_for_event"):
        logger.warning(
            "recompute_jaccard_for_entity: graph backend lacks compute_jaccard_for_event"
        )
        return 0

    total_seen = 0
    page = 1
    recomputed = 0
    while True:
        rows, total = db.graph.get_entity_events(
            canonical_id,
            include_deleted=False,
            page=page,
            size=page_size,
        )
        if not rows:
            break
        for ev in rows:
            domain = ev.get("domain")
            event_id = ev.get("id")
            if not domain or not event_id:
                continue
            try:
                db.graph.compute_jaccard_for_event(domain, event_id)
                recomputed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "recompute_jaccard_for_entity: event=%s failed: %s",
                    event_id, exc,
                )
        total_seen += len(rows)
        if total_seen >= total:
            break
        page += 1

    logger.info(
        "recompute_jaccard_for_entity: canonical=%s recomputed=%d events",
        canonical_id, recomputed,
    )
    return recomputed
