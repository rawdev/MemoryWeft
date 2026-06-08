"""BP-74 — DomainImporter.

Reads a `.mweft.tar.gz` archive and applies its rows to a target graph store
via direct INSERT statements (column whitelist).  Direct SQL is used rather
than the high-level Protocol write methods so that archive-time values for
``created_at`` / ``timestamp`` / ``deprecated`` etc. are preserved verbatim —
round-trip exactness is the whole point.

Strategies:
- ``skip``       (default) — PK conflict ⇒ row ignored.
- ``overwrite``  — PK conflict ⇒ row replaced.
- ``fail``       — PK conflict ⇒ abort + rollback.

Independent ``dry_run=True`` flag suppresses all writes and reports what
*would* happen.  Useful before destructive (overwrite) imports.

Transactions: a single ``BEGIN ... COMMIT`` wraps the entire import; on
exception the entire archive is rolled back.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

from k2g.portable.archive_io import ArchiveReader, DirReader
from k2g.portable.manifest import ArchiveManifest
from k2g.portable.schema import (
    CONTENT_TABLES,
    SEGMENT_TABLES,
    TABLE_PKS,
    TIER1_TABLES,
    TIER2_TABLES,
    denormalize_for_column,
    paramstyle_placeholder,
)

logger = logging.getLogger(__name__)


class SchemaVersionMismatch(Exception):
    """The archive's `schema_version` is incompatible with this importer."""


class TargetNotEmpty(Exception):
    """Restore mode hit a non-empty target without ``confirm_replace=True``.

    Carries per-table existing row counts so the caller (HTTP route / UI) can
    warn the user precisely about what a destructive replace would erase.
    """

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = dict(counts)
        total = sum(counts.values())
        super().__init__(
            f"target database is not empty ({total} rows across "
            f"{len(counts)} table(s)); a restore would ERASE it — "
            f"pass confirm_replace=True to proceed"
        )


class ConflictStrategy(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    FAIL = "fail"


_VALID_STRATEGIES = {s.value for s in ConflictStrategy}

# Import modes (intent-level, above the SQL conflict strategy):
#   merge   — legacy row-by-row, PK-conflict strategy applies (skip/overwrite/fail)
#   restore — clone: make the target EXACTLY match the archive. Bulk insert into
#             an empty target; a non-empty target is fully wiped first and so
#             requires confirm_replace=True. No identity remap (that is the
#             future "merge by name" work) — entities/tags keep archive UUIDs.
_VALID_MODES = {"merge", "restore"}

# Rows per executemany batch in restore bulk insert — bounds peak memory while
# keeping the round-trip count low.
_BULK_CHUNK = 1000


# FK-safe insertion order: (table, archive_member, optional).
# Tier-1 members are mandatory; Tier-2 members are optional (older archives
# may not contain them, and ``options.include_plan=False`` archives omit the
# ``plan/`` folder).
IMPORT_ORDER: tuple[tuple[str, str, bool], ...] = (
    # Tier-1 base
    ("entities",                "db/entities.jsonl", False),
    ("events",                  "db/events.jsonl", False),
    ("groups",                  "db/groups.jsonl", False),
    # Tier-2 base (CG / ETG before any edge that references them)
    ("context_groups",          "db/context_groups.jsonl", True),
    ("event_template_groups",   "db/event_template_groups.jsonl", True),
    # Tier-1 edges
    ("participated_in",         "db/edges/participated_in.jsonl", False),
    ("event_member_of",         "db/edges/event_member_of.jsonl", False),
    ("event_sequential_next",   "db/edges/event_sequential_next.jsonl", False),
    ("entity_connection",       "db/edges/entity_connection.jsonl", False),
    # Tier-2 plan (gated upstream by options.include_plan)
    ("plan_nodes",              "db/plan/plan_nodes.jsonl", True),
    ("plan_direction_nodes",    "db/plan/plan_direction_nodes.jsonl", True),
    # Tier-2 references
    ("k2g_entity_alias",        "db/edges/k2g_entity_alias.jsonl", True),
    ("k2g_event_reference",     "db/edges/k2g_event_reference.jsonl", True),
    # Segments (gated upstream by options.include_segments)
    ("k2g_raw_document",        "db/k2g_raw_document.jsonl", True),
    ("k2g_segment_blob",        "db/k2g_segment_blob.jsonl", True),
)

# Backward compatibility — Tier-1-only subset still referenced by older
# tests / external callers.
TIER1_ORDER: tuple[str, ...] = tuple(t for t, _, opt in IMPORT_ORDER if not opt)


def _all_cols() -> dict[str, tuple[str, ...]]:
    return {**TIER1_TABLES, **TIER2_TABLES, **SEGMENT_TABLES, **CONTENT_TABLES}


def _insert_sql(
    table: str,
    columns: tuple[str, ...],
    pk_cols: tuple[str, ...],
    backend: str,
    strategy: str,
) -> str:
    p = paramstyle_placeholder(backend)
    cols_sql = ", ".join(columns)
    placeholders = ", ".join(p for _ in columns)
    base = f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})"

    if strategy == ConflictStrategy.SKIP.value:
        if backend == "sqlite":
            return f"INSERT OR IGNORE INTO {table} ({cols_sql}) VALUES ({placeholders})"
        return f"{base} ON CONFLICT DO NOTHING"

    if strategy == ConflictStrategy.OVERWRITE.value:
        if backend == "sqlite":
            return (
                f"INSERT OR REPLACE INTO {table} ({cols_sql}) "
                f"VALUES ({placeholders})"
            )
        pk_list = ", ".join(pk_cols)
        non_pk = [c for c in columns if c not in pk_cols]
        if not non_pk:
            return f"{base} ON CONFLICT ({pk_list}) DO NOTHING"
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
        return f"{base} ON CONFLICT ({pk_list}) DO UPDATE SET {updates}"

    if strategy == ConflictStrategy.FAIL.value:
        return base  # plain INSERT — duplicate PK raises IntegrityError

    raise ValueError(f"unknown strategy: {strategy!r}")


def _plain_insert_sql(
    table: str,
    columns: tuple[str, ...],
    backend: str,
) -> str:
    """Bare ``INSERT INTO t (cols) VALUES (...)`` — no conflict clause.

    Used by restore (clone) mode: the target is empty (or wiped first), so a
    plain insert cannot conflict, and ``executemany`` can batch it.
    """
    p = paramstyle_placeholder(backend)
    cols_sql = ", ".join(columns)
    placeholders = ", ".join(p for _ in columns)
    return f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})"


def _topo_order_by_parent(
    rows: list[dict[str, Any]],
    id_col: str = "id",
    parent_col: str = "parent_id",
) -> list[dict[str, Any]]:
    """Order self-referential rows so each parent precedes its children.

    Used for Postgres bulk restore of ``groups`` / ``context_groups`` whose
    ``parent_id`` FK is self-referential and (on managed PG like Supabase) NOT
    deferrable: a child can otherwise land in an earlier ``execute_values`` page
    than its parent and trip an immediate FK check. Rows whose parent is NULL or
    outside the set come first. Iterative (no recursion limit); cycle-safe.
    """
    by_id = {r.get(id_col): r for r in rows}
    visited: set[Any] = set()
    out: list[dict[str, Any]] = []
    for start in rows:
        if start.get(id_col) in visited:
            continue
        # Walk up the parent chain, then emit top-down (ancestor first).
        stack: list[dict[str, Any]] = []
        seen_local: set[Any] = set()
        cur: dict[str, Any] | None = start
        while cur is not None:
            cid = cur.get(id_col)
            if cid in visited or cid in seen_local:
                break  # already placed, or a cycle — stop walking
            seen_local.add(cid)
            stack.append(cur)
            pid = cur.get(parent_col)
            cur = by_id.get(pid) if (pid is not None and pid != cid) else None
        for r in reversed(stack):
            rid = r.get(id_col)
            if rid not in visited:
                visited.add(rid)
                out.append(r)
    return out


def _pk_exists_sql(
    table: str,
    pk_cols: tuple[str, ...],
    backend: str,
) -> str:
    p = paramstyle_placeholder(backend)
    where = " AND ".join(f"{c} = {p}" for c in pk_cols)
    return f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"


def _empty_counts() -> dict[str, int]:
    return {"inserted": 0, "skipped": 0, "overwritten": 0, "failed": 0}


def _open_reader(archive_path: str | Path) -> ArchiveReader | DirReader:
    """Pick the reader by path shape — a directory is an unpacked export
    folder (:class:`DirReader`), anything else a ``.mweft.tar.gz`` archive."""
    return (
        DirReader(archive_path)
        if Path(archive_path).is_dir()
        else ArchiveReader(archive_path)
    )


class DomainImporter:
    """Apply a `.mweft.tar.gz` archive to a target graph store."""

    def __init__(
        self, graph: Any, backend: str, *, content_store: Any = None,
    ) -> None:
        self._graph = graph
        self._conn = graph._conn
        self._backend = backend
        # BP-86 — content store (separate DB connection).  None ⇒ skip the
        # content import step.
        self._content_store = content_store

    def import_archive(
        self,
        archive_path: str | Path,
        *,
        strategy: str = ConflictStrategy.SKIP.value,
        dry_run: bool = False,
        mode: str = "merge",
        confirm_replace: bool = False,
    ) -> dict[str, Any]:
        """Import an archive in ``merge`` (legacy) or ``restore`` (clone) mode.

        Returns a dict with ``manifest`` / ``dry_run`` / ``mode`` /
        ``existing_counts`` / ``results`` (and ``strategy`` for merge).

        - ``mode="restore"`` makes the target EXACTLY match the archive. An
          empty target is bulk-inserted; a non-empty target is fully wiped
          first and therefore requires ``confirm_replace=True`` (otherwise
          :class:`TargetNotEmpty` is raised so the caller can confirm). A
          ``dry_run`` restore writes nothing and reports ``existing_counts``
          (what a replace would erase) plus would-insert counts.
        - ``mode="merge"`` keeps the legacy per-row PK-conflict behavior
          (``strategy`` = skip|overwrite|fail). ``dry_run`` reports what would
          happen with no DB mutation.
        """
        if mode not in _VALID_MODES:
            raise ValueError(
                f"unknown mode={mode!r}; expected one of {sorted(_VALID_MODES)}",
            )
        if mode == "merge" and strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"unknown strategy={strategy!r}; "
                f"expected one of {sorted(_VALID_STRATEGIES)}",
            )

        results: dict[str, dict[str, int]] = {
            t: _empty_counts() for t, _, _ in IMPORT_ORDER
        }

        with _open_reader(archive_path) as r:
            manifest = ArchiveManifest.from_json(r.read_text("manifest.json"))
            if not manifest.is_compatible():
                raise SchemaVersionMismatch(
                    f"archive schema_version={manifest.schema_version!r} is "
                    f"incompatible with this importer"
                )

            if mode == "restore":
                return self._run_restore(
                    r, manifest, results, dry_run=dry_run,
                    confirm_replace=confirm_replace,
                )

            logger.info(
                "Importing domain=%s strategy=%s dry_run=%s — large archives "
                "can take several minutes; per-table progress follows.",
                manifest.source.domain, strategy, dry_run,
            )
            if dry_run:
                self._dry_run(r, strategy, results)
            else:
                self._real_import(r, strategy, results)
            # BP-86 — content store (separate connection, no FK constraints)
            self._import_content_store(r, strategy, results, dry_run)

        logger.info(
            "Imported domain=%s strategy=%s dry_run=%s results=%s",
            manifest.source.domain, strategy, dry_run, results,
        )
        return {
            "manifest": manifest,
            "dry_run": dry_run,
            "mode": "merge",
            "strategy": strategy,
            "existing_counts": {},
            "results": results,
        }

    # ------------------------------------------------------------------
    # restore (clone) mode — BP-74 follow-up
    # ------------------------------------------------------------------

    def _run_restore(
        self,
        reader: ArchiveReader | DirReader,
        manifest: ArchiveManifest,
        results: dict[str, dict[str, int]],
        *,
        dry_run: bool,
        confirm_replace: bool,
    ) -> dict[str, Any]:
        existing = self._existing_counts()

        if dry_run:
            would = self._count_archive_rows(reader)
            logger.info(
                "Restore dry-run domain=%s existing=%s would_insert=%s",
                manifest.source.domain, existing, would,
            )
            return {
                "manifest": manifest,
                "dry_run": True,
                "mode": "restore",
                "existing_counts": existing,
                "results": {
                    t: {**_empty_counts(), "inserted": n}
                    for t, n in would.items()
                },
                "strategy": None,
            }

        if existing and not confirm_replace:
            raise TargetNotEmpty(existing)

        # Always wipe-then-insert. Restore must end up EXACTLY matching the
        # archive, and globally-unique names in the archive (notably
        # groups.name) would otherwise collide with ANY pre-existing row — even
        # one the count probe missed. On a truly empty target the DELETEs are
        # harmless no-ops.
        logger.info(
            "Restoring domain=%s (existing=%s) — wiping target then bulk-"
            "loading the archive; per-table progress follows.",
            manifest.source.domain, existing,
        )
        self._restore_graph(reader, results, wipe=True)
        self._restore_content_store(reader, results, wipe=True)
        self._restore_vectors(reader, results)
        self._reconcile_domain_registry()
        logger.info(
            "Restored domain=%s results=%s", manifest.source.domain, results,
        )
        return {
            "manifest": manifest,
            "dry_run": False,
            "mode": "restore",
            "existing_counts": existing,
            "results": results,
            "strategy": None,
        }

    def _reconcile_domain_registry(self) -> None:
        """Make ``domain_registry`` match the restored data.

        The registry is not carried in the archive, so after a full-replace
        restore it still lists the *old* project's domains and omits the
        archive's. Reset it to exactly the domains present in the restored rows
        (entities ∪ events): drop stale entries, register the real ones. A
        missing table (older schema) is a no-op.
        """
        conn = self._conn
        try:
            self._exec(conn, "DELETE FROM domain_registry")
        except Exception:
            try:
                conn.rollback()  # table absent on this schema — nothing to do
            except Exception:
                pass
            return

        domains: set[str] = set()
        for table in ("entities", "events"):
            cur = conn.cursor()
            try:
                cur.execute(
                    f"SELECT DISTINCT domain FROM {table} "  # noqa: S608
                    f"WHERE domain IS NOT NULL"
                )
                domains.update(row[0] for row in cur.fetchall())
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

        p = paramstyle_placeholder(self._backend)
        for d in sorted(domains):
            cur = conn.cursor()
            try:
                cur.execute(
                    f"INSERT INTO domain_registry (name) VALUES ({p})", (d,),
                )
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        conn.commit()
        logger.info("Reconciled domain_registry → %s", sorted(domains))

    def _existing_counts(self) -> dict[str, int]:
        """Per-table existing row counts in the target (nonzero only).

        A missing table (fresh DB, Tier-2 not yet created) counts as 0. On
        Postgres a missing-table error aborts the shared transaction, so we
        roll back after each failed probe to keep the connection usable.
        """
        out: dict[str, int] = {}
        # Clear any aborted transaction first: on a shared Postgres connection a
        # single prior failed statement leaves it in InFailedSqlTransaction, and
        # then EVERY COUNT below raises and is swallowed to 0 — silently
        # under-reporting the target as empty (the bug that skipped the wipe).
        try:
            self._conn.rollback()
        except Exception:
            pass
        for table, _m, _o in IMPORT_ORDER:
            n = self._safe_count(self._conn, table)
            if n:
                out[table] = n
        if self._content_store is not None:
            cs_conn = getattr(self._content_store, "_conn", None)
            if cs_conn is not None:
                n = self._safe_count(cs_conn, "content_store")
                if n:
                    out["content_store"] = n
        return out

    def _safe_count(self, conn: Any, table: str) -> int:
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — fixed table list
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            # A missing table (fresh DB) is expected → 0. Anything else is worth
            # a line so a silent under-count can't hide again.
            logger.debug("existing-count probe failed for %s: %s", table, exc)
            try:
                conn.rollback()  # PG: clear aborted txn from the failed probe
            except Exception:
                pass
            return 0
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass

    def _count_archive_rows(
        self, reader: ArchiveReader | DirReader,
    ) -> dict[str, int]:
        out: dict[str, int] = {}
        for table, member, optional in IMPORT_ORDER:
            if optional and not reader.has_member(member):
                continue
            out[table] = sum(1 for _ in reader.iter_jsonl(member))
        if reader.has_member("db/content_store.jsonl"):
            out["content_store"] = sum(
                1 for _ in reader.iter_jsonl("db/content_store.jsonl")
            )
        return out

    @staticmethod
    def _exec(conn: Any, sql: str) -> None:
        """Run a side-effecting statement on either DB-API connection.

        sqlite3 connections expose ``.execute`` directly, psycopg2 ones do
        not — going through a cursor works for both.
        """
        cur = conn.cursor()
        try:
            cur.execute(sql)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _restore_graph(
        self,
        reader: ArchiveReader | DirReader,
        results: dict[str, dict[str, int]],
        *,
        wipe: bool,
    ) -> None:
        """Bulk-restore the graph tables (single transaction).

        Self-referential rows (groups.parent_id / context_groups.parent_id)
        can precede their parent, so FK enforcement is relaxed for the
        duration: SQLite disables it outright (``PRAGMA foreign_keys=OFF``),
        Postgres defers it where the schema allows. The target is clean
        (empty, or wiped here), so a plain INSERT cannot conflict.
        """
        cols_map = _all_cols()
        try:
            if self._backend == "sqlite":
                for _setup in ("setup_training_schema", "setup_bp30_schema"):
                    fn = getattr(self._graph, _setup, None)
                    if callable(fn):
                        fn()
                self._conn.commit()
                self._exec(self._conn, "PRAGMA foreign_keys = OFF")
            else:
                # Postgres: defer FK checks where constraints are DEFERRABLE so
                # a self-referential child/parent pair inside one bulk statement
                # is fine. No-op (and harmless) for non-deferrable constraints.
                try:
                    self._exec(self._conn, "SET CONSTRAINTS ALL DEFERRED")
                except Exception:
                    pass

            if wipe:
                for table, _m, _o in reversed(IMPORT_ORDER):
                    try:
                        self._exec(self._conn, f"DELETE FROM {table}")  # noqa: S608
                    except Exception:
                        # Table may not exist on this target — nothing to wipe.
                        pass

            for table, member, optional in IMPORT_ORDER:
                if optional and not reader.has_member(member):
                    continue
                cols = cols_map[table]
                count = self._bulk_insert(self._conn, table, cols, reader, member)
                results[table]["inserted"] = count
                logger.info("  restored %s: %d rows", table, count)

            self._conn.commit()
            if self._backend == "sqlite":
                self._exec(self._conn, "PRAGMA foreign_keys = ON")
        except Exception:
            self._conn.rollback()
            if self._backend == "sqlite":
                try:
                    self._exec(self._conn, "PRAGMA foreign_keys = ON")
                except Exception:
                    pass
            raise

    def _restore_content_store(
        self,
        reader: ArchiveReader | DirReader,
        results: dict[str, dict[str, int]],
        *,
        wipe: bool,
    ) -> None:
        member = "db/content_store.jsonl"
        if self._content_store is None or not reader.has_member(member):
            return
        cs_conn = getattr(self._content_store, "_conn", None)
        if cs_conn is None:
            return
        cols = _all_cols()["content_store"]
        try:
            if wipe:
                try:
                    self._exec(cs_conn, "DELETE FROM content_store")
                except Exception:
                    pass
            count = self._bulk_insert(cs_conn, "content_store", cols, reader, member)
            cs_conn.commit()
            results["content_store"] = {**_empty_counts(), "inserted": count}
        except Exception:
            cs_conn.rollback()
            raise

    def _restore_vectors(
        self,
        reader: ArchiveReader | DirReader,
        results: dict[str, dict[str, int]],
    ) -> None:
        """Restore entity/event embeddings from the ``db/vectors/*`` members.

        Embeddings ride a side member (portable ``list[float]``) rather than the
        generic column path, because the storage form is backend-specific:
        a sqlite-vec BLOB under SQLite, a pgvector ``vector`` under Postgres.
        Entities/events were already inserted by the graph restore, so this is a
        bulk UPDATE keyed by id. Absent members (older archives /
        ``include_vectors=False``) are simply skipped — vectors stay NULL.
        """
        for table, member in (
            ("entities", "db/vectors/entities.jsonl"),
            ("events", "db/vectors/events.jsonl"),
        ):
            if not reader.has_member(member):
                continue
            try:
                n = self._bulk_update_vectors(table, reader, member)
                results.setdefault(table, _empty_counts())
                results[table]["vectors"] = n
                logger.info("  restored %s embeddings: %d rows", table, n)
            except Exception:
                self._conn.rollback()
                raise

    def _bulk_update_vectors(
        self,
        table: str,
        reader: ArchiveReader | DirReader,
        member: str,
        chunk: int = _BULK_CHUNK,
    ) -> int:
        """Bulk UPDATE ``{table}.embedding`` from a vectors member.

        SQLite: chunked ``executemany`` with sqlite-vec BLOBs.
        Postgres: ``execute_values`` + ``UPDATE … FROM (VALUES …)`` with an
        explicit ``::vector`` cast on the text literal (mirrors
        PgVectorStore.upsert; needs no pgvector adapter on this connection).
        """
        from k2g.portable.vector_codec import (
            encode_embedding_sqlite,
            pg_vector_literal,
        )

        count = 0
        if self._backend == "sqlite":
            sql = f"UPDATE {table} SET embedding = ? WHERE id = ?"  # noqa: S608
            cur = self._conn.cursor()
            try:
                batch: list[tuple[Any, ...]] = []
                for row in reader.iter_jsonl(member):
                    blob = encode_embedding_sqlite(row.get("embedding"))
                    if blob is None:
                        continue
                    batch.append((blob, row.get("id")))
                    if len(batch) >= chunk:
                        cur.executemany(sql, batch)
                        count += len(batch)
                        batch = []
                if batch:
                    cur.executemany(sql, batch)
                    count += len(batch)
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            return count

        # Postgres
        from psycopg2.extras import execute_values

        sql = (
            f"UPDATE {table} AS t SET embedding = d.emb::vector "  # noqa: S608
            f"FROM (VALUES %s) AS d(id, emb) WHERE t.id = d.id"
        )
        cur = self._conn.cursor()
        try:
            page = 500
            batch = []
            for row in reader.iter_jsonl(member):
                lit = pg_vector_literal(row.get("embedding"))
                if lit is None:
                    continue
                batch.append((row.get("id"), lit))
                if len(batch) >= page:
                    execute_values(cur, sql, batch, page_size=page)
                    count += len(batch)
                    batch = []
            if batch:
                execute_values(cur, sql, batch, page_size=page)
                count += len(batch)
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return count

    def _bulk_insert(
        self,
        conn: Any,
        table: str,
        cols: tuple[str, ...],
        reader: ArchiveReader | DirReader,
        member: str,
    ) -> int:
        """Stream a JSONL member into a clean target as a true bulk insert.

        - **SQLite**: chunked ``executemany`` (the fast native path).
        - **Postgres**: ``psycopg2.extras.execute_values`` — a single multi-row
          ``INSERT ... VALUES (...), (...), …`` per page, turning ~N row
          round-trips into ~N/page_size. Essential over a remote DB where each
          round-trip carries network latency.
        """
        if self._backend == "sqlite":
            return self._bulk_insert_sqlite(conn, table, cols, reader, member)
        return self._bulk_insert_pg(conn, table, cols, reader, member)

    def _bulk_insert_sqlite(
        self,
        conn: Any,
        table: str,
        cols: tuple[str, ...],
        reader: ArchiveReader | DirReader,
        member: str,
        chunk: int = _BULK_CHUNK,
    ) -> int:
        sql = _plain_insert_sql(table, cols, "sqlite")
        count = 0
        cur = conn.cursor()
        try:
            batch: list[tuple[Any, ...]] = []
            for row in reader.iter_jsonl(member):
                batch.append(
                    tuple(denormalize_for_column(c, row.get(c)) for c in cols)
                )
                if len(batch) >= chunk:
                    cur.executemany(sql, batch)
                    count += len(batch)
                    batch = []
            if batch:
                cur.executemany(sql, batch)
                count += len(batch)
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return count

    def _bulk_insert_pg(
        self,
        conn: Any,
        table: str,
        cols: tuple[str, ...],
        reader: ArchiveReader | DirReader,
        member: str,
    ) -> int:
        from psycopg2.extras import execute_values

        cols_sql = ", ".join(cols)
        sql = f"INSERT INTO {table} ({cols_sql}) VALUES %s"  # noqa: S608
        # Rows-per-statement under Postgres' 65535-parameter ceiling.
        page = max(1, min(5000, 60000 // max(1, len(cols))))
        cur = conn.cursor()
        try:
            if "id" in cols and "parent_id" in cols:
                # Self-referential table (groups / context_groups): order
                # parents before children so a child never precedes its parent
                # across pages (the FK is immediate / non-deferrable here).
                # These tables are small, so materializing them is fine.
                rows = _topo_order_by_parent(list(reader.iter_jsonl(member)))
                values = [
                    tuple(denormalize_for_column(c, r.get(c)) for c in cols)
                    for r in rows
                ]
                execute_values(cur, sql, values, page_size=page)
                return len(values)

            # Non-self-referential: stream (low memory) and count as we go.
            count = 0

            def _rows():
                nonlocal count
                for row in reader.iter_jsonl(member):
                    count += 1
                    yield tuple(denormalize_for_column(c, row.get(c)) for c in cols)

            execute_values(cur, sql, _rows(), page_size=page)
            return count
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _real_import(
        self,
        reader: ArchiveReader | DirReader,
        strategy: str,
        results: dict[str, dict[str, int]],
    ) -> None:
        cols_map = _all_cols()
        try:
            if self._backend == "sqlite":
                # A fresh target DB has only the Tier-1 schema (graph store
                # __init__ creates it).  Tier-2 tables (context_groups / ETG /
                # plan) are created on demand — ensure they exist before
                # importing Tier-2 rows.  CREATE TABLE IF NOT EXISTS keeps
                # this idempotent on an already-provisioned DB.
                for _setup in ("setup_training_schema", "setup_bp30_schema"):
                    _fn = getattr(self._graph, _setup, None)
                    if callable(_fn):
                        _fn()
                # Bulk load — disable FK enforcement.  Rows cannot be inserted
                # in a single FK-safe order because of self-references
                # (groups.parent_id, context_groups.parent_id): a child may
                # precede its parent.  ``PRAGMA defer_foreign_keys`` is reset
                # at every COMMIT and only applies inside the transaction it
                # is set in — it cannot be enabled reliably across Python's
                # autocommit boundary, so disabling enforcement outright is
                # the robust bulk-restore idiom.  Safe here: the exporter
                # JOIN-filters every edge to same-domain endpoints, so the
                # archive is internally consistent by construction.  PRAGMA
                # foreign_keys is a no-op inside a transaction → close any
                # open one first.
                self._conn.commit()
                self._conn.execute("PRAGMA foreign_keys = OFF")

            for table, member, optional in IMPORT_ORDER:
                if optional and not reader.has_member(member):
                    continue
                cols = cols_map[table]
                pk_cols = TABLE_PKS[table]
                sql = _insert_sql(table, cols, pk_cols, self._backend, strategy)
                cur = self._conn.cursor()
                try:
                    for row in reader.iter_jsonl(member):
                        values = tuple(
                            denormalize_for_column(c, row.get(c)) for c in cols
                        )
                        before = self._pk_existed(table, pk_cols, row)
                        cur.execute(sql, values)
                        rowcount = cur.rowcount or 0

                        if strategy == ConflictStrategy.SKIP.value:
                            if rowcount > 0:
                                results[table]["inserted"] += 1
                            else:
                                results[table]["skipped"] += 1
                        elif strategy == ConflictStrategy.OVERWRITE.value:
                            if before:
                                results[table]["overwritten"] += 1
                            else:
                                results[table]["inserted"] += 1
                        elif strategy == ConflictStrategy.FAIL.value:
                            # plain INSERT — if we reached here, the row
                            # was inserted (duplicate would have raised)
                            results[table]["inserted"] += 1
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                logger.info(
                    "  imported %s: %s",
                    table,
                    {k: v for k, v in results[table].items() if v} or "0 rows",
                )
            self._conn.commit()
            if self._backend == "sqlite":
                self._conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            self._conn.rollback()
            if self._backend == "sqlite":
                try:
                    self._conn.execute("PRAGMA foreign_keys = ON")
                except Exception:
                    pass
            raise

    def _dry_run(
        self,
        reader: ArchiveReader | DirReader,
        strategy: str,
        results: dict[str, dict[str, int]],
    ) -> None:
        # Read-only: probe each row's PK; do not INSERT.  Caller's connection
        # is never written to, so no commit/rollback needed.
        for table, member, optional in IMPORT_ORDER:
            if optional and not reader.has_member(member):
                continue
            pk_cols = TABLE_PKS[table]
            sql = _pk_exists_sql(table, pk_cols, self._backend)
            cur = self._conn.cursor()
            try:
                for row in reader.iter_jsonl(member):
                    pk_values = tuple(row.get(c) for c in pk_cols)
                    cur.execute(sql, pk_values)
                    exists = cur.fetchone() is not None

                    if not exists:
                        results[table]["inserted"] += 1
                        continue

                    # PK conflict — strategy decides
                    if strategy == ConflictStrategy.SKIP.value:
                        results[table]["skipped"] += 1
                    elif strategy == ConflictStrategy.OVERWRITE.value:
                        results[table]["overwritten"] += 1
                    elif strategy == ConflictStrategy.FAIL.value:
                        results[table]["failed"] += 1
                        # do NOT short-circuit — continue counting so the
                        # report shows every conflict, not just the first
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    def _import_content_store(
        self,
        reader: ArchiveReader | DirReader,
        strategy: str,
        results: dict[str, dict[str, int]],
        dry_run: bool,
    ) -> None:
        """BP-86 — import ``db/content_store.jsonl`` into the target content
        store (a *separate* connection from the graph DB).

        No-op when no content store was wired in or the archive predates
        BP-86 (member absent).  ``content_store`` is a standalone table with
        no foreign keys — none of the graph import's FK handling applies.
        """
        member = "db/content_store.jsonl"
        if self._content_store is None or not reader.has_member(member):
            return
        cs_conn = getattr(self._content_store, "_conn", None)
        if cs_conn is None:
            return

        cols = _all_cols()["content_store"]
        pk_cols = TABLE_PKS["content_store"]
        counts = _empty_counts()
        results["content_store"] = counts

        exists_sql = _pk_exists_sql("content_store", pk_cols, self._backend)
        insert_sql = _insert_sql(
            "content_store", cols, pk_cols, self._backend, strategy,
        )
        cur = cs_conn.cursor()
        try:
            for row in reader.iter_jsonl(member):
                cur.execute(exists_sql, tuple(row.get(c) for c in pk_cols))
                existed = cur.fetchone() is not None

                if dry_run:
                    if not existed:
                        counts["inserted"] += 1
                    elif strategy == ConflictStrategy.SKIP.value:
                        counts["skipped"] += 1
                    elif strategy == ConflictStrategy.OVERWRITE.value:
                        counts["overwritten"] += 1
                    else:
                        counts["failed"] += 1
                    continue

                if existed and strategy == ConflictStrategy.SKIP.value:
                    counts["skipped"] += 1
                    continue
                values = tuple(
                    denormalize_for_column(c, row.get(c)) for c in cols
                )
                cur.execute(insert_sql, values)
                if existed:
                    counts["overwritten"] += 1
                else:
                    counts["inserted"] += 1
            if not dry_run:
                cs_conn.commit()
        except Exception:
            if not dry_run:
                cs_conn.rollback()
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _pk_existed(
        self,
        table: str,
        pk_cols: tuple[str, ...],
        row: dict[str, Any],
    ) -> bool:
        """Pre-INSERT PK probe used only by the ``overwrite`` accounting path.

        For ``skip`` we rely on ``cur.rowcount``; for ``fail`` a conflict
        raises (so we never look at this).  For ``overwrite`` we cannot
        distinguish insert vs replace from ``rowcount`` alone (SQLite's
        ``INSERT OR REPLACE`` reports rowcount=1 in both cases).
        """
        sql = _pk_exists_sql(table, pk_cols, self._backend)
        cur = self._conn.cursor()
        try:
            cur.execute(sql, tuple(row.get(c) for c in pk_cols))
            return cur.fetchone() is not None
        finally:
            try:
                cur.close()
            except Exception:
                pass
