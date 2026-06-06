"""MCP call telemetry emitter (JSONL).

Wraps FastMCP ``@mcp_app.tool()`` decorators.  Recorded per call:
- duration_ms
- result_count (length of array / dict)
- error_class (on exception)

Output: JSONL file (``${LOG_FILE_DIR}/k2g-mcp-YYYYMMDD.jsonl``).
A file rather than DB rows suits the high-volume telemetry use case.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Callable[..., Any])

# Module-level lazy file handle — opened once per process
_FILE_HANDLE: Any = None
_CURRENT_DATE: str = ""


def _get_telemetry_path(log_file_dir: str, suffix_host: bool = False) -> Path:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    if suffix_host:
        host = socket.gethostname().split(".")[0]
        name = f"k2g-mcp-{today}-{host}.jsonl"
    else:
        name = f"k2g-mcp-{today}.jsonl"
    return Path(log_file_dir) / name


def _ensure_handle(log_file_dir: str, suffix_host: bool = False) -> Any:
    """Open file lazily, rotate on date change."""
    global _FILE_HANDLE, _CURRENT_DATE

    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    if _FILE_HANDLE is not None and _CURRENT_DATE == today:
        return _FILE_HANDLE

    if _FILE_HANDLE is not None:
        try:
            _FILE_HANDLE.close()
        except Exception:  # noqa: BLE001
            pass

    Path(log_file_dir).mkdir(parents=True, exist_ok=True)
    path = _get_telemetry_path(log_file_dir, suffix_host)
    _FILE_HANDLE = open(path, "a", encoding="utf-8")
    _CURRENT_DATE = today
    return _FILE_HANDLE


def emit_telemetry(record: dict[str, Any]) -> None:
    """Append one JSONL line.  Logs a warning on failure."""
    try:
        from k2g.core.config import get_settings
        s = get_settings()
        log_dir = s.log_file_dir or os.path.join(s.data_dir or ".", "logs")
        fh = _ensure_handle(log_dir, suffix_host=s.log_file_hostname_suffix)
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        fh.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP telemetry emit failed: %s", exc)


def telemetry_wrap(tool_name: str | None = None) -> Callable[[T], T]:
    """FastMCP tool decorator.  Returns the original result unchanged;
    on failure, records the error in telemetry then re-raises.
    """
    def _deco(fn: T) -> T:
        name = tool_name or getattr(fn, "__name__", "unknown")

        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            t0 = time.perf_counter()
            error_class = None
            result_count = None
            try:
                result = fn(*args, **kwargs)
                # Estimate result_count
                if hasattr(result, "__len__"):
                    try:
                        result_count = len(result)
                    except Exception:  # noqa: BLE001
                        result_count = None
                elif isinstance(result, dict):
                    # Dict result (e.g. SearchResult) — check common list keys
                    for key in ("hits", "matches", "events", "rows"):
                        v = result.get(key)
                        if hasattr(v, "__len__"):
                            try:
                                result_count = len(v)
                                break
                            except Exception:  # noqa: BLE001
                                pass
                return result
            except Exception as exc:
                error_class = type(exc).__name__
                raise
            finally:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                # Record owner_id from session context when available
                owner_id: str | None = None
                try:
                    from k2g.security.session_context import get_session_context
                    ctx = get_session_context()
                    if ctx and ctx.user_id:
                        owner_id = ctx.user_id
                except Exception:  # noqa: BLE001
                    pass
                # Client-supplied conversation_id (explicit kwarg).
                # Passed by clients (Claude/Codex) until FastMCP middleware
                # standardises it.  Used to trace the search→raw_fetch flow
                # within a conversation (summary fidelity metrics).
                conversation_id = kwargs.get("conversation_id") if kwargs else None
                record = {
                    "ts": datetime.now(tz=timezone.utc).isoformat(
                        timespec="milliseconds",
                    ).replace("+00:00", "Z"),
                    "tool": name,
                    "duration_ms": duration_ms,
                    "result_count": result_count,
                    "error_class": error_class,
                    "owner_id": owner_id,
                    "conversation_id": conversation_id,
                }
                emit_telemetry(record)
                # Mirror the mcp_call milestone to the application log
                # (separate from JSONL telemetry; for grep / alerting).
                log_extra = {
                    "tool": name,
                    "duration_ms": duration_ms,
                    "result_count": result_count,
                }
                if conversation_id:
                    log_extra["conversation_id"] = conversation_id
                if error_class:
                    log_extra["error_class"] = error_class
                    logger.warning("mcp_call_failed", extra=log_extra)
                else:
                    logger.info("mcp_call", extra=log_extra)

        return _wrapped  # type: ignore[return-value]

    return _deco


__all__ = ["telemetry_wrap", "emit_telemetry"]
