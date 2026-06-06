"""LLM free-form SQL MCP tool handlers.

Core idea: an LLM (Claude etc.) inspects the schema and issues arbitrary
SELECT/WITH queries against K2G.  RLS policies (Postgres) or query_filter
(SQLite) automatically block rows outside the caller's permission scope.

Three tools:
- ``k2g_sql_query(sql)``     — execute a SELECT/WITH query written by the LLM
- ``k2g_describe_schema()``  — expose all K2G tables / columns / relations
- ``k2g_explain_query(sql)`` — return EXPLAIN output for LLM query debugging

Five safety layers:
1. RLS / app-level filter (DB-level access control)
2. Read-only validation (sql_safety.check_read_only)
3. Dangerous function block (sql_safety.check_no_dangerous_functions)
4. Row limit (sql_safety.add_row_limit)
5. statement_timeout (Postgres only, query_timeout_ms)
"""

from __future__ import annotations

import logging
from typing import Any

from k2g.mcp.contracts import match_active_contracts
from k2g.mcp.factory import Deps
from k2g.security.session_context import get_session_context
from k2g.security.sql_safety import (
    add_row_limit,
    check_no_dangerous_functions,
    check_read_only,
)

logger = logging.getLogger(__name__)

DEFAULT_ROW_LIMIT = 10000
DEFAULT_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# k2g_sql_query
# ---------------------------------------------------------------------------


def sql_query_tool(
    deps: Deps,
    *,
    sql: str,
    max_rows: int = DEFAULT_ROW_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """Execute a free-form SQL query submitted by the LLM.

    Args:
        deps: MCP Deps (requires graph).
        sql: SELECT / WITH SQL statement.
        max_rows: LIMIT automatically appended when not already present.
        timeout_ms: Postgres ``statement_timeout`` (ms).

    Returns:
        ``{"rows": [...], "columns": [...], "row_count": N}`` on success, or
        ``{"error": "...", "blocked_by": "..."}`` on rejection/error.

    Access control is handled automatically by RLS / query_filter.
    This function enforces only the misuse-prevention safety layers.

    Execution results, rejections, and errors are all recorded in the
    k2g_sql_audit table. Audit INSERT failures are logged as warnings and
    do not affect the query result.
    """
    import time as _time
    audit_t0 = _time.perf_counter()
    audit_status = "success"
    audit_error_class: str | None = None
    audit_error_detail: str | None = None
    audit_row_count: int | None = None

    # 1. Read-only check
    ok, err = check_read_only(sql)
    if not ok:
        audit_status = "rejected"
        audit_error_class = "read_only_check"
        audit_error_detail = err
        _audit_sql(deps, sql=sql, status=audit_status,
                   error_class=audit_error_class, error_detail=audit_error_detail,
                   row_count=None,
                   duration_ms=int((_time.perf_counter() - audit_t0) * 1000))
        return {"error": f"SQL rejected: {err}", "blocked_by": "read_only_check"}

    # 2. Block dangerous functions.
    ok, err = check_no_dangerous_functions(sql)
    if not ok:
        audit_status = "rejected"
        audit_error_class = "dangerous_function_check"
        audit_error_detail = err
        _audit_sql(deps, sql=sql, status=audit_status,
                   error_class=audit_error_class, error_detail=audit_error_detail,
                   row_count=None,
                   duration_ms=int((_time.perf_counter() - audit_t0) * 1000))
        return {
            "error": f"SQL rejected: {err}",
            "blocked_by": "dangerous_function_check",
        }

    # 3. Automatically append row limit.
    bounded_sql = add_row_limit(sql, max_rows=max_rows)

    # 4. Check session context (for debugging).
    ctx = get_session_context()
    if ctx is None or ctx.user_id is None:
        logger.debug("k2g_sql_query: unauthenticated context — RLS determines access")

    # 5. Execute
    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    conn = graph._conn

    cur = conn.cursor()
    try:
        # Postgres — statement_timeout
        if backend == "postgres":
            cur.execute(
                f"SET LOCAL statement_timeout = {int(timeout_ms)}",
            )

        cur.execute(bounded_sql)
        rows = cur.fetchall()
        # Column names
        if cur.description:
            columns = [d[0] for d in cur.description]
        else:
            columns = []
        # Normalise rows to dicts
        out_rows: list[dict[str, Any]] = []
        for r in rows:
            if hasattr(r, "keys"):
                out_rows.append(dict(r))
            elif isinstance(r, dict):
                out_rows.append(r)
            else:
                out_rows.append(dict(zip(columns, r)))
        audit_row_count = len(out_rows)
        # Automatically attach active contracts to the response to prevent
        # negative LLM inference. Matches touched tables / columns /
        # result patterns (up to 3 contracts). Omit the context key entirely
        # when the list is empty to avoid bloating the payload.
        active_contracts = match_active_contracts(
            sql=bounded_sql, columns=columns, rows=out_rows,
        )
        response: dict[str, Any] = {
            "rows": out_rows,
            "columns": columns,
            "row_count": len(out_rows),
            "executed_sql": bounded_sql,
        }
        if active_contracts:
            response["context"] = {"active_contracts": active_contracts}
        return response
    except Exception as exc:  # noqa: BLE001
        audit_status = "error"
        audit_error_class = type(exc).__name__
        audit_error_detail = str(exc)[:1000]
        return {
            "error": str(exc),
            "blocked_by": "execution_error",
            "executed_sql": bounded_sql,
        }
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
        # Always clean up the transaction — a rollback is sufficient for
        # read-only queries.  SET LOCAL statement_timeout starts an implicit
        # transaction, so cleanup is required regardless of success or failure.
        # Omitting this causes the next query to fail permanently with
        # "current transaction is aborted".
        _safe_rollback(conn, backend, where="sql_query_tool")
        _audit_sql(deps, sql=bounded_sql, status=audit_status,
                   error_class=audit_error_class, error_detail=audit_error_detail,
                   row_count=audit_row_count,
                   duration_ms=int((_time.perf_counter() - audit_t0) * 1000))


def _safe_rollback(conn: Any, backend: str, *, where: str) -> None:
    """Clean up a transaction — rollback only when in abort or intrans state.

    Prevents permanent blocking by
    "current transaction is aborted, commands ignored until end of transaction
    block". Called from the ``finally`` block of read-only tools.

    Postgres only. SQLite relies on cursor close (autocommit mode assumed).
    """
    if backend != "postgres":
        return
    try:
        from psycopg2.extensions import (
            TRANSACTION_STATUS_INERROR,
            TRANSACTION_STATUS_INTRANS,
        )
        ts = conn.info.transaction_status
        if ts in (TRANSACTION_STATUS_INTRANS, TRANSACTION_STATUS_INERROR):
            conn.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("safe_rollback failed at %s", where)


def _audit_sql(
    deps: Deps, *,
    sql: str,
    status: str,
    error_class: str | None,
    error_detail: str | None,
    row_count: int | None,
    duration_ms: int,
) -> None:
    """Insert one row into k2g_sql_audit. Failures are logged as warnings."""
    try:
        graph = deps.graph
        backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
        ph = "%s" if backend == "postgres" else "?"
        actor_id = None
        try:
            ctx = get_session_context()
            actor_id = getattr(ctx, "user_id", None) if ctx else None
        except Exception:  # noqa: BLE001
            pass
        domain = None
        try:
            ctx = get_session_context()
            domain = getattr(ctx, "home_domain", None) if ctx else None
        except Exception:  # noqa: BLE001
            pass
        conn = graph._conn
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO k2g_sql_audit (actor_id, domain, query_sql, "
                " row_count, duration_ms, status, error_class, error_detail) "
                f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
                (actor_id, domain, sql[:5000], row_count, duration_ms,
                 status, error_class, error_detail),
            )
            conn.commit()
        except Exception:
            # On INSERT failure, immediately clean up the transaction to protect
            # the next query from an aborted-transaction cascade.
            _safe_rollback(conn, backend, where="audit_sql_insert")
            raise
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        # Successful call — trace at DEBUG to avoid log bloat (high volume)
        if status == "ok":
            logger.debug("mcp_sql", extra={
                "row_count": row_count, "duration_ms": duration_ms,
                "actor": actor_id, "domain": domain,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_sql_audit_insert_failed", extra={
            "err": str(exc)[:200],
        })


# ---------------------------------------------------------------------------
# k2g_describe_schema
# ---------------------------------------------------------------------------


def describe_schema_tool(deps: Deps) -> dict[str, Any]:
    """Return all K2G tables and columns for LLM schema inspection.

    Supports both SQLite (sqlite_master) and Postgres (information_schema).
    """
    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    conn = graph._conn
    cur = conn.cursor()
    try:
        if backend == "sqlite":
            return _describe_sqlite(cur)
        else:
            return _describe_postgres(cur)
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
        # Clean up the transaction — schema metadata queries can also abort
        # (e.g. on insufficient permissions).
        _safe_rollback(conn, backend, where="describe_schema_tool")


def _describe_sqlite(cur: Any) -> dict[str, Any]:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )
    tables = [r["name"] if hasattr(r, "keys") else r[0] for r in cur.fetchall()]

    schema: list[dict[str, Any]] = []
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        columns = []
        for col in cur.fetchall():
            # PRAGMA: cid, name, type, notnull, dflt_value, pk
            col_dict = (
                dict(col) if hasattr(col, "keys")
                else {
                    "cid": col[0], "name": col[1], "type": col[2],
                    "notnull": col[3], "dflt_value": col[4], "pk": col[5],
                }
            )
            columns.append({
                "name": col_dict["name"],
                "type": col_dict["type"],
                "not_null": bool(col_dict.get("notnull", 0)),
                "default": col_dict.get("dflt_value"),
                "primary_key": bool(col_dict.get("pk", 0)),
            })
        schema.append({"table": table, "columns": columns})
    return {"backend": "sqlite", "tables": schema, "table_count": len(schema)}


def _describe_postgres(cur: Any) -> dict[str, Any]:
    """Fetch all public tables and columns via a single JOIN query.

    The previous N+1 pattern (one query for the table list, then one per
    table) caused 10-30 s hangs in environments with 70+ tables and a
    Supabase transaction pooler due to accumulated round-trips. Switching to
    a single query reduces this to one round-trip.
    """
    cur.execute(
        "SET LOCAL statement_timeout = 30000",
    )
    cur.execute("""
        SELECT t.table_name,
               c.column_name,
               c.data_type,
               c.is_nullable,
               c.column_default,
               c.ordinal_position
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema
         AND c.table_name  = t.table_name
        WHERE t.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
        ORDER BY t.table_name, c.ordinal_position
    """)
    rows = cur.fetchall()

    # Group by table_name in memory (all rows fetched in one query)
    schema_by_table: dict[str, list[dict[str, Any]]] = {}
    table_order: list[str] = []
    for r in rows:
        if isinstance(r, dict):
            d = r
        elif hasattr(r, "keys"):
            d = dict(r)
        else:
            d = {
                "table_name": r[0], "column_name": r[1], "data_type": r[2],
                "is_nullable": r[3], "column_default": r[4],
                "ordinal_position": r[5],
            }
        tbl = d["table_name"]
        if tbl not in schema_by_table:
            schema_by_table[tbl] = []
            table_order.append(tbl)
        schema_by_table[tbl].append({
            "name": d["column_name"],
            "type": d["data_type"],
            "not_null": d["is_nullable"] == "NO",
            "default": d["column_default"],
        })

    schema = [
        {"table": tbl, "columns": schema_by_table[tbl]}
        for tbl in table_order
    ]
    return {"backend": "postgres", "tables": schema, "table_count": len(schema)}


# ---------------------------------------------------------------------------
# k2g_explain_query
# ---------------------------------------------------------------------------


def explain_query_tool(deps: Deps, *, sql: str) -> dict[str, Any]:
    """Return EXPLAIN output for LLM query efficiency debugging."""
    ok, err = check_read_only(sql)
    if not ok:
        return {"error": f"SQL rejected: {err}"}
    ok, err = check_no_dangerous_functions(sql)
    if not ok:
        return {"error": f"SQL rejected: {err}"}

    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    conn = graph._conn
    cur = conn.cursor()
    try:
        if backend == "sqlite":
            cur.execute(f"EXPLAIN QUERY PLAN {sql}")
        else:
            cur.execute(f"EXPLAIN {sql}")
        rows = cur.fetchall()
        out = [dict(r) if hasattr(r, "keys") else
               {"row": list(r)} for r in rows]
        return {"plan": out, "backend": backend}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
        # Clean up the transaction to prevent an abort state from persisting
        # after an EXPLAIN failure.
        _safe_rollback(conn, backend, where="explain_query_tool")


__all__ = [
    "sql_query_tool",
    "describe_schema_tool",
    "explain_query_tool",
    "DEFAULT_ROW_LIMIT",
    "DEFAULT_TIMEOUT_MS",
]
