"""Entity merge orchestrator (BP-87 §11).

Replace + audit pattern. New schema: 0 — uses existing
`entities.deprecated`, `k2g_entity_alias`, and the BP-87
`replace_entity_in_links` graph helper.

Transaction layout:
  - Body (link replace / deprecated mark / alias insert) inside one
    `db.session()` — atomic.
  - Vector / jaccard recompute outside the session — a recompute failure
    must not roll back the merge. Audit row in `k2g_entity_alias` is the
    source of truth.

Chain rule:
  - If `canonical` is itself an alias of some `terminal`, resolve forward
    and target `terminal` instead.
  - After merge, any row where `alias` was a canonical (entity_id_b =
    alias) is rewritten so it points to the new canonical.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.db_store import DbStore

logger = logging.getLogger(__name__)


def _is_postgres(conn) -> bool:
    # psycopg2 conn module is ``psycopg2.extensions`` (no "postgres" substring) —
    # detect the driver. See ``k2g.web.routes._sql._is_pg`` for the canonical form.
    return "psycopg2" in type(conn).__module__.lower()


def _ph(conn) -> str:
    """Param placeholder for the active backend ('%s' pg / '?' sqlite)."""
    return "%s" if _is_postgres(conn) else "?"


def _exec(conn, sql: str, params: tuple = ()):
    """backend-agnostic execute — psycopg2 conn has no ``.execute`` (sqlite3 does)
    and its default RealDictCursor breaks ``r[0]`` index access. PG uses DictCursor
    (index+key). Placeholders are already chosen via ``_ph``/explicit branches, so
    no ``?``→``%s`` rewrite is needed here. Returns the cursor."""
    if _is_postgres(conn):
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    else:
        cur = conn.cursor()
    cur.execute(sql, params)
    return cur


@dataclass(frozen=True)
class MergeResult:
    alias_id: str
    canonical_id: str
    final_canonical_id: str  # after chain resolution; usually == canonical_id
    participated_in_moved: int
    participated_in_dedup: int
    connections_moved: int
    connections_merged: int
    vector_recomputed: bool
    jaccard_events_recomputed: int
    audit_inserted: bool


def _resolve_canonical(db: "DbStore", entity_id: str, *, depth: int = 0) -> str:
    """Follow k2g_entity_alias chain forward to the terminal canonical.

    `entity_id_a` = alias, `entity_id_b` = canonical, relation='same'.
    Returns the terminal id (no further chain). Depth-limited to avoid
    cycles (shouldn't happen but defensive).
    """
    if depth > 20:
        logger.warning("alias chain too long for %s; stopping resolve", entity_id)
        return entity_id
    conn = db.graph._conn  # type: ignore[attr-defined]
    ph = _ph(conn)
    cur = _exec(conn, 
        f"SELECT entity_id_b FROM k2g_entity_alias "
        f"WHERE entity_id_a = {ph} AND relation = 'same' LIMIT 1",
        (entity_id,),
    )
    row = cur.fetchone()
    if row is None:
        return entity_id
    next_id = row[0]
    if not next_id or next_id == entity_id:
        return entity_id
    return _resolve_canonical(db, next_id, depth=depth + 1)


def _rewrite_alias_chain(db: "DbStore", old_canonical: str, new_canonical: str) -> int:
    """Any row that points to `old_canonical` as canonical should now point
    to `new_canonical` (we just merged `old_canonical` into it).
    """
    conn = db.graph._conn  # type: ignore[attr-defined]
    ph = _ph(conn)
    cur = _exec(conn, 
        f"UPDATE k2g_entity_alias SET entity_id_b = {ph} "
        f"WHERE entity_id_b = {ph} AND relation = 'same'",
        (new_canonical, old_canonical),
    )
    return int(cur.rowcount or 0)


def merge_entities(
    alias_id: str,
    canonical_id: str,
    db: "DbStore",
    *,
    asserted_by: str = "viewer",
    confidence: float = 1.0,
    recompute: bool = True,
) -> MergeResult:
    """Merge `alias_id` into `canonical_id` (BP-87 §11.2).

    Raises:
        ValueError — if ids match, alias missing, canonical missing, or
        canonical itself was previously deprecated.
    """
    if alias_id == canonical_id:
        raise ValueError("alias and canonical must differ")

    alias_row = db.graph.get_entity_by_id(alias_id)
    if alias_row is None:
        raise ValueError(f"alias not found: {alias_id}")
    canonical_row = db.graph.get_entity_by_id(canonical_id)
    if canonical_row is None:
        raise ValueError(f"canonical not found: {canonical_id}")

    # Resolve forward chain — if canonical is itself an alias, retarget to
    # the terminal entity. This lets users target intermediates (e.g.
    # newly-merged aliases) safely.
    final_canonical = _resolve_canonical(db, canonical_id)
    if final_canonical != canonical_id:
        logger.info(
            "merge_entities: canonical %s redirected to terminal %s",
            canonical_id, final_canonical,
        )
    if alias_id == final_canonical:
        raise ValueError(
            f"alias {alias_id} resolves to itself via chain — refusing"
        )

    # Only the *terminal* canonical must be non-deprecated. An intermediate
    # (already-merged) entity is allowed as input and is transparently
    # resolved above.
    terminal_row = (
        canonical_row if final_canonical == canonical_id
        else db.graph.get_entity_by_id(final_canonical)
    )
    if terminal_row is None:
        raise ValueError(f"terminal canonical {final_canonical} not found")
    if terminal_row.get("deprecated"):
        raise ValueError(
            f"terminal canonical {final_canonical} is deprecated — refusing"
        )

    counts: dict[str, int] = {}
    audit_inserted = False
    with db.session():
        counts = db.graph.replace_entity_in_links(alias_id, final_canonical)
        conn = db.graph._conn  # type: ignore[attr-defined]
        ph = _ph(conn)
        true_lit = "TRUE" if _is_postgres(conn) else "1"
        # Mark alias as deprecated (soft-delete, not physical).
        _exec(conn, 
            f"UPDATE entities SET deprecated = {true_lit} WHERE id = {ph}",
            (alias_id,),
        )
        # Audit row (idempotent upsert on PK (entity_id_a, entity_id_b)).
        now = datetime.now(timezone.utc).isoformat()
        if _is_postgres(conn):
            alias_sql = (
                "INSERT INTO k2g_entity_alias "
                "(entity_id_a, entity_id_b, relation, confidence, asserted_by, asserted_at) "
                "VALUES (%s, %s, 'same', %s, %s, %s) "
                "ON CONFLICT (entity_id_a, entity_id_b) DO UPDATE SET "
                "relation = EXCLUDED.relation, confidence = EXCLUDED.confidence, "
                "asserted_by = EXCLUDED.asserted_by, asserted_at = EXCLUDED.asserted_at"
            )
        else:
            alias_sql = (
                "INSERT OR REPLACE INTO k2g_entity_alias "
                "(entity_id_a, entity_id_b, relation, confidence, asserted_by, asserted_at) "
                "VALUES (?, ?, 'same', ?, ?, ?)"
            )
        _exec(conn, 
            alias_sql,
            (alias_id, final_canonical, float(confidence), asserted_by, now),
        )
        audit_inserted = True
        # Chain rewrite — any old row claiming alias was a canonical of X
        # should now claim final_canonical.
        _rewrite_alias_chain(db, alias_id, final_canonical)

    # Vector backend: drop alias's cached vector (canonical owns it now).
    if hasattr(db.vector, "delete_entity_vector"):
        try:
            db.vector.delete_entity_vector(alias_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("merge_entities: delete_entity_vector(%s) failed: %s",
                           alias_id, exc)

    vector_ok = False
    jaccard_n = 0
    if recompute:
        from k2g.curation.jaccard_recompute import recompute_jaccard_for_entity
        from k2g.curation.vector_recompute import recompute_entity_vector

        try:
            vector_ok = recompute_entity_vector(final_canonical, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("merge_entities: vector recompute failed: %s", exc)
        try:
            jaccard_n = recompute_jaccard_for_entity(final_canonical, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("merge_entities: jaccard recompute failed: %s", exc)

    return MergeResult(
        alias_id=alias_id,
        canonical_id=canonical_id,
        final_canonical_id=final_canonical,
        participated_in_moved=int(counts.get("participated_in_moved", 0)),
        participated_in_dedup=int(counts.get("participated_in_dedup", 0)),
        connections_moved=int(counts.get("connections_moved", 0)),
        connections_merged=int(counts.get("connections_merged", 0)),
        vector_recomputed=vector_ok,
        jaccard_events_recomputed=jaccard_n,
        audit_inserted=audit_inserted,
    )


def unmerge_entity(alias_id: str, db: "DbStore") -> bool:
    """Reactivate a previously deprecated alias entity (BP-87 §11.6).

    *Link-level restoration is impossible* — once participated_in /
    entity_connection rows were rewritten to the canonical, we cannot tell
    which were originally the alias's. This call only:
      - flips entities.deprecated back to 0
      - removes the k2g_entity_alias audit row

    Use it sparingly. The "right" tool for split is BP-92 (entity_split).
    """
    conn = db.graph._conn  # type: ignore[attr-defined]
    ph = _ph(conn)
    false_lit = "FALSE" if _is_postgres(conn) else "0"
    cur = _exec(conn, 
        f"SELECT entity_id_b FROM k2g_entity_alias "
        f"WHERE entity_id_a = {ph} AND relation = 'same' LIMIT 1",
        (alias_id,),
    )
    row = cur.fetchone()
    if row is None:
        logger.info("unmerge_entity: no alias record for %s", alias_id)
        return False

    with db.session():
        _exec(conn, 
            f"UPDATE entities SET deprecated = {false_lit} WHERE id = {ph}",
            (alias_id,),
        )
        _exec(conn, 
            f"DELETE FROM k2g_entity_alias "
            f"WHERE entity_id_a = {ph} AND relation = 'same'",
            (alias_id,),
        )
    logger.info("unmerge_entity: %s reactivated", alias_id)
    return True
