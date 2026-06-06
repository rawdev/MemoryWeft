"""Pure (no-FastAPI) helpers for writing ``.mweft/project.yaml``.

Used by both the web route ([web/routes/project_init.py](../web/routes/project_init.py))
and the CLI (``mweft-init``). The CLI must work *without any HTTP server
running* — the user's primary inconvenience. Web route imports the same
functions and wraps validation errors in HTTPException.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from k2g.ui.project_config import (
    ProjectConfig,
    load_project_config,
    project_config_path,
)

logger = logging.getLogger(__name__)


class ProjectInitError(Exception):
    """Raised for validation / fs errors during project init.

    The web route translates this to HTTP 400 / 409; the CLI prints
    ``str(exc)`` and exits non-zero.
    """

    def __init__(self, msg: str, *, conflict: bool = False) -> None:
        super().__init__(msg)
        self.conflict = conflict


@dataclass(frozen=True)
class SqliteBackend:
    data_dir: str


@dataclass(frozen=True)
class PostgresBackend:
    dsn: str


@dataclass(frozen=True)
class EmbeddingChoice:
    provider: str = "local"
    model: str = "BAAI/bge-m3"
    dim: int | None = None      # None → resolved from model


def write_project_yaml(
    project_dir: str | Path,
    *,
    group: str,
    domain: str,
    backend: SqliteBackend | PostgresBackend,
    search_targets: list[dict[str, str]] | None = None,
    save_tags: list[str] | None = None,
    embedding: EmbeddingChoice | None = None,
    force: bool = False,
) -> tuple[Path, dict[str, str]]:
    """Write ``<project_dir>/.mweft/project.yaml`` and return ``(path, env)``.

    ``env`` is the dict the MCP server consumes (same shape as
    :meth:`ProjectConfig.to_env_dict`) — caller wires it into ``.mcp.json``.

    Raises :class:`ProjectInitError` on conflict (exists w/o force),
    invalid input, or fs failure.
    """
    pd = Path(project_dir)
    if not pd.is_dir():
        try:
            pd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectInitError(
                f"project_dir not creatable: {exc}",
            ) from exc

    if not group:
        raise ProjectInitError("group is required (non-empty string)")
    if not domain:
        raise ProjectInitError("domain is required (non-empty string)")

    target = project_config_path(pd)
    if target.exists() and not force:
        raise ProjectInitError(
            f"{target} already exists. Pass force=True to overwrite, or "
            f"edit the file directly.",
            conflict=True,
        )

    emb = embedding or EmbeddingChoice()
    payload: dict[str, Any] = {
        "group": group,
        "domain": domain,
        "search_targets": search_targets or [{"domain": domain}],
        "save_tags": [
            str(t).strip() for t in (save_tags or []) if str(t).strip()
        ],
        "embedding": {
            "provider": emb.provider,
            "model": emb.model,
        },
    }
    if emb.dim is not None:
        payload["embedding"]["dim"] = int(emb.dim)

    if isinstance(backend, SqliteBackend):
        data_dir = Path(backend.data_dir).expanduser()
        if not data_dir.is_absolute():
            data_dir = (pd / data_dir).resolve()
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectInitError(
                f"data_dir not creatable: {exc}",
            ) from exc
        payload["data_dir"] = str(data_dir)
        payload["postgres_dsn"] = ""
    elif isinstance(backend, PostgresBackend):
        if not backend.dsn.startswith(("postgres://", "postgresql://")):
            raise ProjectInitError(
                f"postgres dsn must start with 'postgresql://': {backend.dsn!r}",
            )
        payload["data_dir"] = ""
        payload["postgres_dsn"] = backend.dsn
    else:
        raise ProjectInitError(
            f"Unsupported backend: {type(backend).__name__}",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# MWeft project config\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("project_init: wrote %s", target)

    # Round-trip to confirm validity + return env preview.
    cfg = load_project_config(pd)
    return target, cfg.to_env_dict()
