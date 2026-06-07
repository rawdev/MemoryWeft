"""Backend-agnostic SQL execution helpers for the Manager web routes.

Background: the web routes used `db.graph._conn.execute(...)` (a SQLite convenience
method) directly, but **a psycopg2 connection has no `.execute()`** (only `.cursor()`)
-> AttributeError everywhere on Postgres. Also PG's default `RealDictCursor` yields
dict rows, which breaks `row[0]` index access.

Fix: these helpers **force a `DictCursor` on PG** (DictRow — supports both index and
key access, the same shape as sqlite3.Row). So a call site only needs to swap
`conn.execute(...)` -> `q(conn, ...)`, and the existing row access (both index and
key) and placeholder (_ph) code keeps working on both SQLite and Postgres.
"""
from __future__ import annotations

from typing import Any


def _is_pg(conn: Any) -> bool:
    # A psycopg2 connection's module is ``psycopg2.extensions`` (no "postgres"
    # substring), so detect the driver instead. Getting this wrong skips the
    # DictCursor override below → the connection's RealDictCursor leaks through →
    # ``row[0]`` index access raises KeyError on every web SQL route.
    return "psycopg2" in type(conn).__module__.lower()


def cursor(conn: Any):  # noqa: ANN201
    """Backend-agnostic cursor. PG forces a DictCursor (index+key access both work)."""
    if _is_pg(conn):
        import psycopg2.extras

        return conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    return conn.cursor()


def is_pg(conn: Any) -> bool:
    """Public alias — True when ``conn`` is a psycopg2 (Postgres) connection."""
    return _is_pg(conn)


def ph(conn: Any) -> str:
    """Param placeholder for this backend: ``%s`` (psycopg2) or ``?`` (sqlite).

    Detect on the *connection* via the driver — NOT ``"postgres" in
    type(conn).__module__``: a psycopg2 conn's module is ``psycopg2.extensions``
    (no "postgres" substring) so that check always picks ``?`` and Postgres then
    rejects it with ``syntax error at or near "AND"``.
    """
    return "%s" if _is_pg(conn) else "?"


def not_deprecated(col: str, conn: Any) -> str:
    """Backend-correct ``<col> is not deprecated`` SQL predicate.

    Postgres stores ``deprecated`` as a real boolean → ``= 0`` / ``= 'f'`` raise
    ``operator does not exist: boolean = integer``; use ``IS NOT TRUE`` (matches
    FALSE and NULL). SQLite stores 0/1 or legacy ``'f'``/``'t'`` strings, so
    enumerate the falsy forms (``IS NOT TRUE`` would wrongly keep ``'t'``).
    """
    if _is_pg(conn):
        return f"({col} IS NOT TRUE)"
    return f"({col} IS NULL OR {col} IN (0, 'f', 'false', 'FALSE'))"


def q(conn: Any, sql: str, args: tuple = ()):  # noqa: ANN201
    """Replacement for ``conn.execute`` — returns the cursor after running.

    Consume it with .fetchone()/.fetchall()/iteration, as before.
    psycopg2 has no connection.execute(), so this goes through a cursor. The returned
    cursor is consumed then discarded by the caller (request scope). Same semantics as
    sqlite3 ``conn.execute``.

    NOTE: the returned cursor is **not closed** — the caller is responsible. To avoid
    accumulation, prefer ``q_one``/``q_all``/``q_exec`` (which close the cursor after
    running) for reads/writes consumed in one shot.
    """
    cur = cursor(conn)
    cur.execute(sql, args)
    return cur


def q_one(conn: Any, sql: str, args: tuple = ()):  # noqa: ANN201
    """SELECT -> first row (None if absent). **Closes the cursor immediately**.

    Avoids request-scope cursor accumulation.
    A read-only helper that does not touch the transaction (no rollback) — web routes
    mix read/write in one tx and commit at the end, so a rollback here could discard a
    pending write. The rows are already fetched, so closing is enough.
    """
    cur = cursor(conn)
    try:
        cur.execute(sql, args)
        return cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def q_all(conn: Any, sql: str, args: tuple = ()) -> list:
    """SELECT -> list of all rows (iterable). **Closes the cursor**. (read-only — see q_one.)"""
    cur = cursor(conn)
    try:
        cur.execute(sql, args)
        return list(cur.fetchall())
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def q_exec(conn: Any, sql: str, args: tuple = ()) -> int:
    """Run a write/DDL -> rowcount. **Closes the cursor**. commit is the caller's responsibility."""
    cur = cursor(conn)
    try:
        cur.execute(sql, args)
        return cur.rowcount
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def q_rollback(conn: Any) -> None:
    """Roll back a Postgres connection left in an aborted transaction.

    A shared psycopg2 connection (``autocommit=False``) is poisoned by any failed
    statement: every later command then raises ``InFailedSqlTransaction`` until a
    rollback. Call this before standalone DDL and in route error handlers so one
    failed statement does not break later requests. PG-only + best-effort, so it
    never discards a pending SQLite write transaction.
    """
    if not _is_pg(conn):
        return
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "q", "q_one", "q_all", "q_exec", "q_rollback",
    "cursor", "is_pg", "ph", "not_deprecated",
]
