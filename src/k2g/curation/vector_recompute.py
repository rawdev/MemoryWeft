"""Wrap BP-81 entity vector recompute for a single canonical entity (BP-87).

After a merge, the canonical entity's participating events have changed
(it inherited the alias's events). Re-run mean+L2norm centroid via
`ProjectionEngine.compute_entity_centroid` with `use_cache=False` so the
cached `ref_event_count` doesn't short-circuit the recompute.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.db_store import DbStore

logger = logging.getLogger(__name__)


def recompute_entity_vector(
    entity_id: str,
    db: "DbStore",
) -> bool:
    """Return True if a vector was computed and upserted; False if skipped.

    Skip reasons (return False):
      - vector backend lacks entity-vector CRUD (legacy backend)
      - entity has no events
      - degenerate centroid (zero norm)
    """
    if not hasattr(db.vector, "upsert_entity_vector"):
        logger.warning(
            "recompute_entity_vector: vector backend %s lacks entity-vector CRUD",
            type(db.vector).__name__,
        )
        return False

    from k2g.trainer.projection import ProjectionEngine

    engine = ProjectionEngine(
        graph_store=db.graph,
        vector_store=db.vector,
        content_store=None,
        object_storage=None,
        llm_client=None,
        embedding_client=None,
    )
    centroid = engine.compute_entity_centroid(entity_id=entity_id, use_cache=False)
    ok = centroid is not None
    logger.info(
        "recompute_entity_vector: entity_id=%s ok=%s",
        entity_id, ok,
    )
    return ok
