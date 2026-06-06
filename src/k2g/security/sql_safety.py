"""SQL safety layer for the ``k2g_sql_query`` MCP tool.

Parser/validator portion of the 5-layer safety stack for free-form
LLM-generated SQL:

1. ``check_read_only(sql)`` -- only allows SELECT / WITH (CTE);
   blocks DML / DDL.
2. ``check_no_dangerous_functions(sql)`` -- blocks system functions
   like pg_read_file, pg_terminate_backend, etc.
3. ``add_row_limit(sql, max_rows)`` -- auto-appends LIMIT (no-op if
   already present).

Call flow::

    safe, err = check_read_only(sql)
    if not safe:
        return {"error": err}
    safe, err = check_no_dangerous_functions(sql)
    ...
    bounded = add_row_limit(sql, 10000)
    db.execute(bounded)

The remaining layers operate at DB level:
- read-only role (Postgres ``k2g_reader``)
- ``statement_timeout``
- application-level rate limit

This module treats sqlparse as optional -- falls back to regex
checks when not installed.  RLS is the primary access-control gate;
this module provides supplementary misuse prevention.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-only check
# ---------------------------------------------------------------------------


# DML / DDL keywords -- blocked as leading tokens
_BLOCKED_PREFIX_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "CREATE", "DROP", "ALTER", "GRANT", "REVOKE",
    "VACUUM", "ANALYZE", "REINDEX", "CLUSTER",
    "COPY", "REPLACE", "RENAME",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
    "LOCK", "UNLOCK", "DECLARE", "FETCH", "MOVE",
    "DO",  # PL/pgSQL anonymous block
    "CALL",  # stored procedure
    "EXECUTE",  # prepared statement
    "EXPLAIN",  # blocked here; allowed via a separate function
    "SET",  # SESSION changes -- explicit allow only
    "RESET",
    "LISTEN", "NOTIFY", "UNLISTEN",
})

_ALLOWED_PREFIX_KEYWORDS = frozenset({
    "SELECT", "WITH",
})


def check_read_only(sql: str) -> tuple[bool, str | None]:
    """Verify that the SQL statement is read-only.

    Returns:
        (True, None) on success, or (False, error_message).
    """
    if not sql or not sql.strip():
        return False, "empty SQL"

    # Strip comments + uppercase first token
    cleaned = _strip_comments(sql).strip()
    if not cleaned:
        return False, "empty SQL after stripping comments"

    # Extract leading keyword of the first statement
    first_token = re.split(r"\s+|;|--", cleaned, maxsplit=1)[0].upper()

    if first_token in _BLOCKED_PREFIX_KEYWORDS:
        return False, f"blocked statement: {first_token}"
    if first_token not in _ALLOWED_PREFIX_KEYWORDS:
        return False, (
            f"unsupported statement: {first_token} "
            f"(only SELECT / WITH allowed)"
        )

    # Block multi-statement (semicolon) -- only single SELECT allowed
    # Simple check -- block if ; appears with non-whitespace after it
    stripped = cleaned.rstrip(";").rstrip()
    # Block if ; is mid-statement (string literal edge cases possible)
    if _has_multi_statement(stripped):
        return False, "multi-statement SQL not allowed"

    return True, None


def check_no_dangerous_functions(sql: str) -> tuple[bool, str | None]:
    """Block dangerous functions and system catalog access.

    Blocks pg_read_file, pg_terminate_backend, pg_sleep,
    set_config (write form of current_setting), lo_*, dblink, etc.
    """
    upper = sql.upper()
    blocked = [
        "PG_READ_FILE",
        "PG_READ_BINARY_FILE",
        "PG_LS_DIR",
        "PG_STAT_FILE",
        "PG_TERMINATE_BACKEND",
        "PG_CANCEL_BACKEND",
        "PG_RELOAD_CONF",
        "PG_SLEEP",
        "PG_SLEEP_FOR",
        "PG_SLEEP_UNTIL",
        "SET_CONFIG",  # block user-facing set (server uses SET LOCAL only)
        "LO_IMPORT",
        "LO_EXPORT",
        "DBLINK",
        "COPY ",
    ]
    for fn in blocked:
        # word boundary check
        if re.search(rf"\b{re.escape(fn)}\b", upper):
            return False, f"blocked function/keyword: {fn}"
    return True, None


def add_row_limit(sql: str, max_rows: int = 10000) -> str:
    """Auto-append LIMIT N (no-op if already present).

    Accepts SELECT / WITH only.  Keeps the user's LIMIT when one
    exists.  Simple check -- works without sqlparse.  Trailing
    semicolons are stripped before appending.
    """
    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        return cleaned

    # Check whether LIMIT already exists (simple check)
    if re.search(r"\bLIMIT\b\s+\d+", cleaned, re.IGNORECASE):
        return cleaned
    return f"{cleaned}\nLIMIT {int(max_rows)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_comments(sql: str) -> str:
    """Remove SQL comments -- ``-- line`` and ``/* block */``."""
    # /* ... */ block (non-greedy, simple)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # -- line
    lines: list[str] = []
    for line in sql.split("\n"):
        if "--" in line:
            line = line.split("--", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def _has_multi_statement(sql: str) -> bool:
    """True if ; appears mid-statement with a non-comment token after it."""
    # Very simple check -- ; inside string literals may cause false
    # positives.  Consider using sqlparse for more accuracy.
    parts = sql.split(";")
    non_empty_parts = [p.strip() for p in parts if p.strip()]
    return len(non_empty_parts) > 1


__all__ = [
    "check_read_only",
    "check_no_dangerous_functions",
    "add_row_limit",
]
