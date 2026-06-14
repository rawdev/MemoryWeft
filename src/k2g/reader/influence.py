"""EventInfluenceService -- set_influence + suggest_influence_review.

Following the principle that K2G never renders an automatic verdict,
the event weight ``influence_score`` is only changed by an explicit
human / LLM call.  This service exposes the call path
(set_influence) and a hint function (suggest_influence_review) that
surfaces candidates that *may* need review.

Design principles:
- ``set_influence`` changes the score and simultaneously records one
  ``events_audit`` row (audit trail).  Repeated calls on the same
  event accumulate audit entries.
- ``suggest_influence_review`` does not make decisions.  It groups
  and returns hints (cases where active and superseded coexist for
  the same file / segment_key).

When manifest is not injected, suggest returns an empty result --
the function is only meaningful when manifest is available in the
same environment.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from k2g.db_store import DbStore
    from k2g.updater.manifest import BuildManifestStore

logger = logging.getLogger(__name__)


def _ph(conn: Any) -> str:
    """SQL placeholder for the connection's backend — psycopg2 → ``%s``,
    sqlite3 → ``?``. Canonical detector (see ``web.routes._sql._is_pg``)."""
    return "%s" if "psycopg2" in type(conn).__module__.lower() else "?"


class EventInfluenceService:
    """Explicit manipulation of events.influence_score + review candidate hints."""

    def __init__(
        self,
        db: "DbStore",
        *,
        manifest: "BuildManifestStore | None" = None,
    ) -> None:
        self._db = db
        self._manifest = manifest

    # ------------------------------------------------------------------
    def set_influence(
        self, event_id: str, score: float, reason: str | None = None,
    ) -> dict[str, Any]:
        """Update events.influence_score and append an audit row.

        Returns: {"event_id", "score_before", "score_after", "reason"}.
        If event_id does not exist, ``found=False``.
        """
        conn = getattr(self._db.graph, "_conn", None)
        if conn is None:
            raise RuntimeError("graph store does not expose _conn -- check backend")
        ph = _ph(conn)
        cur = conn.cursor()
        cur.execute(
            f"SELECT influence_score FROM events WHERE id = {ph}", (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {
                "event_id": event_id, "found": False,
                "score_before": None, "score_after": None, "reason": reason,
            }
        # PG RealDictCursor → dict rows; SQLite → tuple/Row.
        sb = row["influence_score"] if hasattr(row, "keys") else row[0]
        score_before = float(sb) if sb is not None else 1.0
        score_after = float(score)
        cur.execute(
            f"UPDATE events SET influence_score = {ph} WHERE id = {ph}",
            (score_after, event_id),
        )
        cur.execute(
            f"INSERT INTO events_audit "
            f"(event_id, score_before, score_after, reason) "
            f"VALUES ({ph}, {ph}, {ph}, {ph})",
            (event_id, score_before, score_after, reason),
        )
        conn.commit()
        return {
            "event_id": event_id, "found": True,
            "score_before": score_before, "score_after": score_after,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    def suggest_influence_review(
        self, *, domain: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return event groups that may need review, with evidence hints.

        Candidate categories (manifest-based):
        - When both status='active' and 'superseded' rows exist for the
          same (domain, file_path, segment_key), surface them so the
          user / LLM can compare the two events' scores.

        Returns: list of {"domain", "file_path", "segment_key", "candidates":
        [{"event_id", "status", "influence_score", "file_hash", "built_at",
          "source_root"}]}.
        """
        if self._manifest is None:
            return []
        mconn = getattr(self._manifest, "_conn", None)
        if mconn is None:
            return []
        cur = mconn.cursor()
        mph = _ph(mconn)
        params: list[Any] = []
        sql = (
            "SELECT s.domain, s.file_path, s.segment_key, s.event_id, s.status, "
            "       mf.file_hash, mf.built_at, mf.source_root "
            "  FROM build_segment_manifest s "
            "  LEFT JOIN build_file_manifest mf "
            "    ON mf.domain = s.domain AND mf.file_path = s.file_path "
            "  WHERE s.event_id IS NOT NULL AND s.event_id != '' "
        )
        if domain:
            sql += f"    AND s.domain = {mph} "
            params.append(domain)
        sql += "  ORDER BY s.domain, s.file_path, s.segment_key, s.id "
        cur.execute(sql, params)
        rows = cur.fetchall()
        # group by (domain, file_path, segment_key)
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in rows:
            key = (r["domain"], r["file_path"], r["segment_key"])
            groups.setdefault(key, []).append({
                "event_id": r["event_id"],
                "status": r["status"],
                "file_hash": r["file_hash"],
                "ingested_at": r["built_at"],
                "source_root": r["source_root"],
            })

        # only keep groups with both active and superseded
        candidates: list[dict[str, Any]] = []
        for (dom, fpath, skey), members in groups.items():
            statuses = {m["status"] for m in members}
            if "active" in statuses and "superseded" in statuses:
                # enrich with influence_score
                gconn = getattr(self._db.graph, "_conn", None)
                if gconn is not None:
                    gph = _ph(gconn)
                    gcur = gconn.cursor()
                    for m in members:
                        try:
                            gcur.execute(
                                f"SELECT influence_score FROM events WHERE id = {gph}",
                                (m["event_id"],),
                            )
                            erow = gcur.fetchone()
                            ev = (
                                erow["influence_score"] if hasattr(erow, "keys")
                                else erow[0]
                            ) if erow is not None else None
                            m["influence_score"] = (
                                float(ev) if ev is not None else None
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "suggest_influence_review: events query failed %s: %s",
                                m["event_id"], exc,
                            )
                            m["influence_score"] = None
                candidates.append({
                    "domain": dom,
                    "file_path": fpath,
                    "segment_key": skey,
                    "candidates": members,
                })
                if len(candidates) >= limit:
                    break
        return candidates


__all__ = ["EventInfluenceService"]
