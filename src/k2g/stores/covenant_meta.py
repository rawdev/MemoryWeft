"""Backend-agnostic Covenant Metadata Store.

Successor to ``CovenantStore`` (legacy, SQLite-only with a separate
covenant.db file). Uses the ``k2g_covenant`` / ``k2g_covenant_history`` /
``k2g_source_file`` tables in the *main DB* (sqlite/postgres) — the schema
is created automatically at ``setup_schema`` time via
``_CREATE_COVENANT_META_TABLES_SQL`` in ``db_store/<backend>/schema.py``.

Public API is equivalent to ``CovenantStore``
(list / get / add / remove / set_enabled / list_history + source_file CRUD)
so ``covenant_validator`` / ``plan_policy`` can use either store without
modification.

Backend differences are limited to two points: placeholder (``?`` vs
``%s``) and JSON serialization (TEXT vs JSONB). Both are handled by
``__init__(conn, *, backend)``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from k2g.stores.covenant_store import VALID_GROUP_POLICIES, VALID_TYPES, CovenantRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """ISO 8601 UTC — compatible with both SQLite TEXT and Postgres TIMESTAMPTZ."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: Any) -> CovenantRecord:
    """Accept sqlite3.Row or dict (psycopg2 RealDictCursor) as input."""
    # row must support mapping-style access (both sqlite3.Row and dict do)
    config_raw = row["config_json"]
    if isinstance(config_raw, str):
        config = json.loads(config_raw)
    elif isinstance(config_raw, dict):
        # Postgres JSONB is already deserialized to dict
        config = config_raw
    else:
        config = {}
    enabled_raw = row["enabled"]
    enabled = bool(enabled_raw) if not isinstance(enabled_raw, bool) else enabled_raw
    created_at = row["created_at"]
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    updated_at = row["updated_at"]
    if isinstance(updated_at, datetime):
        updated_at = updated_at.isoformat()
    return CovenantRecord(
        id=row["id"],
        domain=row["domain"],
        source_id=row["source_id"],
        type=row["type"],
        config=config,
        group_policy=row["group_policy"],
        enabled=enabled,
        description=row["description"],
        created_at=created_at,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class CovenantMetaStore:
    """CRUD operations on the main DB covenant tables.

    Args:
        conn: An already-open DB connection.
            - sqlite3.Connection (``SqliteGraphStore._conn``)
            - psycopg2 connection (``PostgresGraphStore._conn``)
        backend: ``"sqlite"`` or ``"postgres"``. Used for placeholder and
            JSON serialization branching.

    Design notes:
        - ``setup_schema`` is assumed to have already been called (handled
          during graph_store init). This store is responsible only for
          runtime CRUD.
        - Transaction management is the caller's responsibility. Patterns
          using ``db.session()`` context auto-commit. Use explicit
          transactions where needed.
        - SQLite: the connection must be opened with
          ``check_same_thread=False`` for safe use in multi-threaded
          environments (FastAPI / MCP).
    """

    def __init__(
        self,
        conn: Any,
        *,
        backend: Literal["sqlite", "postgres"] = "sqlite",
    ) -> None:
        self._conn = conn
        self._backend = backend
        self._ph = "?" if backend == "sqlite" else "%s"
        logger.info("CovenantMetaStore initialized: backend=%s", backend)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _exec(self, sql: str, params: tuple = ()) -> Any:
        """Backend-agnostic execute. Caller must close the returned cursor."""
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def _serialize_config(self, config: dict[str, Any]) -> Any:
        """SQLite: JSON string. Postgres: psycopg2 Json wrapper."""
        if self._backend == "postgres":
            from psycopg2.extras import Json  # lazy import
            return Json(config)
        return json.dumps(config, ensure_ascii=False)

    def _commit(self) -> None:
        self._conn.commit()

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list(
        self,
        domain: str | None = None,
        enabled_only: bool = False,
    ) -> list[CovenantRecord]:
        sql = "SELECT * FROM k2g_covenant"
        clauses: list[str] = []
        params: list[Any] = []
        if domain is not None:
            clauses.append(f"domain = {self._ph}")
            params.append(domain)
        if enabled_only:
            if self._backend == "postgres":
                clauses.append("enabled = TRUE")
            else:
                clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY domain, source_id"

        cur = self._exec(sql, tuple(params))
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        return [_row_to_record(r) for r in rows]

    def get(self, domain: str, source_id: str) -> CovenantRecord | None:
        sql = (
            f"SELECT * FROM k2g_covenant "
            f"WHERE domain = {self._ph} AND source_id = {self._ph}"
        )
        cur = self._exec(sql, (domain, source_id))
        try:
            row = cur.fetchone()
        finally:
            cur.close()
        return _row_to_record(row) if row else None

    def list_history(
        self,
        domain: str | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM k2g_covenant_history"
        clauses: list[str] = []
        params: list[Any] = []
        if domain is not None:
            clauses.append(f"domain = {self._ph}")
            params.append(domain)
        if source_id is not None:
            clauses.append(f"source_id = {self._ph}")
            params.append(source_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"

        cur = self._exec(sql, tuple(params))
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        # SQLite Row → dict; Postgres RealDictCursor is already a dict
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, record: CovenantRecord) -> int:
        """Insert a covenant record and append an 'add' history entry."""
        if record.type not in VALID_TYPES:
            raise ValueError(
                f"invalid type: {record.type} (valid: {sorted(VALID_TYPES)})",
            )
        if record.group_policy not in VALID_GROUP_POLICIES:
            raise ValueError(
                f"invalid group_policy: {record.group_policy} "
                f"(valid: {sorted(VALID_GROUP_POLICIES)})",
            )

        now = _now()
        config_param = self._serialize_config(record.config)
        enabled_param = (
            record.enabled if self._backend == "postgres"
            else (1 if record.enabled else 0)
        )

        cur = self._conn.cursor()
        try:
            insert_sql = (
                f"INSERT INTO k2g_covenant "
                f"(domain, source_id, type, config_json, group_policy, "
                f" enabled, description, created_at, updated_at) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})"
            )
            if self._backend == "postgres":
                insert_sql += " RETURNING id"
            cur.execute(insert_sql, (
                record.domain,
                record.source_id,
                record.type,
                config_param,
                record.group_policy,
                enabled_param,
                record.description,
                now,
                now,
            ))
            if self._backend == "postgres":
                row = cur.fetchone()
                # RealDictCursor → dict / plain cursor → tuple
                new_id = row["id"] if isinstance(row, dict) else row[0]
            else:
                new_id = cur.lastrowid
            self._append_history(
                cur, new_id, "add", record.domain, record.source_id,
                before=None, after=record.config,
            )
            self._commit()
        except (sqlite3.IntegrityError, Exception) as e:
            self._rollback()
            # Unify IntegrityError handling (psycopg2.IntegrityError is also caught)
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError(
                    f"covenant already exists: domain={record.domain} "
                    f"source_id={record.source_id}",
                ) from e
            raise
        finally:
            cur.close()

        assert new_id is not None
        return int(new_id)

    def remove(self, domain: str, source_id: str) -> bool:
        existing = self.get(domain, source_id)
        if existing is None:
            return False

        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM k2g_covenant WHERE domain = {self._ph} "
                f"AND source_id = {self._ph}",
                (domain, source_id),
            )
            self._append_history(
                cur, existing.id, "remove", domain, source_id,
                before=existing.config, after=None,
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()
        return True

    def set_enabled(
        self,
        domain: str,
        source_id: str,
        enabled: bool,
    ) -> bool:
        existing = self.get(domain, source_id)
        if existing is None:
            return False
        if existing.enabled == enabled:
            return True  # no-op

        now = _now()
        enabled_param = (
            enabled if self._backend == "postgres" else (1 if enabled else 0)
        )

        cur = self._conn.cursor()
        try:
            cur.execute(
                f"UPDATE k2g_covenant SET enabled = {self._ph}, "
                f"updated_at = {self._ph} "
                f"WHERE domain = {self._ph} AND source_id = {self._ph}",
                (enabled_param, now, domain, source_id),
            )
            action = "enable" if enabled else "disable"
            self._append_history(
                cur, existing.id, action, domain, source_id,
                before=existing.config, after=existing.config,
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()
        return True

    # ------------------------------------------------------------------
    # SourceFile CRUD
    # ------------------------------------------------------------------

    def create_source_file(self, record: dict[str, Any]) -> None:
        """Upsert a SourceFile record (keyed on id)."""
        cur = self._conn.cursor()
        try:
            if self._backend == "sqlite":
                sql = (
                    "INSERT OR REPLACE INTO k2g_source_file "
                    "(id, covenant_id, relative_path, content_hash, "
                    " size_bytes, mime_type, last_ingested, last_vcs_sha, "
                    " extractor_key, unit_strategy, domain) "
                    f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                    f"{self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                    f"{self._ph}, {self._ph}, "
                    # domain — covenant-derived tenant axis (RLS_TABLES parity)
                    f"(SELECT domain FROM k2g_covenant "
                    f"WHERE CAST(id AS TEXT) = {self._ph}))"
                )
            else:
                # Postgres: ON CONFLICT (id) DO UPDATE
                sql = (
                    "INSERT INTO k2g_source_file "
                    "(id, covenant_id, relative_path, content_hash, "
                    " size_bytes, mime_type, last_ingested, last_vcs_sha, "
                    " extractor_key, unit_strategy, domain) "
                    f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                    f"{self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                    f"{self._ph}, {self._ph}, "
                    # domain — covenant-derived tenant axis (RLS_TABLES parity)
                    f"(SELECT domain FROM k2g_covenant "
                    f"WHERE id::text = {self._ph})) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  covenant_id   = EXCLUDED.covenant_id, "
                    "  relative_path = EXCLUDED.relative_path, "
                    "  content_hash  = EXCLUDED.content_hash, "
                    "  size_bytes    = EXCLUDED.size_bytes, "
                    "  mime_type     = EXCLUDED.mime_type, "
                    "  last_ingested = EXCLUDED.last_ingested, "
                    "  last_vcs_sha  = EXCLUDED.last_vcs_sha, "
                    "  extractor_key = EXCLUDED.extractor_key, "
                    "  unit_strategy = EXCLUDED.unit_strategy, "
                    "  domain        = EXCLUDED.domain"
                )
            cur.execute(sql, (
                record["id"],
                record["covenant_id"],
                record["relative_path"],
                record["content_hash"],
                record.get("size_bytes", 0),
                record.get("mime_type", ""),
                record["last_ingested"],
                record.get("last_vcs_sha"),
                record.get("extractor_key", "default"),
                record.get("unit_strategy", "whole_file"),
                record["covenant_id"],  # for the domain subquery above
            ))
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()

    def get_source_files_by_covenant(
        self,
        covenant_id: str,
    ) -> list[dict[str, Any]]:
        cur = self._exec(
            f"SELECT * FROM k2g_source_file WHERE covenant_id = {self._ph}",
            (covenant_id,),
        )
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        return [dict(r) for r in rows]

    def update_source_file_hash(
        self,
        sf_id: str,
        content_hash: str,
        last_ingested: str,
    ) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"UPDATE k2g_source_file SET content_hash = {self._ph}, "
                f"last_ingested = {self._ph} WHERE id = {self._ph}",
                (content_hash, last_ingested, sf_id),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()

    def delete_source_file(self, sf_id: str) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM k2g_source_file WHERE id = {self._ph}",
                (sf_id,),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append_history(
        self,
        cur: Any,
        covenant_id: int | None,
        action: str,
        domain: str,
        source_id: str,
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        before_param = (
            None if before is None else self._serialize_config(before)
        )
        after_param = (
            None if after is None else self._serialize_config(after)
        )
        cur.execute(
            f"INSERT INTO k2g_covenant_history "
            f"(covenant_id, action, domain, source_id, before_json, "
            f" after_json, acted_at) "
            f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph})",
            (
                covenant_id,
                action,
                domain,
                source_id,
                before_param,
                after_param,
                _now(),
            ),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def covenant_meta_store_for(graph_store: Any) -> CovenantMetaStore:
    """Convenience factory — auto-detects backend and connection from graph_store.

    Args:
        graph_store: A ``SqliteGraphStore`` or ``PostgresGraphStore`` instance
            with a ``_conn`` attribute. The existing connection is reused.
    """
    cls_name = type(graph_store).__name__
    if "Sqlite" in cls_name:
        backend = "sqlite"
    elif "Postgres" in cls_name:
        backend = "postgres"
    else:
        raise ValueError(
            f"unknown graph_store type: {cls_name} "
            "(expected SqliteGraphStore or PostgresGraphStore)",
        )
    conn = getattr(graph_store, "_conn", None)
    if conn is None:
        raise AttributeError(
            f"{cls_name} has no _conn attribute — "
            "verify that graph_store has been initialized",
        )
    return CovenantMetaStore(conn, backend=backend)


__all__ = ["CovenantMetaStore", "covenant_meta_store_for"]
