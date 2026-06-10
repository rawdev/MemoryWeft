"""JSON Lines logging + centralized dictConfig.

Unifies 3 scattered setup sites (MCP / CLI / build pipeline) into a
single entry point: ``configure_logging(settings)``.  Existing 155+
files using ``logging.getLogger(__name__)`` are unchanged -- only
the root logger's handler / formatter is replaced.

The existing ``mcp_debug.log`` (DEBUG_MODE) behaviour is preserved --
``configure_logging(include_mcp_debug=True)`` attaches an additional
handler with the same file and format.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.core.config import Settings


# Lightweight reserved-attr set -- used by the JSON formatter to exclude
# stdlib LogRecord standard attributes so only *extra* fields are
# serialized into the context dict.
_LOG_RECORD_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonLinesFormatter(logging.Formatter):
    """JSON Lines formatter using only the stdlib.

    Output format (log system design §6.3)::

        {"ts": "...", "level": "warning", "logger": "k2g.agent.pool",
         "build_id": "b_2026...", "session_id": "...",
         "domain": "...", "owner_id": "...",
         "msg": "...", "context": {...}}

    Extra attrs passed via ``logger.warning("msg", extra={"build_id": "...", ...})``
    are collected into ``context``; standard LogRecord attrs are excluded.
    """

    def __init__(self, *, default_owner_id: str | None = None) -> None:
        super().__init__()
        self._default_owner_id = default_owner_id

    def format(self, record: logging.LogRecord) -> str:
        # getMessage() handles %-interpolation of args
        msg = record.getMessage()

        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds",
        )
        # ISO 8601 with 'Z' suffix
        if ts.endswith("+00:00"):
            ts = ts[:-6] + "Z"

        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": msg,
        }

        # Collect extra attrs; promote first-class dimensions to top-level
        # for easy querying; everything else goes inside the context dict.
        context: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_RESERVED:
                continue
            if key.startswith("_"):
                continue
            # First-class dimensions (build_id / session_id / domain / owner_id)
            # are promoted to top level so they are queryable directly.
            if key in {"build_id", "session_id", "domain", "owner_id"}:
                payload[key] = value
            else:
                context[key] = value
        if "owner_id" not in payload and self._default_owner_id:
            payload["owner_id"] = self._default_owner_id
        if context:
            payload["context"] = context

        # Exception
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover — paranoid fallback
            return json.dumps({"ts": ts, "level": "error",
                               "logger": "k2g.observability.logging_config",
                               "msg": "json_format_failure"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def configure_logging(
    settings: "Settings | None" = None,
    *,
    log_file_basename: str = "k2g-app",
    include_mcp_debug: bool = False,
    force: bool = False,
) -> dict[str, str]:
    """Configure the K2G root logger from settings.

    Args:
        settings: ``Settings`` instance.  When None, ``get_settings()`` is used.
        log_file_basename: Log file prefix.  Defaults to ``k2g-app``; use
            ``k2g-poll``, ``k2g-calibrate``, etc. for other entry points.
        include_mcp_debug: Set True when called from the MCP entry point to
            attach an additional ``mcp_debug.log`` (DEBUG_MODE) handler,
            matching existing behaviour.  False for CLI/build entry points.
        force: Re-configure even if already configured (for tests / option
            refresh).

    Returns:
        ``{"log_dir": ..., "log_path": ..., "format": ...}`` — callers can
        inspect the active log destination.
    """
    from k2g.core.config import get_settings  # deferred import — avoid circular

    if settings is None:
        settings = get_settings()

    # Skip if already configured and force=False
    root = logging.getLogger()
    if root.handlers and not force:
        return {
            "log_dir": settings.log_file_dir or "",
            "log_path": "",
            "format": settings.log_format,
            "skipped": "already_configured",
        }

    # Remove existing handlers (force or reconfigure)
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)

    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    # Determine formatter
    fmt_kind = (settings.log_format or "text").lower()
    if fmt_kind == "json":
        formatter: logging.Formatter = JsonLinesFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    output_kind = (settings.log_output or "stdout").lower()

    log_dir = ""
    log_path = ""

    # Context filter — auto-injects thread-local build_id/session_id/domain/owner_id
    from k2g.observability.log_context import LogContextFilter

    ctx_filter = LogContextFilter()

    # stdout / stderr handler
    if output_kind in {"stdout", "both"}:
        sh = logging.StreamHandler(stream=sys.stderr)
        sh.setFormatter(formatter)
        sh.setLevel(level)
        sh.addFilter(ctx_filter)
        root.addHandler(sh)

    # File handler
    if output_kind in {"file", "both"}:
        log_dir = settings.log_file_dir or "./data/logs"
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        # filename — k2g-app-YYYYMMDD.jsonl + optional hostname suffix
        suffix = ".jsonl" if fmt_kind == "json" else ".log"
        if settings.log_file_hostname_suffix:
            host = socket.gethostname().split(".")[0]
            base = f"{log_file_basename}-{host}{suffix}"
        else:
            base = f"{log_file_basename}{suffix}"
        log_path = os.path.join(log_dir, base)

        # TimedRotatingFileHandler — when='midnight', utc=True
        fh = logging.handlers.TimedRotatingFileHandler(
            filename=log_path,
            when="midnight",
            backupCount=int(settings.log_retention_days or 90),
            utc=True,
            encoding="utf-8",
        )
        fh.setFormatter(formatter)
        fh.setLevel(level)
        fh.addFilter(ctx_filter)
        # Append date to rotated file names — k2g-app.jsonl.2026-05-05
        fh.suffix = "%Y-%m-%d"
        root.addHandler(fh)

        # Optional gzip compression immediately after rotation (namer/rotator hook)
        if settings.log_file_compress:
            try:
                import gzip
                import shutil

                def _gz_rotator(source: str, dest: str) -> None:
                    with open(source, "rb") as sf, gzip.open(
                        dest + ".gz", "wb",
                    ) as df:
                        shutil.copyfileobj(sf, df)
                    os.remove(source)

                fh.rotator = _gz_rotator  # type: ignore[assignment]
            except Exception:  # pragma: no cover
                pass

    # MCP debug branch — preserves existing mcp_debug.log behaviour
    if include_mcp_debug and settings.debug_mode:
        debug_path = os.path.join(settings.data_dir or "./data", "mcp_debug.log")
        Path(os.path.dirname(debug_path) or ".").mkdir(parents=True, exist_ok=True)
        dh = logging.handlers.RotatingFileHandler(
            filename=debug_path,
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        # Debug file is always plain text — grep-friendly
        dh.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        dh.setLevel(logging.DEBUG)
        dh.addFilter(ctx_filter)
        root.addHandler(dh)
        root.setLevel(logging.DEBUG)  # raise root to DEBUG when debug file is active

    return {
        "log_dir": log_dir,
        "log_path": log_path,
        "format": fmt_kind,
    }


__all__ = ["configure_logging", "JsonLinesFormatter"]
