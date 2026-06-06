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


class ConflictStrategy(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    FAIL = "fail"


_VALID_STRATEGIES = {s.value for s in ConflictStrategy}


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
    ) -> dict[str, Any]:
        """Import all Tier-1 rows.

        Returns::

            {
              "manifest":  ArchiveManifest,
              "dry_run":   bool,
              "strategy":  str,
              "results":   {
                  table: {"inserted": N, "skipped": N,
                          "overwritten": N, "failed": N},
                  ...
              },
            }

        When ``dry_run`` is True, ``inserted`` / ``skipped`` / ``overwritten``
        report what *would* have happened — no DB mutation occurs.
        """
        if strategy not in _VALID_STRATEGIES:
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
            "strategy": strategy,
            "results": results,
        }

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
