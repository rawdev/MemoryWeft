"""Leidenalg community detection engine (entity + event).

Two graphs, both clustered with ``leidenalg`` (igraph, C). networkx Louvain
is NOT used here: it hangs on float (jaccard) edge weights (488K edges ~35min
measured); leidenalg is sub-second.

  - **entity**: ``entity_connection`` (a_id,b_id,event_count), weight =
    jaccard-normalized ``count / (deg_a + deg_b - count)``. Entities with
    ``user_tag='stopword'`` are excluded (human-curated hub removal).
  - **event**: ``event_jaccard_connected`` (a_id,b_id,entity_jaccard),
    weight = ``entity_jaccard``.

Data is read via ``db.graph._conn`` (sqlite all-in-one path, mirroring
``scripts/train_louvain.py``). Assignment write-back uses the backend-agnostic
``upsert_*_community_assignments`` graph methods (in the caller / script).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from k2g.db_store import DbStore

Target = Literal["entity", "event"]


@dataclass
class CommunityResult:
    target: str
    assignments: list[tuple[str, int]]  # (node_id, community_id)
    num_communities: int
    modularity: float
    node_count: int
    edge_count: int
    top_sizes: list[int]


def _rows(conn, sql: str, params: tuple = ()) -> list:
    """backend-agnostic fetchall — psycopg2 conn has no ``.execute`` (sqlite3 does)
    and its default RealDictCursor breaks ``r[0]`` index access.

    - SQL is written with ``?`` placeholders → rewritten to ``%s`` on Postgres.
    - Postgres uses ``DictCursor`` (index *and* key access; mirrors
      ``k2g.web.routes._sql.cursor``) so positional unpacking keeps working.
    psycopg2 conn module is ``psycopg2.extensions`` (no "postgres" substring) so we
    detect the driver, not the module name.
    """
    if "psycopg2" in type(conn).__module__.lower():
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql.replace("?", "%s"), params)
    else:
        cur = conn.cursor()
        cur.execute(sql, params)
    return cur.fetchall()


def _entity_totals(conn) -> dict[str, int]:
    """Per-entity DB-wide event count (jaccard denominator)."""
    return {
        r[0]: int(r[1])
        for r in _rows(
            conn,
            "SELECT entity_id, COUNT(*) FROM participated_in GROUP BY entity_id",
        )
    }


def _stopword_ids(conn) -> set[str]:
    return {
        r[0] for r in _rows(
            conn, "SELECT id FROM entities WHERE user_tag = 'stopword'",
        )
    }


def _scope_event_ids(conn, scope_group_id: str) -> set[str]:
    """Scope=tag boundary: events belonging to this group (+ descendant groups).

    Forced project tags are usually flat (level 0), but hierarchical tags are
    supported via RECURSIVE to collect event_member_of across the subtree.
    """
    rows = _rows(
        conn,
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM groups WHERE id = ?
            UNION ALL
            SELECT g.id FROM groups g JOIN sub s ON g.parent_id = s.id
        )
        SELECT DISTINCT em.event_id FROM event_member_of em
        WHERE em.group_id IN (SELECT id FROM sub)
        """,
        (scope_group_id,),
    )
    return {r[0] for r in rows}


def _scope_entity_ids(conn, scope_group_id: str) -> set[str]:
    """Entities within the scope=tag boundary = entities participating in
    the boundary events (2-hop derivation)."""
    rows = _rows(
        conn,
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM groups WHERE id = ?
            UNION ALL
            SELECT g.id FROM groups g JOIN sub s ON g.parent_id = s.id
        )
        SELECT DISTINCT pi.entity_id FROM participated_in pi
        WHERE pi.event_id IN (
            SELECT em.event_id FROM event_member_of em
            WHERE em.group_id IN (SELECT id FROM sub)
        )
        """,
        (scope_group_id,),
    )
    return {r[0] for r in rows}


def build_entity_graph(
    db: "DbStore",
    domain: str | None = None,
    *,
    exclude_stopwords: bool = True,
    scope_ids: set[str] | None = None,
) -> tuple[Any, dict[int, str]]:
    """entity_connection → igraph with jaccard-normalized weights.

    Returns ``(igraph.Graph, {vertex_index: entity_id})``. Stopword entities
    and their edges are dropped. Isolated entities (no surviving edge) are
    absent from the graph (no community assigned).

    When ``scope_ids`` is provided, only edges whose both endpoints belong
    to that set (scope boundary) are used (subset community). The jaccard
    denominator (totals) is kept **global** — preserving each node's
    global importance within the boundary (no weight distortion from
    subsetting).
    """
    import igraph as ig

    conn = db.graph._conn  # type: ignore[attr-defined]
    totals = _entity_totals(conn)
    stop = _stopword_ids(conn) if exclude_stopwords else set()

    if domain:
        rows = _rows(
            conn,
            "SELECT ec.a_id, ec.b_id, ec.event_count FROM entity_connection ec "
            "JOIN entities ea ON ea.id = ec.a_id "
            "JOIN entities eb ON eb.id = ec.b_id "
            "WHERE ea.domain = ? AND eb.domain = ?",
            (domain, domain),
        )
    else:
        rows = _rows(
            conn, "SELECT a_id, b_id, event_count FROM entity_connection",
        )

    idx: dict[str, int] = {}
    rev: dict[int, str] = {}

    def vid(e: str) -> int:
        i = idx.get(e)
        if i is None:
            i = len(idx)
            idx[e] = i
            rev[i] = e
        return i

    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for a, b, c in rows:
        if a in stop or b in stop:
            continue
        if scope_ids is not None and (a not in scope_ids or b not in scope_ids):
            continue
        c = int(c)
        ta, tb = totals.get(a, c), totals.get(b, c)
        union = ta + tb - c
        edges.append((vid(a), vid(b)))
        weights.append(c / union if union > 0 else 0.0)

    g = ig.Graph(n=len(idx), edges=edges)
    g.es["w"] = weights
    return g, rev


def build_event_graph(
    db: "DbStore",
    domain: str | None = None,
    *,
    scope_ids: set[str] | None = None,
    theta_e: float = 0.4,
) -> tuple[Any, dict[int, str]]:
    """event_jaccard_connected → igraph with entity_jaccard weights.

    Storage is unfiltered (all entity_jaccard > 0); theta_e is a
    **read-time parameter** (like leiden resolution). Only edges with
    ``entity_jaccard >= theta_e`` are included in the graph. Default 0.4
    preserves the canonical behavior for dense code/document domains.
    Sparse conversational memory benefits from a lower theta_e (e.g. 0.05)
    to retain content-based edges for scoped exploration
    (theta_e down -> more edges, lower modularity).

    When ``scope_ids`` is provided, only edges whose both endpoints are
    scope-boundary events are used.
    """
    import igraph as ig

    conn = db.graph._conn  # type: ignore[attr-defined]
    if domain:
        rows = _rows(
            conn,
            "SELECT ejc.a_id, ejc.b_id, ejc.entity_jaccard "
            "FROM event_jaccard_connected ejc "
            "JOIN events ea ON ea.id = ejc.a_id "
            "JOIN events eb ON eb.id = ejc.b_id "
            "WHERE ea.domain = ? AND eb.domain = ? AND ejc.entity_jaccard >= ?",
            (domain, domain, theta_e),
        )
    else:
        rows = _rows(
            conn,
            "SELECT a_id, b_id, entity_jaccard FROM event_jaccard_connected "
            "WHERE entity_jaccard >= ?",
            (theta_e,),
        )

    idx: dict[str, int] = {}
    rev: dict[int, str] = {}

    def vid(e: str) -> int:
        i = idx.get(e)
        if i is None:
            i = len(idx)
            idx[e] = i
            rev[i] = e
        return i

    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for a, b, j in rows:
        if scope_ids is not None and (a not in scope_ids or b not in scope_ids):
            continue
        edges.append((vid(a), vid(b)))
        weights.append(float(j or 0.0))

    g = ig.Graph(n=len(idx), edges=edges)
    g.es["w"] = weights
    return g, rev


def run_leiden(g: Any, *, seed: int = 42, resolution: float = 1.0):
    """leidenalg partition. RBConfigurationVertexPartition at resolution=1.0
    is equivalent to modularity, and exposes a resolution knob."""
    import leidenalg as la

    return la.find_partition(
        g,
        la.RBConfigurationVertexPartition,
        weights="w",
        seed=seed,
        resolution_parameter=resolution,
    )


def detect(
    db: "DbStore",
    target: Target,
    *,
    domain: str | None = None,
    seed: int = 42,
    resolution: float = 1.0,
    exclude_stopwords: bool = True,
    scope_group_id: str | None = None,
    scope_ids: set[str] | None = None,
    theta_e: float = 0.4,
) -> CommunityResult:
    """Build the graph for ``target`` and run leiden. Returns CommunityResult
    with ``assignments`` ready for upsert_*_community_assignments.

    Scope (subset boundary) can be specified two ways:
    - ``scope_group_id`` — tag (+subtree) boundary; member id set is
      resolved internally.
    - ``scope_ids`` — pre-computed node-id set (e.g. re-partitioning an
      existing community's members at higher resolution = drill-down).
      Takes precedence over ``scope_group_id`` when both are given.
    Scope is a graph *boundary* (subset filter), not an edge filter —
    entity jaccard weights are preserved as-is to avoid collapse/cycles.
    """
    conn = db.graph._conn  # type: ignore[attr-defined]
    if target == "entity":
        if scope_ids is None and scope_group_id:
            scope_ids = _scope_entity_ids(conn, scope_group_id)
        g, rev = build_entity_graph(
            db, domain, exclude_stopwords=exclude_stopwords, scope_ids=scope_ids,
        )
    elif target == "event":
        if scope_ids is None and scope_group_id:
            scope_ids = _scope_event_ids(conn, scope_group_id)
        g, rev = build_event_graph(db, domain, scope_ids=scope_ids, theta_e=theta_e)
    else:
        raise ValueError(f"unknown target: {target!r}")

    if g.vcount() == 0:
        return CommunityResult(target, [], 0, 0.0, 0, 0, [])

    part = run_leiden(g, seed=seed, resolution=resolution)
    assignments = [(rev[v], int(cid)) for v, cid in enumerate(part.membership)]
    sizes = sorted(part.sizes(), reverse=True)
    return CommunityResult(
        target=target,
        assignments=assignments,
        num_communities=len(part),
        modularity=float(part.modularity),
        node_count=g.vcount(),
        edge_count=g.ecount(),
        top_sizes=sizes[:5],
    )
