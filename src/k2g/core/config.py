"""
K2G configuration management.

- pydantic-settings: environment variables + .mwf file
- DomainConfig: YAML-based domain configuration
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# .chronoGraph -> .mwf automatic rename hook
# ---------------------------------------------------------------------------

def _maybe_rename_legacy_chronograph(path: Path) -> Path:
    """Auto-rename .chronoGraph to .mwf once per environment.

    Ignores .chronoGraph if .mwf already exists (user created a new file).
    Falls back to reading .chronoGraph directly if rename fails
    (e.g. read-only filesystem), limited to one attempt.
    """
    if path.name != ".mwf":
        return path
    legacy = path.parent / ".chronoGraph"
    if legacy.exists() and not path.exists():
        try:
            legacy.rename(path)
            logger.warning(
                "Auto-renamed %s to %s. "
                "Only .mwf will be recognized from now on.",
                legacy, path,
            )
        except OSError as e:
            logger.warning(
                "Auto-rename failed: %s — reading %s directly",
                e, legacy,
            )
            return legacy
    return path


# ---------------------------------------------------------------------------
# Search target -- (domain, optional group sub-tree) tuple
# ---------------------------------------------------------------------------

class SearchTarget(BaseModel):
    """MCP search target unit -- domain + optional group sub-tree path.

    Aligned with the (domain, name) unique constraint in the K2G groups
    table. Expressed as a tuple list during search -- each entry defines
    one search scope; multiple entries are combined with OR.

    Example: ``SearchTarget(domain="K2G", group="src/k2g/mcp")`` ->
    events under the src/k2g/mcp sub-tree of the K2G domain.
    ``SearchTarget(domain="ai_memory")`` -> all of ai_memory.
    """

    model_config = {"extra": "ignore", "frozen": False}

    domain: str
    group: str | None = None

    def to_str(self) -> str:
        """Format as ``"domain"`` or ``"domain:group"``."""
        return f"{self.domain}:{self.group}" if self.group else self.domain

    @classmethod
    def from_str(cls, entry: str) -> "SearchTarget":
        """Parse ``"domain"`` or ``"domain:group"``."""
        entry = entry.strip()
        if ":" in entry:
            domain, group = entry.split(":", 1)
            return cls(domain=domain.strip(), group=group.strip() or None)
        return cls(domain=entry)


def search_targets_to_csv(targets: Any) -> str:
    """Serialize search targets → the CSV form ``K2G_USER_SEARCH_TARGETS`` expects.

    Inverse of :meth:`Settings._parse_user_search_targets`. Accepts a list of
    ``SearchTarget`` / ``dict({domain, group?})`` / ``str`` / ``(domain, group)``
    and returns ``"a,b:g"``. The env var is **CSV, not JSON** — emitting
    ``json.dumps`` here breaks the parser (``NoDecode`` blocks JSON decode, so the
    raw string is split on ``,`` → garbage domains like ``[{"domain"``).
    """
    if not targets:
        return ""
    parts: list[str] = []
    for t in targets:
        if isinstance(t, SearchTarget):
            st = t
        elif isinstance(t, dict):
            domain = str(t.get("domain") or "").strip()
            if not domain:
                continue
            grp = t.get("group")
            st = SearchTarget(domain=domain, group=(str(grp).strip() or None) if grp else None)
        elif isinstance(t, (tuple, list)) and len(t) >= 1:
            domain = str(t[0]).strip()
            if not domain:
                continue
            grp = t[1] if len(t) > 1 else None
            st = SearchTarget(domain=domain, group=(str(grp).strip() or None) if grp else None)
        else:
            s = str(t).strip()
            if not s:
                continue
            st = SearchTarget.from_str(s)
        parts.append(st.to_str())
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Application settings (environment-variable based)
# ---------------------------------------------------------------------------

def _resolve_env_file() -> str | None:
    """Resolve ``K2G_DOTENV_FILE`` with explicit disable support.

    Prevents the case on Windows where ``env: {K2G_DOTENV_FILE: ""}``
    in ``.mcp.json`` is treated as unset and falls back to the default
    ``.mwf``. Returns None when an explicit sentinel is set
    (``"none"`` / ``"off"`` / ``"disabled"`` / ``""``), disabling
    dotenv loading.
    """
    raw = os.getenv("K2G_DOTENV_FILE")
    if raw is None:
        return ".mwf"
    s = raw.strip().lower()
    if s in ("", "none", "off", "disabled", "false", "0"):
        return None
    return raw


class Settings(BaseSettings):
    """
    K2G global settings.

    Loaded from environment variables or .mwf files.

    env_file can be overridden via the ``K2G_DOTENV_FILE`` env var:
        - Explicit disable: one of ``""`` / ``"none"`` / ``"off"`` /
          ``"disabled"`` / ``"false"`` / ``"0"`` -- skips dotenv
          loading (uses process env only). Useful for MCP servers
          where .mcp.json env is sufficient and stray .mwf keys
          should not leak in.
        - Explicit path: uses that file.
        - Unset: defaults to ``.mwf``.
    """

    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Data Directory
    # ------------------------------------------------------------------
    data_dir: str = Field(
        default="./data",
        description="Top-level data directory; default root for sub-stores.",
    )

    # ------------------------------------------------------------------
    # User-level default override (single-user)
    # ------------------------------------------------------------------
    # Default domain/group when MCP tool calls omit those arguments.
    # Unrelated to RLS -- purely a convenience layer. Explicit LLM
    # arguments override these defaults.
    user_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("K2G_USER_ID", "user_id"),
        description=(
            "Single-user identifier (multi-user relies on OAuth). "
            "Can be added as K2G_USER_ID in the MCP JSON env block."
        ),
    )
    # NoDecode -- prevent JSON parse attempts on CSV input.
    user_search_targets: Annotated[list["SearchTarget"], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "K2G_USER_SEARCH_TARGETS",
            "user_search_targets",
        ),
        description=(
            "Default search targets for MCP search tool calls. Format: "
            "'<domain1>,<domain2>:<group_path>,<domain3>:<group_path>'. "
            "Each entry = (domain[, group sub-tree path]). CSV -> tuple "
            "list. Example: K2G_USER_SEARCH_TARGETS=ai_memory,"
            "K2G:src/k2g/mcp means (all of ai_memory) OR (src/k2g/mcp "
            "sub-tree of K2G domain). Aligned with the (domain, name) "
            "unique constraint in K2G groups. Empty list -> search all "
            "permitted domains. Save defaults use "
            "K2G_USER_MEMORY_SAVE_DOMAIN / K2G_USER_MEMORY_SAVE_GROUP "
            "(single tuple)."
        ),
    )
    user_memory_save_domain: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "K2G_USER_MEMORY_SAVE_DOMAIN",
            "user_memory_save_domain",
        ),
        description=(
            "Default domain when k2g_remember is called without a domain "
            "argument. Must be a *single* domain since events.domain is "
            "NOT NULL. Can be added as K2G_USER_MEMORY_SAVE_DOMAIN in "
            "the MCP JSON env block. Defaults to 'ai_memory' if unset. "
            "Empty string is treated as None. CSV input raises "
            "ValueError."
        ),
    )
    user_memory_save_group: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "K2G_USER_MEMORY_SAVE_GROUP",
            "user_memory_save_group",
        ),
        description=(
            "Default working_folder when k2g_remember is called without "
            "one. Must be a *single* group -- each event belongs to one "
            "group (event_member_of edge). Can be added as "
            "K2G_USER_MEMORY_SAVE_GROUP in the MCP JSON env block. "
            "Empty string is treated as None. CSV input raises "
            "ValueError."
        ),
    )
    # NoDecode -- prevent JSON parse attempts on CSV input
    # (same pattern as user_search_targets).
    user_memory_save_tags: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "K2G_USER_MEMORY_SAVE_TAGS",
            "user_memory_save_tags",
        ),
        description=(
            "Tags forcibly applied on every mweft_remember save. "
            "Purpose: attach cross-cutting meta tags (e.g. org name, "
            "user) that do not appear in event content. Uses existing "
            "predefined group names from Manager UI -- looked up by "
            "name and attached via event_member_of without re-nesting "
            "under save_group (preserves cross-project provenance "
            "facet). Independent of per-call tag/category -- both are "
            "applied. Surfaced in the response as applied_save_tags. "
            "Accepts CSV or JSON list input. Empty value -> []. "
            "Injected from project.yaml save_tags."
        ),
    )

    # ------------------------------------------------------------------
    # Entity Vector Sync Recompute (internal, LLM-invisible)
    # ------------------------------------------------------------------
    entity_vector_sync_recompute: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "K2G_ENTITY_VECTOR_SYNC_RECOMPUTE",
            "entity_vector_sync_recompute",
        ),
        description=(
            "Synchronous entity vector recompute during mweft_remember. "
            "true = immediately recompute centroids for affected "
            "entities at the end of remember (personal use, default). "
            "false = skip recompute (enterprise -- use batch "
            "EntityVectorPhase only). Set via .mwf / process env / "
            "MCP JSON env block. Not exposed to LLM."
        ),
    )

    # ------------------------------------------------------------------
    # Deployment profile (personal | pro)
    # ------------------------------------------------------------------
    deployment: Literal["personal", "pro"] = Field(
        default="personal",
        validation_alias=AliasChoices("K2G_DEPLOYMENT", "deployment"),
        description=(
            "Deployment profile. personal = public/personal edition "
            "(tier-1 incremental ingestion only, batch-0 self-sustain, "
            "default). pro = server edition (+ hdbscan / LLM phase). "
            "Pro-only phases and UI gates -- actual gating in the "
            "controlnode slice. Set via .mwf / process env."
        ),
    )

    # ------------------------------------------------------------------
    # Category layer (user conversation grouping)
    # ------------------------------------------------------------------
    user_memory_category: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "K2G_USER_MEMORY_CATEGORY",
            "user_memory_category",
        ),
        description=(
            "Category layer for mweft_remember. true = use the "
            "working_folder (save group) as tree root and place the "
            "LLM-determined category as a child group (default -- "
            "enables the grouping channel for global-config users). "
            "false = category layer off; events go directly under "
            "working_folder (for power users with per-folder fine "
            "tuning). Set via .mwf / process env."
        ),
    )

    @field_validator("user_search_targets", mode="before")
    @classmethod
    def _parse_user_search_targets(cls, v: Any) -> list["SearchTarget"]:
        """CSV ``"a,b:g,c"`` -> [(a), (b, g), (c)] SearchTarget list.

        Each entry:
        - ``<domain>`` -- domain-wide (no sub-tree restriction)
        - ``<domain>:<group_path>`` -- restricted to that sub-tree
        """
        if v is None or v == "":
            return []
        if isinstance(v, str):
            targets: list[SearchTarget] = []
            for entry in v.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                if ":" in entry:
                    domain, group = entry.split(":", 1)
                    domain = domain.strip()
                    group = group.strip()
                    if not domain:
                        continue
                    targets.append(SearchTarget(domain=domain, group=group or None))
                else:
                    targets.append(SearchTarget(domain=entry))
            return targets
        if isinstance(v, list):
            result: list[SearchTarget] = []
            for item in v:
                if isinstance(item, SearchTarget):
                    result.append(item)
                elif isinstance(item, dict):
                    result.append(SearchTarget(**item))
                elif isinstance(item, (tuple, list)) and len(item) >= 1:
                    result.append(
                        SearchTarget(
                            domain=str(item[0]),
                            group=str(item[1]) if len(item) > 1 and item[1] else None,
                        )
                    )
                else:
                    result.append(SearchTarget(domain=str(item)))
            return result
        return []

    @field_validator("user_memory_save_group", mode="before")
    @classmethod
    def _parse_user_memory_save_group(cls, v: Any) -> str | None:
        """Empty string -> None. Raises ValueError on CSV input."""
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if "," in s:
            raise ValueError(
                "K2G_USER_MEMORY_SAVE_GROUP must be a *single* group "
                "-- CSV list not allowed. Use "
                "K2G_USER_SEARCH_TARGETS for search defaults."
            )
        return s

    @field_validator("user_memory_save_tags", mode="before")
    @classmethod
    def _parse_user_memory_save_tags(cls, v: Any) -> list[str]:
        """CSV ``"a,b/c"`` or JSON list or list -> normalized tag names.

        Empty value / None -> []. Strips blank entries, preserves
        order, and removes duplicates.
        """
        if v is None or v == "":
            return []
        items: list[str]
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json as _json
                try:
                    parsed = _json.loads(s)
                    items = [str(x) for x in parsed] if isinstance(parsed, list) else [s]
                except Exception:  # noqa: BLE001
                    items = [seg for seg in s.split(",")]
            else:
                items = [seg for seg in s.split(",")]
        elif isinstance(v, (list, tuple)):
            items = [str(x) for x in v]
        else:
            items = [str(v)]
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            name = item.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    @field_validator("user_memory_save_domain", mode="before")
    @classmethod
    def _parse_user_memory_save_domain(cls, v: Any) -> str | None:
        """Empty string -> None. Raises ValueError on CSV input."""
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        if "," in s:
            raise ValueError(
                "K2G_USER_MEMORY_SAVE_DOMAIN must be a *single* "
                "domain -- CSV list not allowed. Use "
                "K2G_USER_SEARCH_TARGETS for search defaults."
            )
        return s

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    # DEPRECATED: ``llm_provider`` / ``llm_model`` are legacy SDK path
    #   routing inputs. After the honest mapping change, PoolManager
    #   trusts yaml ``provider`` / ``model`` directly, so these fields
    #   are unused. Scheduled for removal after consolidation of 18
    #   remaining call sites.
    #   Migration: docs/guide/07_settings_migration.md
    llm_provider: Literal[
        "anthropic", "openai", "ollama", "local", "dummy",
        "deepseek", "grok",
    ] = Field(
        default="anthropic",
        description=(
            "[DEPRECATED] Legacy SDK path routing input. Pool path "
            "trusts only yaml tier.provider. Un-migrated paths "
            "(context_divider / text_classifier / ner_legacy / "
            "code_summarizer) still follow this envvar -- becomes "
            "irrelevant once callers inject the pool. Field will be "
            "removed after full consolidation."
        ),
    )
    llm_model: str = Field(
        default="claude-sonnet-4-6",
        description=(
            "[DEPRECATED] LLM model name for legacy path. Pool path "
            "uses yaml tier.model. LLM_PROVIDER and model must be "
            "vendor-consistent (e.g. provider=deepseek + "
            "model=claude-sonnet-4-6 will be rejected). Field will "
            "be removed after full consolidation."
        ),
    )
    ner_split_mode: Literal["auto", "always", "never"] = Field(
        default="auto",
        description=(
            "NER/Summary split-call policy. "
            "auto = decide based on GPU VRAM (>=40GB -> combined, "
            "<40GB -> split), always = always split, never = always "
            "combined. API providers (anthropic/openai) always use "
            "combined calls regardless of this setting."
        ),
    )
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    openai_api_key: str = Field(default="", description="OpenAI API key")
    # DEPRECATED: yaml tier.base_url / provider mapping is authoritative.
    #   This field is only effective in legacy paths without pool
    #   injection -- logs a warning when used (llm_routing.py).
    openai_base_url: str = Field(
        default="",
        description=(
            "[DEPRECATED] Ignored by pool path. Uses yaml "
            "tier.base_url or provider-specific default."
        ),
    )
    gemini_api_key: str = Field(
        default="",
        description=(
            "Gemini API key. Injected into OpenAI SDK when "
            "LLM_MODEL=gemini-*."
        ),
    )
    deepseek_api_key: str = Field(
        default="",
        description=(
            "DeepSeek API key. Used when LLM_PROVIDER=deepseek or "
            "LLM_MODEL=deepseek-* (OpenAI SDK + "
            "base_url=https://api.deepseek.com/v1). Very permissive "
            "with fiction / dark descriptions. Lowest cost ($0.14/M "
            "in)."
        ),
    )
    xai_api_key: str = Field(
        default="",
        description=(
            "xAI (Grok) API key. Used when LLM_PROVIDER=grok or "
            "LLM_MODEL=grok-* (OpenAI SDK + "
            "base_url=https://api.x.ai/v1). Very permissive with "
            "fiction / dark descriptions. Limited Korean-language "
            "validation."
        ),
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server URL",
    )

    # ------------------------------------------------------------------
    # Vector2Text dedicated LLM (None = use default LLM)
    # ------------------------------------------------------------------
    llm_provider_vector2text: Literal[
        "anthropic", "openai", "ollama", "deepseek", "grok",
    ] | None = Field(
        default=None,
        description="Dedicated LLM for Vector2Text. None = use llm_provider",
    )
    llm_model_vector2text: str | None = Field(
        default=None,
        description="Dedicated model for Vector2Text. None = use llm_model",
    )
    llm_api_key_vector2text: str = Field(
        default="",
        description="Dedicated API key for Vector2Text. Falls back if empty",
    )

    # ------------------------------------------------------------------
    # Code Summary dedicated LLM (None = use default LLM)
    # ------------------------------------------------------------------
    llm_provider_code_summary: Literal[
        "anthropic", "openai", "ollama", "deepseek", "grok",
    ] | None = Field(
        default=None,
        description="Dedicated LLM for code summary. None = use llm_provider",
    )
    llm_model_code_summary: str | None = Field(
        default=None,
        description="Dedicated model for code summary. None = use llm_model",
    )
    llm_api_key_code_summary: str = Field(
        default="",
        description="Dedicated API key for code summary. Falls back if empty",
    )

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    embedding_provider: Literal["openai", "local", "dummy", "onnx"] = Field(
        default="local",
        description=(
            "Embedding provider. openai = API, "
            "local = sentence-transformers (torch), "
            "onnx = onnxruntime (no torch, portable deployment), "
            "dummy = deterministic hash-seeded vectors for debugging. "
            "Default is local/BAAI/bge-m3 (1024) — the same fingerprint that "
            "DB creation (cli.init / project registry) stamps, so a fresh "
            "install with no EMBEDDING_* env opens its own DB without tripping "
            "the embedding fingerprint guard. The portable bundle overrides "
            "this to onnx via env. Previously defaulted to openai/1536, which "
            "mismatched bge-m3 DBs and 500'd every data request."
        ),
    )
    embedding_model: str = Field(
        default="BAAI/bge-m3",
        description="Embedding model name (default matches DB-creation default).",
    )
    embedding_dim: int = Field(
        default=1024,
        gt=0,
        description="Embedding vector dimension (1024 = BAAI/bge-m3).",
    )
    embedding_onnx_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EMBEDDING_ONNX_PATH", "embedding_onnx_path",
        ),
        description=(
            "ONNX model path when EMBEDDING_PROVIDER=onnx. If a "
            "directory, looks for `model.onnx` + `tokenizer.json` "
            "inside; if a file, uses it as model.onnx (tokenizer.json "
            "from same folder). Points to the bundled model in the "
            "portable zip (`models/bge-m3-onnx`)."
        ),
    )
    embedding_onnx_max_length: int = Field(
        default=512,
        gt=0,
        description=(
            "ONNX tokenizer truncation max length. K2G summaries are "
            "short so 512 is sufficient (fast). Increase for long "
            "document embeddings to improve alignment with torch "
            "BGE-M3."
        ),
    )

    # ------------------------------------------------------------------
    # Graph DB (All-in-One: Postgres+pgvector or SQLite+sqlite-vec)
    # Kuzu / Neo4j / Qdrant moved to legacy/; only new backends.
    # ------------------------------------------------------------------
    graph_db_provider: Literal["postgres", "sqlite"] = Field(
        default="sqlite",
        description=(
            "postgres = Postgres+pgvector (All-in-One DB), "
            "sqlite = SQLite+sqlite-vec (local file All-in-One DB, "
            "default)"
        ),
    )
    # Explicit backend override -- lets in-memory Settings (desktop
    # app's set_active_settings) authoritatively express the backend.
    # ``_resolve_backend_mode`` auto-detects via ``os.environ`` (cannot
    # distinguish default sqlite from explicit sqlite), so the desktop
    # path running only with in-memory Settings always falls back to
    # sqlite. When this field is set ("sqlite"/"postgres"), it takes
    # precedence over env detection. None = existing env auto-detect
    # (MCP/CLI path unchanged).
    backend_mode: Literal["postgres", "sqlite"] | None = Field(
        default=None,
        description=(
            "Explicit backend override. None = env auto-detect "
            "(factory _resolve_backend_mode). Used by desktop "
            "in-memory Settings."
        ),
    )
    postgres_graph_dsn: str | None = Field(
        default=None,
        description=(
            "PostgresGraphStore DSN (used when "
            "graph_db_provider=postgres). Falls back to postgres_dsn "
            "if unset."
        ),
    )
    sqlite_all_in_one_path: str = Field(
        default="",
        description=(
            "Shared DB file path for SqliteGraphStore / "
            "SqliteVectorStore (used when graph_db_provider=sqlite "
            "or vector_store_provider=sqlite). Defaults to "
            "DATA_DIR/k2g_all_in_one.db. Separate from "
            "content_store's sqlite_path (content_store.db) to "
            "avoid role confusion."
        ),
    )

    # ------------------------------------------------------------------
    # Vector Store (shares or separates from All-in-One backend)
    # ------------------------------------------------------------------
    vector_store_provider: Literal["postgres", "sqlite"] = Field(
        default="sqlite",
        description=(
            "postgres = Postgres+pgvector (All-in-One DB), "
            "sqlite = SQLite+sqlite-vec (local file All-in-One DB, "
            "default)"
        ),
    )
    postgres_vector_dsn: str | None = Field(
        default=None,
        description=(
            "PgVectorStore DSN (used when "
            "vector_store_provider=postgres). Falls back to "
            "postgres_graph_dsn -> postgres_dsn if unset."
        ),
    )

    # ------------------------------------------------------------------
    # Content Store (PostgreSQL / SQLite)
    # ------------------------------------------------------------------
    content_store_mode: Literal["postgres", "sqlite"] = Field(
        default="sqlite",
        description="postgres = PostgreSQL server, sqlite = local file",
    )
    postgres_dsn: str = Field(
        default="postgresql://k2g:k2g-password@localhost:5432/k2g",
        validation_alias=AliasChoices(
            "K2G_POSTGRES_DSN", "POSTGRES_DSN", "postgres_dsn",
        ),
        description=(
            "PostgreSQL DSN. K2G_POSTGRES_DSN env recommended. "
            "When set, graph + vector + content all use PG."
        ),
    )
    sqlite_path: str = Field(
        default="",
        description=(
            "SQLite DB file path. Defaults to "
            "DATA_DIR/content_store.db if unset."
        ),
    )

    # ------------------------------------------------------------------
    # Covenant Store (legacy separate DB + main DB integration)
    # ------------------------------------------------------------------
    covenant_db_path: str = Field(
        default="",
        description=(
            "Legacy covenant SQLite DB file path. Defaults to "
            "DATA_DIR/covenant.db. Main DB integration is "
            "recommended -- when covenant_in_main_db=True this "
            "path is only referenced for *legacy migration*."
        ),
    )
    covenant_in_main_db: bool = Field(
        default=True,
        description=(
            "True = use k2g_covenant and related tables in the main "
            "DB (sqlite/postgres). False = keep the legacy separate "
            "covenant.db SQLite file. Default True -- legacy "
            "environments should run "
            "scripts/migrate_covenant_db.py once on first activation."
        ),
    )

    # ------------------------------------------------------------------
    # Object Storage
    # ------------------------------------------------------------------
    object_storage_provider: Literal["local", "s3"] = Field(default="local")
    object_storage_dir: str = Field(
        default="",
        description="Local object storage root. Defaults to DATA_DIR/objects",
    )
    aws_access_key_id: str = Field(default="")
    aws_secret_access_key: str = Field(default="")
    s3_bucket: str = Field(default="k2g-objects")
    s3_region: str = Field(default="ap-northeast-2")

    # ------------------------------------------------------------------
    # Raw Archive + Wiki (BP-27)
    # ------------------------------------------------------------------
    raw_archive_enabled: bool = Field(
        default=True,
        description="Enable raw archive (content-addressable blobs + manifests)",
    )
    raw_archive_dir: str = Field(
        default="",
        description="L0 blobs/wiki root. Defaults to DATA_DIR/raw_archive",
    )
    raw_archive_manifests_dir: str = Field(
        default="",
        description="Event/source manifest root. Defaults to DATA_DIR/manifests",
    )
    raw_archive_l05_ttl_days: int = Field(
        default=30,
        gt=0,
        description="L0.5 tier TTL in days. P1 is dry-run identification only.",
    )
    wiki_enabled: bool = Field(
        default=True,
        description="Enable human-readable wiki markdown rendering",
    )
    wiki_min_entity_refs: int = Field(
        default=5,
        gt=0,
        description="Minimum reference count for auto-generating entity pages",
    )
    wiki_event_triage_threshold: str = Field(
        default="important",
        description="Minimum triage level for creating wiki event pages",
    )

    community_max_entities_per_event: int = Field(
        default=50,
        gt=1,
        description=(
            "Upper-bound guard on pairwise entity_connection edges "
            "per event. If the entity count for an event reaches "
            "this value, entity_connection creation is skipped "
            "(avoids N(N-1)/2 explosion). Shared by both the "
            "producer and MCP remember ingest paths "
            "(env: COMMUNITY_MAX_ENTITIES_PER_EVENT). Not a total "
            "cap but a single-event pathology guard to protect "
            "Leiden community graph density."
        ),
    )

    community_recompute_min_interval_sec: int = Field(
        default=30,
        ge=0,
        description=(
            "In-process debounce for fire-and-forget community "
            "recompute after ingest (minimum interval per "
            "kind+domain pair, in seconds). Guards against rapid "
            "successive ingests flooding recompute/connection "
            "creation. 0 = no debounce "
            "(env: COMMUNITY_RECOMPUTE_MIN_INTERVAL_SEC)."
        ),
    )

    # ------------------------------------------------------------------
    # Recording (BP-29 Stop-hook turn cache on/off)
    # ------------------------------------------------------------------
    recording_enabled: bool = Field(
        default=True,
        description=(
            "Default for stop-hook turn cache recording. false = "
            "k2g-cache-turn is no-op. Controlled per working "
            "folder via .mwf."
        ),
    )

    # ------------------------------------------------------------------
    # Turn Cache (BP-29)
    # ------------------------------------------------------------------
    cache_dir: str = Field(
        default="",
        description="Turn NDJSON cache root. Defaults to DATA_DIR/cache/turns",
    )
    cache_processed_dir: str = Field(
        default="",
        description=(
            "Path for processed NDJSON files. Defaults to "
            "DATA_DIR/cache/processed"
        ),
    )

    # ------------------------------------------------------------------
    # Doxygen intermediate output
    # ------------------------------------------------------------------
    doxygen_output_dir: str = Field(
        default="",
        description=(
            "Doxygen XML intermediate output root. Defaults to "
            "DATA_DIR/doxygen_output"
        ),
    )

    # ------------------------------------------------------------------
    # Document/Text batch segment intermediate output
    # ------------------------------------------------------------------
    segments_dir: str = Field(
        default="",
        description=(
            "Root for ingest_doc_batch / ingest_text_batch segment "
            "and NER JSON output. Defaults to DATA_DIR/segments. "
            "Falls back to this value if ingestion.segments_dir in "
            "YAML is empty or missing."
        ),
    )
    build_stage_dir: str = Field(
        default="",
        description=(
            "Producer/Loader staging root. Defaults to "
            "DATA_DIR/build_stage. Each session writes "
            "events.ndjson, entities.ndjson, groups.ndjson, "
            "embeddings.npy, manifest.json, produce.state under "
            "build_stage_dir/<session_id>/<domain>/."
        ),
    )

    # ------------------------------------------------------------------
    # VCS extract output
    # ------------------------------------------------------------------
    vcs_output_dir: str = Field(
        default="",
        description=(
            "VcsBatchExtractor output root. Defaults to DATA_DIR/vcs. "
            "Output files: vcs_output_dir/<domain>/<repo_id>.json."
        ),
    )

    # ------------------------------------------------------------------
    # Debug (MCP file log)
    # ------------------------------------------------------------------
    debug_mode: bool = Field(
        default=False,
        description=(
            "When true, raises MCP server log level to DEBUG and "
            "writes detailed logs to {DATA_DIR}/mcp_debug.log."
        ),
    )

    # ------------------------------------------------------------------
    # Cache (Redis)
    # ------------------------------------------------------------------
    redis_url: str = Field(
        default="",
        description="Redis connection URL (empty = use in-memory cache)",
    )

    # ------------------------------------------------------------------
    # General
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO")

    # ------------------------------------------------------------------
    # BP-51 Phase A — Logging (JSON Lines + rotating file)
    # ------------------------------------------------------------------
    log_format: str = Field(
        default="text",
        description=(
            "Log output format. 'json' = JSON Lines (recommended "
            "for production), 'text' = plain (default)."
        ),
    )
    log_output: str = Field(
        default="stdout",
        description="Log output target. 'stdout' / 'file' / 'both'. Default stdout.",
    )
    log_file_dir: str = Field(
        default="",
        description="Log file directory. Defaults to DATA_DIR/logs.",
    )
    log_retention_days: int = Field(
        default=90,
        gt=0,
        description="JSONL file rotation retention period in days (default 90).",
    )
    log_file_hostname_suffix: bool = Field(
        default=False,
        description="Append hostname suffix to file name for shared storage (NFS).",
    )
    log_file_compress: bool = Field(
        default=False,
        description="Gzip compress rotated files (requires gunzip to decode).",
    )
    mcp_telemetry_retention_days: int = Field(
        default=30,
        gt=0,
        description="MCP / SQL call telemetry JSONL retention in days (default 30).",
    )

    # ------------------------------------------------------------------
    # BP-51 Phase C — Cost reconciliation / calibration
    # ------------------------------------------------------------------
    balance_poll_interval_min: int = Field(
        default=60,
        gt=0,
        description="provider_balance_snapshot polling interval in minutes (default 60).",
    )
    calibration_window_days: int = Field(
        default=7,
        gt=0,
        description="L3 calibration regression window in days (default 7).",
    )
    calibration_r2_threshold: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="L3 fitting acceptance threshold -- rejects INSERT if below r2.",
    )
    calibration_refresh_max_age_hours: int = Field(
        default=24,
        gt=0,
        description="ensure_calibration_fresh() auto-refresh threshold in hours (default 24).",
    )

    @model_validator(mode="before")
    @classmethod
    def strip_env_comments(cls, values: Any) -> Any:
        """Strip inline comments (#) and whitespace from env string values."""
        if isinstance(values, dict):
            for k, v in values.items():
                if isinstance(v, str) and "#" in v:
                    values[k] = v.split("#")[0].strip()
                elif isinstance(v, str):
                    values[k] = v.strip()
        return values

    @model_validator(mode="before")
    @classmethod
    def apply_data_dir(cls, values: Any) -> Any:
        """Auto-apply DATA_DIR to sub-paths that are not explicitly set."""
        if not isinstance(values, dict):
            return values
        data_dir = values.get("data_dir", "./data")
        if isinstance(data_dir, str) and "#" in data_dir:
            data_dir = data_dir.split("#")[0].strip()
        # An empty DATA_DIR would make the sub-paths below "/objects", "/logs",
        # … which resolve to the *drive root* (e.g. F:\objects). Fall back to the
        # default so local object storage / logs always land in a real folder —
        # e.g. a Postgres project whose entry carries no local anchor.
        if not isinstance(data_dir, str) or not data_dir.strip():
            data_dir = "./data"
            values["data_dir"] = data_dir

        _defaults = {
            "sqlite_path": "content_store.db",
            "sqlite_all_in_one_path": "k2g_all_in_one.db",
            "covenant_db_path": "covenant.db",
            "object_storage_dir": "objects",
            "raw_archive_dir": "raw_archive",
            "raw_archive_manifests_dir": "manifests",
            "cache_dir": "cache/turns",
            "cache_processed_dir": "cache/processed",
            "doxygen_output_dir": "doxygen_output",
            "segments_dir": "segments",
            "build_stage_dir": "build_stage",
            "vcs_output_dir": "vcs",
            # BP-51 — Logging
            "log_file_dir": "logs",
        }
        for field, suffix in _defaults.items():
            if not values.get(field):
                values[field] = f"{data_dir}/{suffix}"
        return values

    @field_validator("embedding_dim", mode="before")
    @classmethod
    def parse_embedding_dim(cls, v: Any) -> int:
        return int(v)

    @model_validator(mode="after")
    def validate_api_keys(self) -> "Settings":
        # NOTE: anthropic validation for `llm_provider` intentionally removed.
        # After LLM pool routing migration (tier.provider from yaml), this
        # field is a [DEPRECATED] remnant. Processes that don't call LLMs
        # (mweft-ui / memory read path) only produced noisy warnings.
        # Actual anthropic SDK calls raise on missing key at the call site
        # (factory / pool).
        if self.embedding_provider == "openai" and not self.openai_api_key:
            logger.warning(
                "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set."
            )
        return self


# ---------------------------------------------------------------------------
# Active-settings holder (desktop in-memory project switch)
# ---------------------------------------------------------------------------
# Default is env-based ``Settings()`` (MCP/CLI path unchanged). The desktop
# app switches projects by swapping in an in-memory Settings instance via
# ``set_active_settings()``, without modifying os.environ. When
# ``_active_settings`` is None, behavior is 100% identical to before
# (env-based default, lru cache).
_active_settings: "Settings | None" = None


@lru_cache(maxsize=1)
def _default_settings() -> "Settings":
    """Env-based default Settings (MCP/CLI path). Created once, cached."""
    return Settings()


def get_settings() -> Settings:
    """Return the active Settings.

    Returns the instance explicitly injected via
    ``set_active_settings()`` if present, otherwise the env-based
    default (``_default_settings``).
    """
    return _active_settings if _active_settings is not None else _default_settings()


def set_active_settings(settings: "Settings | None") -> None:
    """Replace (or clear with None) the active Settings.

    Called when the desktop app switches projects -- builds an
    in-memory Settings from a registry entry and injects it.
    Passing None reverts to the env-based default.
    """
    global _active_settings
    _active_settings = settings


def _clear_settings_cache() -> None:
    """``get_settings.cache_clear()`` compatible -- clears both the
    env-based default cache and the active override (full reset;
    next call re-reads env).

    Previously ``get_settings`` was ``@lru_cache`` and exposed
    ``get_settings.cache_clear()``. Production code (project_init
    activate env-strip) and many tests call that interface -- the
    holder replacement preserves the same signature.
    """
    global _active_settings
    _active_settings = None
    _default_settings.cache_clear()


# Preserve the legacy ``get_settings.cache_clear()`` call surface.
get_settings.cache_clear = _clear_settings_cache  # type: ignore[attr-defined]


def get_settings_from(path: str | Path = ".mwf") -> Settings:
    """Load and return settings from the .mwf file at the given path.

    System environment variables take precedence over .mwf file values.
    This function is not cached -- each call creates a new instance.

    If only .chronoGraph exists in the same directory (no .mwf), it is
    auto-renamed once. Falls back to reading .chronoGraph directly if
    rename fails.
    """
    path = Path(path)
    path = _maybe_rename_legacy_chronograph(path)
    if not path.exists():
        logger.warning("Config file not found: %s. Using defaults.", path)
    return Settings(_env_file=path)


def resolve_segments_dir(configured: str) -> Path:
    """Use Settings.segments_dir (DATA_DIR fallback) if `configured` is empty.

    Shared helper so ingest_doc_batch and ingest_text_batch use the
    same path resolution.
    """
    if configured:
        return Path(configured).resolve()
    return Path(get_settings().segments_dir).resolve()


def resolve_build_stage_dir(configured: str = "") -> Path:
    """Resolve build_stage_dir path.

    Uses Settings.build_stage_dir (DATA_DIR fallback) if `configured`
    is empty. Follows the same helper pattern as segments_dir to
    allow CLI argument or YAML override.
    """
    if configured:
        return Path(configured).resolve()
    return Path(get_settings().build_stage_dir).resolve()


# ---------------------------------------------------------------------------
# Domain configuration (YAML-based)
# ---------------------------------------------------------------------------

class EntityTypeConfig(BaseModel):
    """Per-domain entity type definition."""

    model_config = {"extra": "ignore"}

    name: str
    description: str = ""
    examples: list[str] = Field(default_factory=list)


class VcsInputConfig(BaseModel):
    """VCS ingestion domain configuration."""

    model_config = {"extra": "ignore"}

    repo_path: str = "."
    branch: str = "main"
    mode: Literal["direct", "dump"] = "direct"
    dump_path: str | None = None
    incremental: bool = True
    vcs_type: Literal["git", "svn"] = "git"


class GroupTreeNode:
    """
    A single node in the group tree.
    Parsed from YAML to represent the hierarchical structure.
    """

    def __init__(
        self,
        name: str,
        level: int,
        domain: str,
        children: list["GroupTreeNode"] | None = None,
    ) -> None:
        self.name = name
        self.level = level
        self.domain = domain
        self.children: list[GroupTreeNode] = children or []

    def flatten(self) -> list["GroupTreeNode"]:
        """Flatten the tree and return all nodes."""
        result: list[GroupTreeNode] = [self]
        for child in self.children:
            result.extend(child.flatten())
        return result

    def __repr__(self) -> str:
        return f"GroupTreeNode(name={self.name!r}, level={self.level})"


class DomainConfig:
    """
    Domain configuration loaded from a YAML file.
    Used for NER prompt generation, group tree initialization, etc.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._raw = data
        self.domain: str = data["domain"]
        self.description: str = data.get("description", "")
        self.language: str = data.get("language", "ko")
        self.entity_types: list[EntityTypeConfig] = [
            EntityTypeConfig(**et) for et in data.get("entity_types", [])
        ]
        self.group_tree: list[GroupTreeNode] = self._parse_group_tree(
            data.get("groups", []), self.domain
        )
        self.ner_instructions: str = data.get("ner_instructions", "")
        self.embedding_config: dict[str, Any] = data.get("embedding", {})

        # Relation configuration
        self.event_to_event: dict[str, Any] = data.get("event_to_event", {})
        self.entity_to_entity: dict[str, Any] = data.get("entity_to_entity", {})

        # Control Node V2 planning configuration
        self.planning: dict[str, Any] = data.get("planning", {})

        # Control Node (weighted composite graph) configuration
        self.control_node: dict[str, Any] = data.get("control_node", {})

        # VCS Ingestion configuration (optional)
        vcs_raw = data.get("vcs")
        self.vcs: VcsInputConfig | None = (
            VcsInputConfig(**vcs_raw) if isinstance(vcs_raw, dict) else None
        )

        # Per-domain Extractor mapping (extension -> extractor_kind).
        # Example: novel.yaml ``extractors: {txt: txt_divider}`` routes
        # .txt to TxtDividerExtractor. Unset = classifier default mapping.
        self.extractors: dict[str, str] = data.get("extractors", {})

    def get_sequential_config(self) -> dict[str, Any]:
        """EVENT_SEQUENTIAL_NEXT creation configuration."""
        return self.event_to_event.get("sequential", {"chunk": True})

    def get_jaccard_config(self) -> dict[str, Any]:
        """EVENT_JACCARD_CONNECTED creation configuration.

        Merges defaults with YAML overrides. Only defines keys that
        correspond 1:1 with the ``compute_jaccard_connected``
        signature in JaccardPhase / backend.

        - ``theta_e`` / ``theta_g`` / ``min_group_intersection`` --
          thresholds (determine output)
        - ``max_entity_degree`` / ``max_group_degree`` -- high-degree
          stopword cap (reduces input candidates; prevents hash-join
          blowup at 28M+ scale)
        - ``work_mem`` -- postgres-backend-only ``SET LOCAL`` value
        - ``stop_groups`` -- group stop-list (injected from
          covenant's ``vcs.jaccard_exclude_patterns``)

        Measured (K2G self-build, 10075 events / 29410 entities):
        cap=200 excludes only 13 noisy entities, candidates
        28.9M -> 674k (43x reduction), false exclusions 0.
        """
        defaults: dict[str, Any] = {
            "scope": "group",
            "threshold": 0.3,                # legacy compat (not used by current code)
            "theta_e": 0.4,
            "theta_g": 0.3,
            "min_group_intersection": 2,
            "max_entity_degree": 200,
            "max_group_degree": 200,
            "work_mem": "128MB",
            # Also exclude entities with user_tag='stopword' from
            # candidates -- removes user-curated noise in the mid-degree
            # range (cap<=degree, e.g. 100~200) that degree cap alone
            # does not catch. Same default as
            # leiden_community.build_entity_graph. False = ignore
            # user_tag (legacy behavior).
            "exclude_stopwords": True,
        }
        return {**defaults, **self.event_to_event.get("jaccard", {})}

    def get_connection_config(self) -> dict[str, Any]:
        """ENTITY_CONNECTION creation configuration."""
        return self.entity_to_entity.get("connection", {"noise_filter": 2})

    def get_control_node_config(self) -> dict[str, Any]:
        """
        Control Node discovery configuration.

        Synthesises EVENT_SEQUENTIAL_NEXT and EVENT_JACCARD_CONNECTED via a
        weighted composite graph to extract Control Node candidates.
        """
        defaults: dict[str, Any] = {
            "seq_weight": 0.5,
            "jac_weight": 0.5,
            "edge_threshold": 0.3,
            "min_cluster_size": 2,
        }
        return {**defaults, **self.control_node}

    def get_planning_config(self) -> dict[str, Any]:
        """Control Node V2 planning configuration."""
        defaults: dict[str, Any] = {
            "strategy": "default",
            "max_plan_depth": 10,
            "template_mining": True,
            "realization_threshold": 0.7,
            "external_search": {
                "enabled": True,
                "threshold": 0.3,
                "max_results": 3,
            },
            "validation": {
                "entity_check": True,
                "confidence_min": 0.3,
            },
        }
        merged = {**defaults, **self.planning}
        if "external_search" in self.planning:
            merged["external_search"] = {
                **defaults["external_search"],
                **self.planning["external_search"],
            }
        if "validation" in self.planning:
            merged["validation"] = {
                **defaults["validation"],
                **self.planning["validation"],
            }
        return merged

    @classmethod
    def from_yaml(cls, path: str | Path) -> "DomainConfig":
        """Load a DomainConfig from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Domain configuration file not found: {path}")
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainConfig":
        """Create a DomainConfig from a dictionary."""
        return cls(data)

    def _parse_group_tree(
        self,
        raw_groups: list[dict[str, Any]],
        domain: str,
        parent_level: int = -1,
    ) -> list[GroupTreeNode]:
        """Recursively parse the group tree."""
        nodes: list[GroupTreeNode] = []
        for raw in raw_groups:
            level = raw.get("level", parent_level + 1)
            node = GroupTreeNode(
                name=raw["name"],
                level=level,
                domain=domain,
                children=self._parse_group_tree(
                    raw.get("children", []),
                    domain,
                    level,
                ),
            )
            nodes.append(node)
        return nodes

    def get_all_group_names(self) -> list[str]:
        """Return all group names including leaf nodes."""
        names: list[str] = []
        for root in self.group_tree:
            for node in root.flatten():
                names.append(node.name)
        return names

    def get_entity_type_names(self) -> list[str]:
        return [et.name for et in self.entity_types]

    def get_entity_type_examples(self) -> dict[str, list[str]]:
        return {et.name: et.examples for et in self.entity_types}

    def build_ner_system_prompt(self) -> str:
        """Build the system prompt used by the NER LLM."""
        entity_type_lines = "\n".join(
            f"  - {et.name}: {et.description} (e.g. {', '.join(et.examples[:3])})"
            for et in self.entity_types
        )
        # When request_summary_coverage is enabled, ask the LLM to self-assess
        # the *raw compression ratio of the summary* (0.0-1.0). Stored in
        # content_store.inline_meta for correlation analysis with raw_fetch_ratio.
        ner_cfg = self._raw.get("ner", {}) if isinstance(self._raw, dict) else {}
        request_coverage = bool(ner_cfg.get("request_summary_coverage", False))
        coverage_field = (
            ',\n  "summary_coverage": 0.7'
            if request_coverage else ""
        )
        coverage_instruction = (
            "\n- summary_coverage (0.0-1.0): rate how well the summary represents "
            "the raw content's key meaning. 0.0 = critical info missing, "
            "1.0 = summary fully captures meaning. Required field."
        ) if request_coverage else ""
        prompt = f"""You are a professional NER (Named Entity Recognition) system that extracts knowledge graph components from text.

Domain: {self.domain}
Language: {self.language}

## Entity types to extract
{entity_type_lines}

## Output format (JSON)
Respond ONLY with the following JSON format:
{{
  "entities": [
    {{"name": "entity_name", "type": "type_name"}}
  ],
  "event": {{
    "summary": "Core summary of this scene/event (include relationships and mood)",
    "timestamp": "time (null if unknown)"
  }},
  "content": {{
    "content_type": "text/plain",
    "inline_meta": {{"language": "{self.language}"}}
  }}{coverage_field}
}}

## Additional instructions
{self.ner_instructions}{coverage_instruction}

## Mandatory rules
- NEVER repeat the same entity name. Each entity must appear exactly once.
- Output ONLY valid JSON. No markdown, no extra text.
- BP-61 — If you cannot identify entities with high confidence from the
  input text, return entities=[]. DO NOT invent or hallucinate entities.
- BP-61 — Each entity name MUST be grounded in the input text: at least
  half of its key tokens (after camelCase / snake_case / whitespace split)
  MUST appear in the input. Names with no textual basis will be rejected
  by post-validation."""
        return prompt

    def build_ner_only_prompt(self) -> str:
        """NER-only prompt (extracts entities only, no summary)."""
        entity_type_lines = "\n".join(
            f"  - {et.name}: {et.description} (e.g. {', '.join(et.examples[:3])})"
            for et in self.entity_types
        )
        return f"""Extract entities from the text.

Domain: {self.domain}
Language: {self.language}

## Entity types to extract
{entity_type_lines}

## Output format (JSON)
Respond ONLY with the following JSON format:
{{
  "entities": [
    {{"name": "entity_name", "type": "type_name"}}
  ],
  "timestamp": "time (null if unknown)"
}}

## Additional instructions
{self.ner_instructions}

## Mandatory rules
- NEVER repeat the same entity name. Each entity must appear exactly once.
- Extract at most 20 entities.
- Output ONLY valid JSON. No markdown, no extra text.
- BP-61 — If you cannot identify entities with high confidence from the
  input text, return entities=[]. DO NOT invent or hallucinate entities.
- BP-61 — Each entity name MUST be grounded in the input text: at least
  half of its key tokens MUST appear in the input. Names with no textual
  basis will be rejected by post-validation."""

    def build_summary_prompt(self) -> str:
        """Summary-only prompt (generates event summary only)."""
        return f"""Summarize the core content of the text in 1-2 sentences in {self.language}.

Domain: {self.domain}

## Output format (JSON)
Respond ONLY with the following JSON format:
{{
  "summary": "Core summary of this scene/event"
}}

Output ONLY valid JSON. No markdown, no extra text."""

    def __repr__(self) -> str:
        return f"DomainConfig(domain={self.domain!r})"
