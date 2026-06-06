"""GraphQueryService -- DbStore-based default QueryService implementation.

Current scope is name-based entity_lookup + thin search.  Vector search /
context detail become functional after db_store Tier 2 is complete; for
now safe defaults are returned.

The automatic ``deprecated=0`` filter has been removed from event
responses.  When an optional ``BuildManifestStore`` is injected,
(source_root, file_path, file_hash, ingested_at) evidence is exposed
alongside events.  ``influence_score`` is read directly from the events
table and also used as a sort key so consumers (UX / LLM) can judge
based on evidence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from k2g.reader.base import ContextDetail, EntityMatch, SearchHit, SearchMode

if TYPE_CHECKING:
    from k2g.db_store import DbStore
    from k2g.updater.manifest import BuildManifestStore

logger = logging.getLogger(__name__)


class GraphQueryService:
    """Read-only facade that depends only on `DbStore`.

    Manifest evidence integration:
    - When ``manifest`` is provided, event responses include
      (source_root, file_path, file_hash, ingested_at) as evidence.
    - When not injected the evidence fields are None -- safe for
      deployments where manifest and graph DB are separate.
    """

    def __init__(
        self,
        db: "DbStore",
        *,
        manifest: "BuildManifestStore | None" = None,
    ) -> None:
        self._db = db
        self._manifest = manifest

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        domain: str,
        mode: SearchMode = "hybrid",
        top_k: int = 10,
    ) -> list[SearchHit]:
        """Simple name-match entity search + (future) vector search.

        Current implementation: exact match on entity name against
        `query`.  Partial match / vector search is deferred to a
        follow-up PR (see legacy `mcp/tools.py` hybrid_search).
        """
        hits: list[SearchHit] = []
        if mode in ("hybrid", "entities") and query:
            # link_or_create_entity returns the existing id if present --
            # a direct SELECT is preferable but the backend protocol
            # currently has no name-based lookup.  A name/domain query
            # will be added in a follow-up migration.  For now, use
            # _conn for a direct SELECT (SQLite only).
            conn = getattr(self._db.graph, "_conn", None)
            if conn is not None:
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT id, name, domain, type FROM entities "
                        "WHERE domain = ? AND name = ? LIMIT ?",
                        (domain, query, top_k),
                    )
                    rows = cur.fetchall()
                except Exception as exc:
                    logger.warning("search: entity SELECT failed: %s", exc)
                    rows = []
                for r in rows:
                    # row is a tuple or dict; use tuple-based access.
                    eid = r[0] if not isinstance(r, dict) else r["id"]
                    ename = r[1] if not isinstance(r, dict) else r["name"]
                    edom = r[2] if not isinstance(r, dict) else r["domain"]
                    etype = r[3] if not isinstance(r, dict) else r["type"]
                    hits.append(
                        SearchHit(
                            kind="entity",
                            id=eid,
                            score=1.0,
                            domain=edom,
                            name_or_summary=ename,
                            metadata={"type": etype or ""},
                        )
                    )
        # event-side: deferred to a follow-up PR (placeholder).
        return hits[:top_k]

    # ------------------------------------------------------------------
    def entity_lookup(
        self, name: str, *, domain: str,
    ) -> list[EntityMatch]:
        """Look up entity by name+domain composite key + linked event summaries."""
        conn = getattr(self._db.graph, "_conn", None)
        if conn is None:
            return []
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id, name, domain, type, user_tag FROM entities "
                "WHERE domain = ? AND name = ? LIMIT 50",
                (domain, name),
            )
            rows = cur.fetchall()
        except Exception as exc:
            logger.warning("entity_lookup failed: %s", exc)
            return []

        results: list[EntityMatch] = []
        for r in rows:
            eid = r[0]
            entity_row = {
                "id": eid,
                "name": r[1],
                "domain": r[2],
                "type": r[3] or "",
                "user_tag": r[4] if len(r) > 4 else None,
            }
            # Up to 10 events this entity participated in.
            # No automatic deprecated=0 filter -- consumers judge
            # via evidence.  influence_score added + DESC sort as
            # primary key.
            try:
                cur.execute(
                    """
                    SELECT e.id, e.summary, e.timestamp, e.domain,
                           e.influence_score, e.deprecated
                      FROM events e
                      JOIN participated_in p ON p.event_id = e.id
                     WHERE p.entity_id = ?
                     ORDER BY e.influence_score DESC, e.order_index
                     LIMIT 10
                    """,
                    (eid,),
                )
                erows = cur.fetchall()
            except Exception as exc:
                logger.warning("entity_lookup event join failed: %s", exc)
                erows = []
            events = [
                self._build_event_payload(
                    event_id=er[0], summary=er[1], timestamp=er[2],
                    domain=er[3], influence_score=er[4], deprecated=er[5],
                )
                for er in erows
            ]
            results.append(
                EntityMatch(entity=entity_row, events=events, total=len(events))
            )
        return results

    # ------------------------------------------------------------------
    def _build_event_payload(
        self,
        *,
        event_id: str,
        summary: str | None,
        timestamp: str | None,
        domain: str,
        influence_score: float | None,
        deprecated: int | bool | None,
    ) -> dict[str, Any]:
        """Build an event response dict with 5 evidence fields.

        When manifest is injected, LEFT JOINs build_segment_manifest
        then build_file_manifest to populate (source_root, file_path,
        file_hash, ingested_at).  Fields are None when no match is
        found -- consumers recognise the lack of evidence.
        """
        payload: dict[str, Any] = {
            "id": event_id,
            "summary": summary,
            "timestamp": timestamp,
            "domain": domain,
            "influence_score": float(influence_score) if influence_score is not None else 1.0,
            "deprecated": bool(deprecated) if deprecated is not None else False,
            "source_root": None,
            "file_path": None,
            "file_hash": None,
            "ingested_at": None,
        }
        if self._manifest is None:
            return payload
        try:
            mconn = getattr(self._manifest, "_conn", None)
            if mconn is None:
                return payload
            cur = mconn.cursor()
            cur.execute(
                """
                SELECT mf.file_path, mf.file_hash, mf.built_at, mf.source_root
                  FROM build_segment_manifest s
                  JOIN build_file_manifest mf
                    ON mf.domain = s.domain
                   AND mf.file_path = s.file_path
                   AND mf.status = 'active'
                 WHERE s.event_id = ?
                 ORDER BY s.id DESC
                 LIMIT 1
                """,
                (event_id,),
            )
            row = cur.fetchone()
            if row is not None:
                payload["file_path"] = row["file_path"]
                payload["file_hash"] = row["file_hash"]
                payload["ingested_at"] = row["built_at"]
                payload["source_root"] = row["source_root"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("manifest evidence join failed event_id=%s: %s", event_id, exc)
        return payload

    # ------------------------------------------------------------------
    def context_detail(
        self, cg_id: str, *, depth: int = 2,
    ) -> ContextDetail | None:
        """ContextGroup detail lookup -- functional after db_store Tier 2."""
        logger.debug(
            "context_detail: db_store Tier 2 (context_groups) is still a "
            "stub, returning None (cg_id=%s).", cg_id,
        )
        return None


__all__ = ["GraphQueryService"]
