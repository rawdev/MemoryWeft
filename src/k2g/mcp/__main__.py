"""Entry point for ``python -m k2g.mcp`` — starts the stdio MCP server.

Init modes:
  - default (eager): builds read deps before server listen to eliminate
    first tool-call latency. Surfaces config errors (DSN, model) at
    startup.
  - ``K2G_MCP_LAZY_INIT=true``: skips eager init and starts listening
    immediately. First tool call triggers a lazy build (~10s one-time
    delay). Use with MCP clients that have short startup timeouts
    (~30s), such as Claude Code.

File log: ``{DATA_DIR}/mcp_debug.log`` (DEBUG level when DEBUG_MODE=true).

The MCP server is read-only: content/object store and ingestion
pipeline are not built here (injected off-band by a separate build pipeline).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _require_mwf_file() -> Path | None:
    """Hard-fail if CWD has no ``.mwf`` file, unless ``K2G_DOTENV_FILE=""``
    is explicitly set.

    Rationale: running with defaults silently creates a separate K2G
    instance with a different ``data_dir`` / embedding dimension, making
    existing memories invisible. The MCP server therefore requires an
    explicit config file.

    ``mweft-init`` users invoke k2g-mcp with all config in ``.mcp.json``
    env (``K2G_DOTENV_FILE=off`` + ``DATA_DIR`` + ``EMBEDDING_*``), so
    they intentionally skip ``.mwf``. When ``K2G_DOTENV_FILE`` is set to
    any disable sentinel, turn the safety check off and return ``None``.

    The opt-out value is a non-empty sentinel (``"off"``) rather than
    ``""`` because some MCP clients drop empty-string env values when
    spawning the server — an empty value would then arrive *unset*, the
    server would require a ``.mwf`` and hard-fail ("startup aborted").
    We still accept ``""`` for backward compatibility, and mirror the
    disable set understood by :func:`k2g.core.config._resolve_env_file`.

    Legacy: if only ``.chronoGraph`` exists, auto-rename once.
    """
    # Env-level opt-out: user explicitly signals ".mwf not used".
    # Sentinel distinguishes an explicit value from a simply-missing var.
    _SENTINEL = object()
    dotenv_override = os.environ.get("K2G_DOTENV_FILE", _SENTINEL)
    if dotenv_override is not _SENTINEL:
        s = str(dotenv_override).strip().lower()
        if s in ("", "none", "off", "disabled", "false", "0"):
            return None     # opt-out — env owns all configuration

    cfg = Path.cwd() / ".mwf"
    if not cfg.exists():
        legacy = Path.cwd() / ".chronoGraph"
        if legacy.exists():
            return legacy
        sys.stderr.write(
            f"[k2g] .mwf config file not found: {cfg}\n"
            f"      No config in the current directory;"
            f" MCP server startup aborted.\n"
            f"      Fix: `cp .mwf.example .mwf` and fill in"
            f" values, or run from a folder set up with"
            f" mweft-init\n"
            f"          (set K2G_DOTENV_FILE=off to opt out"
            f" of .mwf).\n"
        )
        raise SystemExit(2)
    return cfg


# Backward compat alias
_require_chronograph_file = _require_mwf_file


def _setup_logging() -> Path:
    """Delegate to observability.logging_config. mcp_debug.log is
    activated when DEBUG_MODE=true (include_mcp_debug=True)."""
    from k2g.core.config import get_settings
    from k2g.observability.logging_config import configure_logging

    settings = get_settings()
    info = configure_logging(
        settings,
        log_file_basename="k2g-mcp",
        include_mcp_debug=True,
        force=True,
    )
    # Backward compat: return mcp_debug.log path (meaningful only when DEBUG_MODE=true)
    return Path(settings.data_dir) / "mcp_debug.log"


def _eager_init() -> None:
    """Pre-build read deps to eliminate first tool-call latency."""
    from k2g.mcp import server as mcp_server

    log = logging.getLogger("k2g.mcp.__main__")

    t0 = time.time()
    log.info("eager init: building read deps (graph+vector+embedding)")
    mcp_server._get_deps()
    log.info("eager init: read deps ready (%.1fs)", time.time() - t0)


def main() -> None:
    cfg_path = _require_mwf_file()
    log_path = _setup_logging()
    log = logging.getLogger("k2g.mcp.__main__")
    log.info("=" * 60)
    log.info("K2G MCP server start | config=%s | log_file=%s", cfg_path, log_path)

    from k2g.core.config import get_settings
    from k2g.mcp.server import mcp_app

    settings = get_settings()
    log.info(
        "settings: data_dir=%s graph=%s vector=%s embed=%s dim=%s recording=%s debug=%s",
        settings.data_dir,
        settings.graph_db_provider,
        getattr(settings, "vector_store_provider", "n/a"),
        settings.embedding_model,
        settings.embedding_dim,
        settings.recording_enabled,
        settings.debug_mode,
    )

    # K2G_MCP_LAZY_INIT=true skips eager init; deps build on first tool call.
    # Needed for clients with short startup timeout (~30s) like Claude Code.
    # Default is eager -- surfaces DSN/model config errors early.
    lazy = os.environ.get("K2G_MCP_LAZY_INIT", "").strip().lower() in ("1", "true", "yes")
    if lazy:
        log.info("lazy init mode: read deps built on first tool call -- K2G_MCP_LAZY_INIT=true")
    else:
        try:
            _eager_init()
        except Exception:
            log.exception("eager init failed -- aborting server startup")
            raise

    log.info("mcp_server_start", extra={
        "config_path": str(cfg_path),
        "data_dir": settings.data_dir,
        "graph_db": settings.graph_db_provider,
    })
    try:
        mcp_app.run()
    except Exception:
        log.exception("mcp_server_exception")
        raise
    finally:
        log.info("mcp_server_shutdown")


if __name__ == "__main__":
    main()
