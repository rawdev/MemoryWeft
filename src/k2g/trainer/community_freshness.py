"""Community freshness: derived version + OCC pointer + read resolver.

A helper layer that lets multiple clients safely keep the community snapshot
up to date without a dedicated worker (OSS + Postgres).

- **The version is derived from events** (per-domain ``COUNT(*)`` +
  ``MAX(created_at)``). No counter row is kept, so the **insert path is untouched
  and there is no hot-row contention**. With append-only ingest the count is
  monotonically increasing.
- **OCC pointer** ``community_state(kind, domain)`` points at the "current valid
  run" and the base version that run was built on. A write **advances the pointer
  only when its snapshot is newer** (atomic conditional UPSERT) — preventing
  regression.
- **read resolver** ``resolve_latest_run`` — consumers (hint / summary / domain
  summary) read the pointer first, falling back gracefully to
  ``train_run_latest_completed`` (finished_at) when absent.

Backend-agnostic: uses ``db.graph._conn`` directly. **A psycopg2 connection has no
``.execute()`` and supports only ``.cursor()``**, so every access goes through
``conn.cursor()`` (sqlite3 supports it too).

domain convention: ``None`` = cross-domain (all). Internally normalized to the
empty string ``''`` for stable PK storage (avoids NULL PK comparison).
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_KIND = {"entity": "leiden_entity", "event": "leiden_event"}

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS community_state (
    kind          TEXT    NOT NULL,
    domain        TEXT    NOT NULL DEFAULT '',
    latest_run_id TEXT,
    base_count    INTEGER NOT NULL DEFAULT 0,
    base_max_ts   TEXT    NOT NULL DEFAULT '',
    computed_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, domain)
)
"""

_DDL_PG = """
CREATE TABLE IF NOT EXISTS community_state (
    kind          VARCHAR(32)  NOT NULL,
    domain        VARCHAR(128) NOT NULL DEFAULT '',
    latest_run_id VARCHAR(64),
    base_count    BIGINT       NOT NULL DEFAULT 0,
    base_max_ts   TEXT         NOT NULL DEFAULT '',
    computed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, domain)
)
"""


def _is_pg(conn: Any) -> bool:
    # A psycopg2 connection's module is ``psycopg2.extensions`` (no "postgres"
    # substring) — detect the driver instead. The old ``"postgres" in
    # __module__`` always picked ``?`` + SQLite DDL on Postgres, so advance_pointer
    # threw ``syntax error`` every call and the OCC pointer never advanced.
    # Canonical detector: see ``k2g.web.routes._sql._is_pg``.
    return "psycopg2" in type(conn).__module__.lower()


def _ph(conn: Any) -> str:
    return "%s" if _is_pg(conn) else "?"


def _norm_domain(domain: str | None) -> str:
    return domain if domain else ""


def _row_get(row: Any, idx: int, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "keys"):  # sqlite3.Row
        try:
            return row[key]
        except Exception:  # noqa: BLE001
            return row[idx]
    return row[idx]


def _exec(
    conn: Any, sql: str, args: tuple = (), *, fetch: str = "none",
) -> tuple[Any, int]:
    """cursor-based execution — common to psycopg2/sqlite3 (no connection.execute()).

    fetch ∈ {'none','one','all'}. Returns (result, rowcount). The cursor is closed.
    Committing a write (fetch='none') is the caller's responsibility. **A read
    (fetch≠'none') closes the transaction after running on PG** (with
    autocommit=False even a SELECT opens a tx; left uncommitted on a shared,
    long-lived connection [desktop app / MCP] it accumulates "idle in transaction"
    -> interferes with lock/VACUUM). The rows are already fetched, so rollback is safe.
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, args)
        if fetch == "one":
            res: Any = cur.fetchone()
        elif fetch == "all":
            res = cur.fetchall()
        else:
            res = None
        rc = cur.rowcount
        if fetch != "none" and _is_pg(conn):
            _safe_rollback(conn)
        return res, rc
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def ensure_table(conn: Any) -> None:
    """Lazily create community_state."""
    _exec(conn, _DDL_PG if _is_pg(conn) else _DDL_SQLITE)
    if hasattr(conn, "commit"):
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            pass


def derive_version(conn: Any, domain: str | None) -> tuple[int, str]:
    """(count, max_created_at) derived from events. Assumes monotonic (append-only).

    domain=None -> all events. On failure (0, ""). max_created_at is an ISO string,
    so lexical comparison matches chronological order.
    """
    # CAST(... AS TEXT): on PG ``created_at`` is TIMESTAMPTZ, so ``COALESCE(MAX(..), '')``
    # fails the timestamptz<->'' cast (InvalidDatetimeFormat) and the whole SELECT
    # raises -> except returns (0,'') -> is_stale always False -> **auto-recompute was
    # silently dead on PG**. Casting to text first works on both SQLite (TEXT) and
    # PG (TIMESTAMPTZ).
    if domain:
        sql = (
            "SELECT COUNT(*) AS n, COALESCE(CAST(MAX(created_at) AS TEXT), '') AS m "
            f"FROM events WHERE domain = {_ph(conn)}"
        )
        args: tuple = (domain,)
    else:
        sql = "SELECT COUNT(*) AS n, COALESCE(CAST(MAX(created_at) AS TEXT), '') AS m FROM events"
        args = ()
    try:
        row, _ = _exec(conn, sql, args, fetch="one")
    except Exception as exc:  # noqa: BLE001
        logger.debug("derive_version failed (domain=%s): %s", domain, exc)
        if _is_pg(conn):
            _safe_rollback(conn)
        return (0, "")
    n = _row_get(row, 0, "n") or 0
    m = _row_get(row, 1, "m") or ""
    return (int(n), str(m))


def _safe_rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


def get_pointer(conn: Any, kind: str, domain: str | None) -> dict[str, Any] | None:
    """One community_state pointer row. None if absent."""
    sql = (
        "SELECT latest_run_id, base_count, base_max_ts, computed_at "
        f"FROM community_state WHERE kind = {_ph(conn)} AND domain = {_ph(conn)}"
    )
    try:
        ensure_table(conn)
        row, _ = _exec(
            conn, sql, (_KIND.get(kind, kind), _norm_domain(domain)), fetch="one",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_pointer failed: %s", exc)
        if _is_pg(conn):
            _safe_rollback(conn)
        return None
    if row is None:
        return None
    return {
        "latest_run_id": _row_get(row, 0, "latest_run_id"),
        "base_count": int(_row_get(row, 1, "base_count") or 0),
        "base_max_ts": str(_row_get(row, 2, "base_max_ts") or ""),
        "computed_at": _row_get(row, 3, "computed_at"),
    }


def is_stale(conn: Any, kind: str, domain: str | None) -> bool:
    """Stale when the current derived version differs from the pointer's base version.

    No pointer (never computed) + events>0 -> stale=True. events=0 + no pointer -> False.
    """
    cur_count, cur_ts = derive_version(conn, domain)
    ptr = get_pointer(conn, kind, domain)
    if ptr is None:
        return cur_count > 0
    return (cur_count, cur_ts) != (ptr["base_count"], ptr["base_max_ts"])


def set_pointer(
    conn: Any,
    kind: str,
    domain: str | None,
    *,
    run_id: str,
    base_count: int,
    base_max_ts: str,
) -> bool:
    """Unconditionally point ``community_state`` at ``run_id`` (manual recompute).

    Unlike :func:`advance_pointer`'s monotonic OCC CAS — which only advances when
    the data version (``base_count`` / ``base_max_ts``) grows — this overwrites
    the pointer regardless. A manual recompute is an explicit "make THIS
    clustering the canonical view" action: e.g. changing ``theta_e`` on unchanged
    data, where the CAS would otherwise refuse (same data version) and leave the
    new run invisible to every pointer-based read (:func:`resolve_latest_run`).

    The current data version is still stamped so later ingest-triggered staleness
    checks stay correct. Returns True on success.
    """
    ph = _ph(conn)
    now_fn = "NOW()" if _is_pg(conn) else "CURRENT_TIMESTAMP"
    sql = (
        "INSERT INTO community_state "
        "(kind, domain, latest_run_id, base_count, base_max_ts, computed_at) "
        f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {now_fn}) "
        "ON CONFLICT (kind, domain) DO UPDATE SET "
        "  latest_run_id = EXCLUDED.latest_run_id, "
        "  base_count    = EXCLUDED.base_count, "
        "  base_max_ts   = EXCLUDED.base_max_ts, "
        f"  computed_at   = {now_fn}"
    )
    try:
        ensure_table(conn)
        _exec(
            conn, sql,
            (_KIND.get(kind, kind), _norm_domain(domain), run_id,
             int(base_count), str(base_max_ts)),
        )
        if hasattr(conn, "commit"):
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_pointer failed (kind=%s domain=%s): %s",
                       kind, domain, exc)
        if _is_pg(conn):
            _safe_rollback(conn)
        return False


def advance_pointer(
    conn: Any,
    kind: str,
    domain: str | None,
    *,
    run_id: str,
    base_count: int,
    base_max_ts: str,
) -> bool:
    """OCC CAS — advance the pointer only when my snapshot is newer (atomic conditional UPSERT).

    Returns True when the pointer advances to this run (this result is valid). False
    when a newer result already exists, so this result is discardable (the
    train_run/assignment is kept but the pointer does not point at it).

    Monotonic comparison: base_count first, then base_max_ts (ISO) lexically when
    equal. Both Postgres and SQLite support ``ON CONFLICT ... DO UPDATE ... WHERE``
    (referencing excluded) -> a single atomic statement.
    """
    ph = _ph(conn)
    now_fn = "NOW()" if _is_pg(conn) else "CURRENT_TIMESTAMP"
    sql = (
        "INSERT INTO community_state "
        "(kind, domain, latest_run_id, base_count, base_max_ts, computed_at) "
        f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {now_fn}) "
        "ON CONFLICT (kind, domain) DO UPDATE SET "
        "  latest_run_id = EXCLUDED.latest_run_id, "
        "  base_count    = EXCLUDED.base_count, "
        "  base_max_ts   = EXCLUDED.base_max_ts, "
        f"  computed_at   = {now_fn} "
        "WHERE EXCLUDED.base_count > community_state.base_count "
        "   OR (EXCLUDED.base_count = community_state.base_count "
        "       AND EXCLUDED.base_max_ts > community_state.base_max_ts)"
    )
    try:
        ensure_table(conn)
        _, rowcount = _exec(
            conn, sql,
            (_KIND.get(kind, kind), _norm_domain(domain), run_id,
             int(base_count), str(base_max_ts)),
        )
        if hasattr(conn, "commit"):
            conn.commit()
        return rowcount != 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("advance_pointer failed (kind=%s domain=%s): %s",
                       kind, domain, exc)
        if _is_pg(conn):
            _safe_rollback(conn)
        return False


def _advisory_key(kind: str, domain: str | None) -> int:
    """Deterministic signed 64-bit key for ``pg_advisory_lock``.

    blake2b — NOT builtin ``hash()``, which is per-process randomized
    (PYTHONHASHSEED) and would make independent processes contend on *different*
    keys (= no mutual exclusion). A stable digest makes every ingest client map
    the same (kind, domain) to the same lock. ``signed=True`` keeps it in PG's
    ``bigint`` range.
    """
    digest = hashlib.blake2b(
        f"mweft-community-leiden:{_KIND.get(kind, kind)}:{_norm_domain(domain)}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def recompute_lock(conn: Any, kind: str, domain: str | None):
    """Cross-process best-effort herd guard for community recompute.

    Postgres: **session-level** ``pg_try_advisory_lock`` (non-blocking). Yields
    ``True`` if acquired, ``False`` if another process already holds it (= it is
    already recomputing this kind+domain; the caller should skip — that process
    will advance the OCC pointer). Released via ``pg_advisory_unlock`` in
    ``finally``. *Session*-level (not ``_xact_``) so the ``_exec`` read-rollback
    does **not** drop the lock, and writes/commits inside the block keep it.

    SQLite / non-PG: no-op, always yields ``True`` (single-writer; no advisory
    locks). **No correctness divergence** — ``advance_pointer``'s OCC CAS is the
    sole safety net on both backends; this lock only suppresses *duplicate*
    compute on PG. Acquire/release errors degrade to no-guard (yield ``True``).
    """
    if not _is_pg(conn):
        yield True
        return
    key = _advisory_key(kind, domain)
    acquired = False
    try:
        row, _ = _exec(conn, "SELECT pg_try_advisory_lock(%s)", (key,), fetch="one")
        acquired = bool(_row_get(row, 0, "pg_try_advisory_lock"))
    except Exception as exc:  # noqa: BLE001 — lock unavailable → run unguarded
        logger.debug("recompute_lock acquire failed (proceeding unguarded): %s", exc)
        _safe_rollback(conn)
        yield True  # safe-degrade: OCC CAS still guarantees correctness
        return
    try:
        yield acquired
    finally:
        if acquired:
            try:
                _exec(conn, "SELECT pg_advisory_unlock(%s)", (key,), fetch="one")
            except Exception as exc:  # noqa: BLE001
                logger.debug("recompute_lock release failed: %s", exc)
                _safe_rollback(conn)


_DEFAULT_LEIDEN = {"resolution": 1.0, "seed": 42, "theta_e": 0.4}


def read_leiden_params(conn: Any, domain: str | None) -> dict[str, Any]:
    """The Leiden settings (resolution/seed) the Manager saved in ``analysis_param``.

    cascade: domain-specific latest -> (if absent) global (domain IS NULL) latest ->
    default. So if the Manager saved per-domain it uses that, if only global it uses
    the global value, and if nothing it uses the default (resolution=1.0, seed=42).
    Read-only — returns the default when the table is absent (no CREATE).

    Returns: ``{resolution, seed, saved_at, is_default, scope}``.
    """
    import json as _json

    def _q(where_domain: bool) -> Any:
        if where_domain:
            sql = (
                "SELECT params_json, created_at FROM analysis_param "
                f"WHERE domain = {_ph(conn)} ORDER BY id DESC LIMIT 1"
            )
            return _exec(conn, sql, (domain,), fetch="one")[0]
        sql = (
            "SELECT params_json, created_at FROM analysis_param "
            "WHERE domain IS NULL ORDER BY id DESC LIMIT 1"
        )
        return _exec(conn, sql, (), fetch="one")[0]

    row = None
    scope = "default"
    try:
        if domain:
            row = _q(True)
            scope = "domain"
        if row is None:
            row = _q(False)
            scope = "global"
    except Exception as exc:  # noqa: BLE001 — analysis_param missing, etc.
        logger.debug("read_leiden_params failed (domain=%s): %s", domain, exc)
        if _is_pg(conn):
            _safe_rollback(conn)
        return {**_DEFAULT_LEIDEN, "saved_at": None, "is_default": True,
                "scope": "default"}
    if row is None:
        return {**_DEFAULT_LEIDEN, "saved_at": None, "is_default": True,
                "scope": "default"}
    raw = _row_get(row, 0, "params_json")
    saved_at = _row_get(row, 1, "created_at")
    try:
        loaded = _json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        loaded = {}
    merged = {**_DEFAULT_LEIDEN, **(loaded if isinstance(loaded, dict) else {})}
    return {
        "resolution": float(merged.get("resolution", 1.0)),
        "seed": int(merged.get("seed", 42)),
        "theta_e": float(merged.get("theta_e", 0.4)),
        "saved_at": str(saved_at) if saved_at is not None else None,
        "is_default": False,
        "scope": scope,
    }


def _fetch_run_by_id(conn: Any, run_id: str) -> dict[str, Any] | None:
    """One train_run row by id -> dict (same shape as train_run_latest_completed)."""
    try:
        row, _ = _exec(
            conn, f"SELECT * FROM train_run WHERE id = {_ph(conn)}",
            (run_id,), fetch="one",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("_fetch_run_by_id failed: %s", exc)
        if _is_pg(conn):
            _safe_rollback(conn)
        return None
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):  # sqlite3.Row
        return {k: row[k] for k in row.keys()}
    return None


def resolve_latest_run(
    graph: Any, kind: str, domain: str | None = None,
) -> dict[str, Any] | None:
    """Single read entry point — prefer the community_state pointer, fall back otherwise.

    1. The pointer's ``latest_run_id`` -> return that train_run (the "valid run" the
       OCC guarantees). Avoids the regression risk of finished_at ordering across
       multiple clients.
    2. When the pointer / its run is absent -> graceful fallback to
       ``train_run_latest_completed(kind, domain)`` (the original finished_at behavior).

    ``kind`` accepts either 'leiden_entity'/'leiden_event' (consumer convention) or
    'entity'/'event'. The fallback uses the ``kind`` passed by the caller as-is.
    """
    conn = getattr(graph, "_conn", None)
    if conn is not None:
        ptr = get_pointer(conn, kind, domain)
        if ptr and ptr.get("latest_run_id"):
            run = _fetch_run_by_id(conn, ptr["latest_run_id"])
            if run:
                return run
    # fallback — preserve the original behavior
    try:
        return graph.train_run_latest_completed(kind, domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_latest_run fallback failed: %s", exc)
        return None


def resolve_run_domain_first(
    graph: Any, kind: str, domain: str | None,
) -> dict[str, Any] | None:
    """Per-domain run, falling back to the cross-domain (``domain IS NULL``) run.

    Domain-scoped views (an entity and its events all live in one domain) want
    *their* domain's run. ``_ensure_run`` (web/routes/community.py) may seed only
    a cross-domain (``domain=None``) run as a safety net, so fall back to it when
    no per-domain run exists.

    This is the correct call for entity/node-scoped consumers. A bare
    ``resolve_latest_run(kind, None)`` is the bug it replaces: with ``domain=None``
    the pointer lookup misses the per-domain ``community_state`` row and the
    fallback resolves a *stale* cross-domain run, ignoring the richer per-domain
    run entirely.
    """
    if domain:
        run = resolve_latest_run(graph, kind, domain)
        if run is not None:
            return run
    return resolve_latest_run(graph, kind, None)


__all__ = [
    "ensure_table",
    "derive_version",
    "get_pointer",
    "is_stale",
    "advance_pointer",
    "set_pointer",
    "resolve_latest_run",
    "resolve_run_domain_first",
    "read_leiden_params",
]
