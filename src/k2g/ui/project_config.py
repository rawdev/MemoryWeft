"""Project configuration for MWeft Manager UI.

A "project" is a folder containing ``.mweft/project.yaml`` that pins the
``group``, ``domain``, ``search_targets``, ``data_dir``, and optional
``postgres_dsn`` for a single per-project subprocess. The launcher copies a
default template into new folders; the subprocess loads it into env vars
before booting FastAPI.

Backend selection follows docs/107: ``postgres_dsn`` non-empty → PG mode,
otherwise SQLite mode using ``data_dir``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


PROJECT_DIR_NAME = ".mweft"
PROJECT_CONFIG_NAME = "project.yaml"
TEMPLATE_PACKAGE = "k2g.ui.templates"
TEMPLATE_FILENAME = "project.yaml"


# Known embedding model → output dim mapping.  Used when ``project.yaml``
# does not pin an explicit ``embedding.dim`` so we can still emit a
# correct ``EMBEDDING_DIM`` env to the subprocess instead of falling back
# to Settings' default (1536, OpenAI-shaped) which causes a hard dim
# mismatch with BGE-M3 (1024) at vector-store init time.
#
# Keys are matched case-insensitively against ``embedding.model``.
DEFAULT_DIM_BY_MODEL: dict[str, int] = {
    "baai/bge-m3": 1024,
    "intfloat/multilingual-e5-large": 1024,
    "intfloat/multilingual-e5-base": 768,
    "intfloat/multilingual-e5-small": 384,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # Dummy provider — caller usually overrides via EMBEDDING_DIM env.
    "dummy": 1024,
}


def resolve_embedding_dim(model: str, *, fallback: int = 1536) -> int:
    """Best-effort dim lookup from a model identifier."""
    return DEFAULT_DIM_BY_MODEL.get(model.lower().strip(), fallback)


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "local"
    model: str = "BAAI/bge-m3"
    dim: int | None = None  # None → resolved from model at to_env_dict()

    def resolved_dim(self) -> int:
        if self.dim is not None:
            return int(self.dim)
        return resolve_embedding_dim(self.model)


@dataclass(frozen=True)
class ProjectConfig:
    group: str
    domain: str
    search_targets: list[dict[str, str]] = field(default_factory=list)
    # Curated subset of predefined tags (groups) the AI should treat as the
    # default save-tag candidates. Multi-value, distinct from the single
    # ``group`` (save root). Emitted as K2G_USER_MEMORY_SAVE_TAGS (CSV).
    save_tags: list[str] = field(default_factory=list)
    data_dir: str = "./data"
    postgres_dsn: str | None = None
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectConfig:
        emb_raw = data.get("embedding") or {}
        raw_dsn = data.get("postgres_dsn")
        postgres_dsn = (
            str(raw_dsn).strip() if raw_dsn and str(raw_dsn).strip() else None
        )
        # dim is optional in project.yaml — resolve via model lookup if absent
        raw_dim = emb_raw.get("dim")
        dim: int | None
        if raw_dim is None:
            dim = None
        else:
            try:
                dim = int(raw_dim)
            except (TypeError, ValueError):
                dim = None
        raw_data_dir = data.get("data_dir")
        if raw_data_dir is None:
            data_dir = "./data"
        else:
            # Explicit empty string is meaningful: PG mode uses ""
            data_dir = str(raw_data_dir)
        return cls(
            group=str(data.get("group") or "default"),
            domain=str(data.get("domain") or "default"),
            search_targets=[
                {k: str(v) for k, v in entry.items()}
                for entry in (data.get("search_targets") or [])
                if isinstance(entry, dict)
            ],
            save_tags=[
                str(t).strip()
                for t in (data.get("save_tags") or [])
                if str(t).strip()
            ],
            data_dir=data_dir,
            postgres_dsn=postgres_dsn,
            embedding=EmbeddingConfig(
                provider=str(emb_raw.get("provider") or "local"),
                model=str(emb_raw.get("model") or "BAAI/bge-m3"),
                dim=dim,
            ),
        )

    def to_env_dict(self) -> dict[str, str]:
        from k2g.core.config import search_targets_to_csv
        env = {
            "K2G_USER_MEMORY_SAVE_GROUP": self.group,
            "K2G_USER_MEMORY_SAVE_DOMAIN": self.domain,
            # CSV, not JSON — K2G_USER_SEARCH_TARGETS parser (NoDecode) expects
            # "a,b:g". json.dumps here would be split on ',' into garbage.
            "K2G_USER_SEARCH_TARGETS": search_targets_to_csv(self.search_targets),
            # Curated save-tag candidates (CSV). Settings parses CSV → list.
            "K2G_USER_MEMORY_SAVE_TAGS": ",".join(self.save_tags),
            # UI-spawned subprocesses must ignore .mwf.
            "K2G_DOTENV_FILE": "",
            "DATA_DIR": self.data_dir,
            "EMBEDDING_PROVIDER": self.embedding.provider,
            "EMBEDDING_MODEL": self.embedding.model,
            # BP-73 hotfix: explicitly carry EMBEDDING_DIM to the subprocess.
            # Without this, Settings' default (1536, OpenAI-shaped) wins —
            # which mismatches BGE-M3's 1024 output and corrupts the vector
            # store schema on first init.
            "EMBEDDING_DIM": str(self.embedding.resolved_dim()),
        }
        if self.postgres_dsn:
            env["K2G_POSTGRES_DSN"] = self.postgres_dsn
        return env


def project_config_path(project_dir: Path) -> Path:
    return project_dir / PROJECT_DIR_NAME / PROJECT_CONFIG_NAME


def is_project_initialized(project_dir: Path) -> bool:
    return project_config_path(project_dir).is_file()


def load_project_config(project_dir: Path) -> ProjectConfig:
    path = project_config_path(project_dir)
    if not path.is_file():
        raise FileNotFoundError(
            f"Project not initialized — {path} missing. "
            f"Run install_template({project_dir!r}) first.",
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level.")
    return ProjectConfig.from_dict(data)


def install_template(project_dir: Path, *, force: bool = False) -> Path:
    """Copy the bundled project.yaml template into ``<project_dir>/.mweft/``.

    Returns the path of the written file. Skips when present unless ``force``.
    """
    target = project_config_path(project_dir)
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    template_text = (
        resources.files(TEMPLATE_PACKAGE)
        .joinpath(TEMPLATE_FILENAME)
        .read_text(encoding="utf-8")
    )
    target.write_text(template_text, encoding="utf-8")
    return target
