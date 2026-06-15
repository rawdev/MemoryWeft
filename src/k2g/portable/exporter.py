"""BP-74 — DomainExporter.

Streams a single domain (events / entities / groups + Tier-1 edges) from the
graph store's underlying connection into a `.mweft.tar.gz` archive.

Tier-2 (CG/ETG/Plan/Direction/alias/event_reference) and segments are added
in subsequent steps; the public entry point already accepts ArchiveOptions
toggles for forward compatibility.

Backend detection: callers pass an explicit ``backend`` parameter
(``"sqlite"`` or ``"postgres"``) — exporter does not introspect ``graph``.
"""

from __future__ import annotations

import getpass
import logging
import platform
from pathlib import Path
from typing import Any

from k2g.portable.archive_io import ArchiveWriter, DirWriter
from k2g.portable.manifest import (
    ArchiveManifest,
    ArchiveOptions,
    ArchiveSource,
    compute_source_hash,
    utc_now_iso,
)
from k2g.portable.schema import (
    CONTENT_TABLES,
    DERIVED_TABLES,
    SEGMENT_TABLES,
    TIER1_TABLES,
    TIER2_TABLES,
    dumps,
    paramstyle_placeholder,
    row_to_dict,
)

logger = logging.getLogger(__name__)


def _prefix_cols(alias: str, cols: tuple[str, ...]) -> str:
    """Return ``"alias.c1, alias.c2, ..."``.  Avoids JOIN ambiguity when the
    target table shares column names (e.g., ``created_at``) with its joins."""
    return ", ".join(f"{alias}.{c}" for c in cols)


# Edge SQL templates — edges have no `domain` column, so we JOIN on the
# referenced base tables and require *both* endpoints to share the domain.
# This guarantees no dangling FK on import.  ``{cols}`` is replaced with a
# fully alias-qualified column list.
_EDGE_SQL_TEMPLATES = {
    "participated_in": (
        "SELECT {cols} FROM participated_in pi "
        "JOIN entities e ON pi.entity_id = e.id "
        "JOIN events ev ON pi.event_id = ev.id "
        "WHERE e.domain = {p} AND ev.domain = {p}"
    ),
    "event_member_of": (
        "SELECT {cols} FROM event_member_of emo "
        "JOIN events ev ON emo.event_id = ev.id "
        "JOIN groups g ON emo.group_id = g.id "
        "WHERE ev.domain = {p} AND g.domain = {p}"
    ),
    "event_sequential_next": (
        "SELECT {cols} FROM event_sequential_next esn "
        "JOIN events ev1 ON esn.prev_id = ev1.id "
        "JOIN events ev2 ON esn.next_id = ev2.id "
        "WHERE ev1.domain = {p} AND ev2.domain = {p}"
    ),
    "entity_connection": (
        "SELECT {cols} FROM entity_connection ec "
        "JOIN entities ea ON ec.a_id = ea.id "
        "JOIN entities eb ON ec.b_id = eb.id "
        "WHERE ea.domain = {p} AND eb.domain = {p}"
    ),
}

_EDGE_ALIAS = {
    "participated_in": "pi",
    "event_member_of": "emo",
    "event_sequential_next": "esn",
    "entity_connection": "ec",
    "k2g_entity_alias": "kea",
    "k2g_event_reference": "ker",
}

# Tier-2 edge templates — both endpoints must share the export domain.
_EDGE_SQL_TEMPLATES["k2g_entity_alias"] = (
    "SELECT {cols} FROM k2g_entity_alias kea "
    "JOIN entities ea ON kea.entity_id_a = ea.id "
    "JOIN entities eb ON kea.entity_id_b = eb.id "
    "WHERE ea.domain = {p} AND eb.domain = {p}"
)
_EDGE_SQL_TEMPLATES["k2g_event_reference"] = (
    "SELECT {cols} FROM k2g_event_reference ker "
    "JOIN events ea ON ker.event_id_a = ea.id "
    "JOIN events eb ON ker.event_id_b = eb.id "
    "WHERE ea.domain = {p} AND eb.domain = {p}"
)


# Static plans for the export loop.  Tuples: (table, archive_member).
_BASE_DIRECT_EXPORTS: tuple[tuple[str, str], ...] = (
    ("entities", "db/entities.jsonl"),
    ("events", "db/events.jsonl"),
    ("groups", "db/groups.jsonl"),
    ("context_groups", "db/context_groups.jsonl"),
    ("event_template_groups", "db/event_template_groups.jsonl"),
)

_EDGE_EXPORTS: tuple[tuple[str, str], ...] = (
    ("participated_in", "db/edges/participated_in.jsonl"),
    ("event_member_of", "db/edges/event_member_of.jsonl"),
    ("event_sequential_next", "db/edges/event_sequential_next.jsonl"),
    ("entity_connection", "db/edges/entity_connection.jsonl"),
    ("k2g_entity_alias", "db/edges/k2g_entity_alias.jsonl"),
    ("k2g_event_reference", "db/edges/k2g_event_reference.jsonl"),
)

_PLAN_EXPORTS: tuple[tuple[str, str], ...] = (
    ("plan_nodes", "db/plan/plan_nodes.jsonl"),
    ("plan_direction_nodes", "db/plan/plan_direction_nodes.jsonl"),
)


def _all_table_cols() -> dict[str, tuple[str, ...]]:
    return {
        **TIER1_TABLES, **TIER2_TABLES, **DERIVED_TABLES,
        **SEGMENT_TABLES, **CONTENT_TABLES,
    }


# Derived exports — (table, member, alias|None, FROM/WHERE template, n_params).
# alias=None → direct ``WHERE domain`` (columns unprefixed); otherwise the
# columns are alias-qualified and filtered through a JOIN to entities/events so
# only in-domain rows are carried. ``{p}`` = the backend placeholder.
_DERIVED_EXPORTS: tuple[tuple[str, str, str | None, str, int], ...] = (
    # train_run = domain-matching runs ∪ runs referenced by the domain's
    # community assignments. The assignments are filtered by entity/event
    # domain, so a run that produced them but carries a different / NULL
    # train_run.domain would otherwise be left out — breaking the
    # *_community_assignment.run_id FK on import.
    ("train_run", "db/derived/train_run.jsonl", None,
     "FROM train_run WHERE domain = {p} OR id IN ("
     "SELECT run_id FROM entity_community_assignment a "
     "JOIN entities e ON e.id = a.entity_id WHERE e.domain = {p} "
     "UNION "
     "SELECT run_id FROM event_community_assignment a "
     "JOIN events e ON e.id = a.event_id WHERE e.domain = {p})", 3),
    ("entity_embedding_meta", "db/derived/entity_embedding_meta.jsonl", "m",
     "FROM entity_embedding_meta m JOIN entities e ON e.id = m.entity_id "
     "WHERE e.domain = {p}", 1),
    ("event_jaccard_connected", "db/edges/event_jaccard_connected.jsonl", "ej",
     "FROM event_jaccard_connected ej "
     "JOIN events ea ON ea.id = ej.a_id JOIN events eb ON eb.id = ej.b_id "
     "WHERE ea.domain = {p} AND eb.domain = {p}", 2),
    ("event_belongs_to_context", "db/edges/event_belongs_to_context.jsonl", "x",
     "FROM event_belongs_to_context x JOIN events e ON e.id = x.event_id "
     "WHERE e.domain = {p}", 1),
    ("entity_community_assignment",
     "db/derived/entity_community_assignment.jsonl", "a",
     "FROM entity_community_assignment a JOIN entities e ON e.id = a.entity_id "
     "WHERE e.domain = {p}", 1),
    ("event_community_assignment",
     "db/derived/event_community_assignment.jsonl", "a",
     "FROM event_community_assignment a JOIN events e ON e.id = a.event_id "
     "WHERE e.domain = {p}", 1),
    ("cluster_narrative_cache", "db/derived/cluster_narrative_cache.jsonl", None,
     "FROM cluster_narrative_cache WHERE domain = {p}", 1),
)

# Plan edges — ride options.include_plan (same JOIN-to-events filter).
_PLAN_EDGE_EXPORTS: tuple[tuple[str, str, str | None, str, int], ...] = (
    ("plan_from", "db/plan/plan_from.jsonl", "x",
     "FROM plan_from x JOIN events e ON e.id = x.event_id WHERE e.domain = {p}", 1),
    ("realized_as", "db/plan/realized_as.jsonl", "x",
     "FROM realized_as x JOIN events e ON e.id = x.event_id WHERE e.domain = {p}", 1),
)


def _fetch_all(
    conn: Any,
    sql: str,
    params: tuple,
    columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Execute and materialize all rows.  Caller bounds the size by the
    domain selection — large domains are addressed in §10 follow-up
    (chunked spool)."""
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [row_to_dict(r, columns) for r in rows]
    finally:
        try:
            cur.close()
        except Exception:
            pass


class DomainExporter:
    """Export a single domain to a `.mweft.tar.gz` archive."""

    def __init__(
        self,
        graph: Any,
        backend: str,
        *,
        group: str = "default",
        project: str | None = None,
        k2g_version: str = "0.1.0",
        content_store: Any = None,
    ) -> None:
        self._graph = graph
        self._conn = graph._conn
        self._backend = backend
        self._p = paramstyle_placeholder(backend)
        self._group = group
        self._project = project or group
        self._k2g_version = k2g_version
        # BP-86 — content store (separate DB connection).  None ⇒ skip the
        # content export step (keeps pre-BP-86 callers / HTTP route working).
        self._content_store = content_store
        # Resolved final path of the most recent export() call.  Used by the
        # Phase B HTTP route to report back when the caller passed a directory.
        self.last_archive_path: Path | None = None

    def export(
        self,
        domain: str,
        archive_path: str | Path,
        *,
        options: ArchiveOptions | None = None,
        embedding: dict | None = None,
        archive_format: str = "archive",
    ) -> ArchiveManifest:
        """Write the archive and return the manifest that was embedded.

        ``archive_format`` — ``"archive"`` writes a single ``.mweft.tar.gz``;
        ``"dir"`` writes an unpacked folder of JSONL files rooted at
        ``archive_path`` (human-readable / diffable).
        """
        if archive_format not in ("archive", "dir"):
            raise ValueError(
                f"archive_format must be 'archive' or 'dir', got "
                f"{archive_format!r}"
            )
        opts = options or ArchiveOptions()
        path = Path(archive_path)
        if archive_format == "dir":
            writer_cm: ArchiveWriter | DirWriter = DirWriter(path)
        else:
            if path.is_dir():
                path = path / self.suggested_archive_name(domain)
            writer_cm = ArchiveWriter(path)

        row_counts: dict[str, int] = {}
        event_ids: list[str] = []
        entity_ids: list[str] = []
        all_cols = _all_table_cols()

        with writer_cm as writer:
            # --- Direct-domain base tables (Tier-1 + Tier-2) -----------
            for table, member in _BASE_DIRECT_EXPORTS:
                cols = all_cols[table]
                sql = (
                    f"SELECT {', '.join(cols)} FROM {table} "
                    f"WHERE domain = {self._p}"
                )
                rows = _fetch_all(self._conn, sql, (domain,), cols)
                row_counts[table] = len(rows)
                if table == "entities":
                    entity_ids = [r["id"] for r in rows]
                elif table == "events":
                    event_ids = [r["id"] for r in rows]
                writer.add_jsonl(
                    member,
                    rows=iter(rows),
                    header={"table": table, "columns": list(cols)},
                    json_dumps=dumps,
                )

            # --- Tier-1 + Tier-2 edges: JOIN-filtered ------------------
            for table, member in _EDGE_EXPORTS:
                cols = all_cols[table]
                sql = _EDGE_SQL_TEMPLATES[table].format(
                    cols=_prefix_cols(_EDGE_ALIAS[table], cols),
                    p=self._p,
                )
                rows = _fetch_all(self._conn, sql, (domain, domain), cols)
                row_counts[f"edges.{table}"] = len(rows)
                writer.add_jsonl(
                    member,
                    rows=iter(rows),
                    header={"table": table, "columns": list(cols)},
                    json_dumps=dumps,
                )

            # --- Vectors (gated by options.include_vectors) -----------
            # Embeddings live in the backend-specific entities/events.embedding
            # column (sqlite-vec BLOB / pgvector). Exported as a portable
            # list[float] in a side member so the generic column path stays
            # backend-neutral. Lossless graph migration depends on this.
            if opts.include_vectors:
                for table, member in (
                    ("entities", "db/vectors/entities.jsonl"),
                    ("events", "db/vectors/events.jsonl"),
                ):
                    rows = self._fetch_vectors(table, domain)
                    row_counts[f"vectors.{table}"] = len(rows)
                    writer.add_jsonl(
                        member,
                        rows=iter(rows),
                        header={"table": f"{table}.embedding",
                                "columns": ["id", "embedding"]},
                        json_dumps=dumps,
                    )

            # --- Derived: jaccard / community / training --------------
            if opts.include_derived:
                self._export_derived(
                    writer, _DERIVED_EXPORTS, domain, row_counts,
                )

            # --- Plan (gated by options.include_plan) -----------------
            if opts.include_plan:
                # plan node tables (direct domain) + plan edges (events JOIN)
                self._export_derived(
                    writer, _PLAN_EDGE_EXPORTS, domain, row_counts,
                )
                for table, member in _PLAN_EXPORTS:
                    cols = all_cols[table]
                    sql = (
                        f"SELECT {', '.join(cols)} FROM {table} "
                        f"WHERE domain = {self._p}"
                    )
                    rows = _fetch_all(self._conn, sql, (domain,), cols)
                    row_counts[f"plan.{table}"] = len(rows)
                    writer.add_jsonl(
                        member,
                        rows=iter(rows),
                        header={"table": table, "columns": list(cols)},
                        json_dumps=dumps,
                    )

            # --- Segments (gated by options.include_segments) ---------
            if opts.include_segments:
                # k2g_raw_document — direct domain
                cols = all_cols["k2g_raw_document"]
                sql = (
                    f"SELECT {', '.join(cols)} FROM k2g_raw_document "
                    f"WHERE domain = {self._p}"
                )
                rows = _fetch_all(self._conn, sql, (domain,), cols)
                row_counts["k2g_raw_document"] = len(rows)
                writer.add_jsonl(
                    "db/k2g_raw_document.jsonl",
                    rows=iter(rows),
                    header={"table": "k2g_raw_document", "columns": list(cols)},
                    json_dumps=dumps,
                )

                # k2g_segment_blob — JOIN raw_document (single-param)
                cols = all_cols["k2g_segment_blob"]
                sql = (
                    f"SELECT {_prefix_cols('sb', cols)} FROM k2g_segment_blob sb "
                    f"JOIN k2g_raw_document rd ON sb.raw_document_id = rd.id "
                    f"WHERE rd.domain = {self._p}"
                )
                rows = _fetch_all(self._conn, sql, (domain,), cols)
                row_counts["k2g_segment_blob"] = len(rows)
                writer.add_jsonl(
                    "db/k2g_segment_blob.jsonl",
                    rows=iter(rows),
                    header={"table": "k2g_segment_blob", "columns": list(cols)},
                    json_dumps=dumps,
                )

            # --- Content store (BP-86, gated by options.include_content) --
            # Raw text bodies live in a *separate* DB (content_store.db),
            # not the graph DB — exported via the content store connection.
            if opts.include_content and self._content_store is not None:
                cs_conn = getattr(self._content_store, "_conn", None)
                if cs_conn is not None:
                    cols = all_cols["content_store"]
                    sql = (
                        f"SELECT {', '.join(cols)} FROM content_store "
                        f"WHERE domain = {self._p}"
                    )
                    rows = _fetch_all(cs_conn, sql, (domain,), cols)
                    row_counts["content_store"] = len(rows)
                    writer.add_jsonl(
                        "db/content_store.jsonl",
                        rows=iter(rows),
                        header={"table": "content_store", "columns": list(cols)},
                        json_dumps=dumps,
                    )

            # --- Manifest (last write, but reader is order-agnostic) ----
            manifest = ArchiveManifest(
                source=ArchiveSource(
                    project=self._project,
                    group=self._group,
                    domain=domain,
                    embedding=embedding or {},
                    host=platform.node(),
                    user=getpass.getuser(),
                    k2g_version=self._k2g_version,
                ),
                exported_at=utc_now_iso(),
                options=opts,
                row_counts=row_counts,
                source_hash=compute_source_hash(event_ids, entity_ids),
            )
            writer.add_text("manifest.json", manifest.to_json())

        self.last_archive_path = path
        logger.info(
            "Exported domain=%s rows=%d → %s",
            domain, sum(row_counts.values()), path,
        )
        return manifest

    # --- helpers ------------------------------------------------------

    def _safe_fetch(
        self, sql: str, params: tuple, cols: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """``_fetch_all`` that tolerates a missing table/column (older schema).

        Returns [] and rolls back the aborted statement (Postgres) instead of
        failing the whole export when a derived table isn't present yet.
        """
        try:
            return _fetch_all(self._conn, sql, params, cols)
        except Exception as exc:  # noqa: BLE001
            try:
                self._conn.rollback()
            except Exception:
                pass
            logger.debug("derived export skipped (%s): %s", sql[:60], exc)
            return []

    def _export_derived(
        self,
        writer: Any,
        specs: tuple[tuple[str, str, str | None, str, int], ...],
        domain: str,
        row_counts: dict[str, int],
    ) -> None:
        """Export a set of derived/edge tables, each tolerant of absence."""
        all_cols = _all_table_cols()
        for table, member, alias, from_where, nparams in specs:
            cols = all_cols[table]
            sel = _prefix_cols(alias, cols) if alias else ", ".join(cols)
            sql = f"SELECT {sel} {from_where.format(p=self._p)}"  # noqa: S608
            rows = self._safe_fetch(sql, (domain,) * nparams, cols)
            row_counts[f"derived.{table}"] = len(rows)
            writer.add_jsonl(
                member,
                rows=iter(rows),
                header={"table": table, "columns": list(cols)},
                json_dumps=dumps,
            )

    def _fetch_vectors(self, table: str, domain: str) -> list[dict[str, Any]]:
        """Read ``(id, embedding)`` for a domain, embedding decoded to floats.

        Only rows that actually carry a vector are emitted (``embedding IS NOT
        NULL``), so a partially-embedded domain exports just what it has.
        """
        from k2g.portable.vector_codec import decode_embedding

        sql = (
            f"SELECT id, embedding FROM {table} "  # noqa: S608 — table is a fixed literal
            f"WHERE domain = {self._p} AND embedding IS NOT NULL"
        )
        cur = self._conn.cursor()
        try:
            cur.execute(sql, (domain,))
            out: list[dict[str, Any]] = []
            for row in cur.fetchall():
                # PostgresGraphStore uses a RealDictCursor → rows are dict-like;
                # SQLite returns tuples. Tuple-unpacking a dict row yields its KEY
                # names ('id', 'embedding'), so the old `for rid, emb in ...` made
                # decode_embedding choke and silently exported 0 vectors on PG.
                if isinstance(row, dict):
                    rid, emb = row["id"], row["embedding"]
                else:
                    rid, emb = row[0], row[1]
                vec = decode_embedding(emb)
                if vec is not None:
                    out.append({"id": rid, "embedding": vec})
            return out
        except Exception as exc:  # noqa: BLE001
            # No embedding column on this schema (older/minimal DB) → nothing to
            # export. Roll back the aborted statement (Postgres) and move on.
            try:
                self._conn.rollback()
            except Exception:
                pass
            logger.debug("vector export skipped for %s: %s", table, exc)
            return []
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def suggested_archive_name(self, domain: str) -> str:
        """`<group>_<domain>_<UTC-basic-ISO>.mweft.tar.gz`."""
        ts = utc_now_iso().replace(":", "").replace("-", "")
        return f"{self._group}_{domain}_{ts}.mweft.tar.gz"
