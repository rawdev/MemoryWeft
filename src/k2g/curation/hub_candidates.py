"""BP-90 — hub candidate signals + stopword toggle.

Measurement: no single graph metric cleanly separates a
true background hub (a frequent author / an LLM) from a frequent-but-topical entity
(Neo4j/pgvector) — neighbor-community entropy, type, and normalized category
scatter all failed. So this module does NOT auto-decide. It surfaces the
human-readable signals (DF% + #area + type) for a person to review; the final
exclusion is the human's, persisted as ``entities.user_tag='stopword'`` and
applied at community graph-build time (see leiden_community.build_entity_graph).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.db_store import DbStore


@dataclass
class HubCandidate:
    entity_id: str
    name: str
    type: str | None
    event_count: int
    df_pct: float        # event_count / total events (scale-invariant)
    area_count: int      # distinct root group-areas the entity's events touch
    is_stopword: bool


def _exec(conn, sql: str, params: tuple = ()):
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


def _group_root_map(conn) -> dict[str, str]:
    """group_id -> top-level root group id (follow parent_id chain)."""
    parent = dict(_exec(conn, "SELECT id, parent_id FROM groups").fetchall())
    cache: dict[str, str] = {}

    def root(g: str) -> str:
        if g in cache:
            return cache[g]
        seen: list[str] = []
        cur = g
        while cur is not None and parent.get(cur) is not None and cur not in seen:
            seen.append(cur)
            cur = parent.get(cur)
        for s in seen:
            cache[s] = cur
        cache[g] = cur
        return cur

    return {g: root(g) for g in parent}


def compute_hub_candidates(
    db: "DbStore",
    domain: str | None = None,
    *,
    top_n: int = 50,
) -> list[HubCandidate]:
    """Top-N entities by event count with hub signals. No auto-exclusion —
    data for human review only."""
    conn = db.graph._conn  # type: ignore[attr-defined]

    if domain:
        total = _exec(
            conn, "SELECT COUNT(*) FROM events WHERE domain = ?", (domain,)
        ).fetchone()[0]
        rows = _exec(
            conn,
            "SELECT e.id, e.name, e.type, e.user_tag, COUNT(*) AS ev "
            "FROM participated_in p JOIN entities e ON e.id = p.entity_id "
            "WHERE e.domain = ? GROUP BY e.id ORDER BY ev DESC LIMIT ?",
            (domain, int(top_n)),
        ).fetchall()
    else:
        total = _exec(conn, "SELECT COUNT(*) FROM events").fetchone()[0]
        rows = _exec(
            conn,
            "SELECT e.id, e.name, e.type, e.user_tag, COUNT(*) AS ev "
            "FROM participated_in p JOIN entities e ON e.id = p.entity_id "
            "GROUP BY e.id ORDER BY ev DESC LIMIT ?",
            (int(top_n),),
        ).fetchall()
    total = int(total or 0)
    cand_ids = [r[0] for r in rows]

    area: dict[str, set] = {eid: set() for eid in cand_ids}
    if cand_ids:
        root_map = _group_root_map(conn)
        placeholders = ",".join(["?"] * len(cand_ids))
        for eid, gid in _exec(
            conn,
            f"SELECT p.entity_id, m.group_id FROM participated_in p "
            f"JOIN event_member_of m ON m.event_id = p.event_id "
            f"WHERE p.entity_id IN ({placeholders})",
            cand_ids,
        ).fetchall():
            area[eid].add(root_map.get(gid, gid))

    out: list[HubCandidate] = []
    for eid, name, typ, user_tag, ev in rows:
        ev = int(ev)
        out.append(
            HubCandidate(
                entity_id=eid,
                name=name,
                type=typ,
                event_count=ev,
                df_pct=round(100.0 * ev / total, 2) if total else 0.0,
                area_count=len(area.get(eid, ())),
                is_stopword=(user_tag == "stopword"),
            )
        )
    return out


def set_entity_stopword(db: "DbStore", entity_id: str, on: bool) -> None:
    """Mark/unmark an entity as a community stopword (analysis exclusion only;
    search/dossier unaffected). Reuses the generic user_tag setter."""
    db.graph.set_user_tag("entity", entity_id, "stopword" if on else None)
