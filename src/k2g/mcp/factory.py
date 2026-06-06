"""K2G MCP -- dependency factory (reconstruct_module).

Builds the Deps bundle injected into read-only MCP tool handlers.
The write path (k2g_ingest_event) has been moved to the off-band
build CLI. Only DbStore + QueryService + embedding are prepared here.

Legacy Kuzu / Neo4j / Qdrant branches were absorbed into the db_store
module, so this file just calls ``DbStore.from_settings`` and
delegates internal selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Deps:
    """Read-only dependency bundle injected into MCP tool handlers.

    ``manifest`` (BuildManifestStore | None) and ``influence``
    (EventInfluenceService | None) are optional -- when no manifest DB
    exists or no build has run, they stay None and evidence fields are
    left empty.
    """

    db: Any              # k2g.db_store.DbStore
    graph: Any           # = db.graph (legacy compat alias)
    vector: Any          # = db.vector (legacy compat alias)
    embedding: Any
    query: Any           # k2g.reader.QueryService (default GraphQueryService)
    settings: Any
    manifest: Any = None    # BuildManifestStore | None
    influence: Any = None   # EventInfluenceService | None


def build_dependencies() -> Deps:
    """Build DbStore / Embedding / QueryService /
    BuildManifestStore / EventInfluenceService from ``get_settings()``.

    Each phase is timed via logger.info for startup-latency tracking
    in lazy-init environments.

    In SQLite mode, depending on ``K2G_USE_HUB`` policy, embedding
    may be injected as a ``ProxyEmbeddingClient`` (hub forward)
    instead of the in-process ``LocalEmbeddingClient``.
    """
    import logging as _logging
    import time as _time

    from k2g.core.config import get_settings
    from k2g.db_store import DbStore
    from k2g.reader import GraphQueryService
    from k2g.reader.influence import EventInfluenceService
    from k2g.updater.manifest import BuildManifestStore

    _log = _logging.getLogger("k2g.mcp.factory")

    t0 = _time.time()
    settings = get_settings()

    t = _time.time()
    db = DbStore.from_settings(settings)
    _log.info("build_dependencies: DbStore ready (%.2fs)", _time.time() - t)

    t = _time.time()
    embedding = _build_embedding_with_hub_policy(settings, _log)
    _log.info("build_dependencies: embedding ready (%.2fs)", _time.time() - t)

    # Assumes build_manifest.db is in the same data_dir. Missing is OK
    # (BuildManifestStore.__init__ auto-creates the directory/file).
    manifest: Any
    try:
        manifest = BuildManifestStore()
    except Exception as exc:  # noqa: BLE001
        _log.warning("build_dependencies: manifest store failed -- %s", exc)
        manifest = None

    query = GraphQueryService(db, manifest=manifest)
    influence = EventInfluenceService(db, manifest=manifest)
    _log.info("build_dependencies: all ready (%.2fs)", _time.time() - t0)

    return Deps(
        db=db,
        graph=db.graph,
        vector=db.vector,
        embedding=embedding,
        query=query,
        settings=settings,
        manifest=manifest,
        influence=influence,
    )


def build_plan_deps(deps: "Deps" | None = None) -> Any:
    """Assemble PlanDeps -- lazy import avoids hard dependency on the
    training layer.

    The training module (legacy location) has not yet been ported to
    the reconstruct layout, so the plan_tools import itself may fail.
    In that case a NotImplementedError is raised.
    """
    try:
        from k2g.mcp.plan_tools import PlanDeps
    except Exception as exc:
        raise NotImplementedError(
            "plan_tools unavailable until the training layer "
            f"(HDBSCAN/ETG/Plan) is ported. Cause: {exc}"
        ) from exc

    d = deps or build_dependencies()
    settings = d.settings
    provider = getattr(settings, "llm_provider", "anthropic")
    model = getattr(settings, "llm_model", "claude-sonnet-4-6")
    llm_client = _build_llm_client(settings)
    return PlanDeps(
        graph=d.graph,
        embedding=d.embedding,
        llm_client=llm_client,
        llm_provider=provider,
        llm_model=model,
    )


def _build_embedding_with_hub_policy(settings: Any, _log: Any) -> Any:
    """Choose between in-process embedding and the BP-75 hub proxy.

    Decision matrix (driven by env + Settings):

    - PG mode → always in-process (PG has no single-writer / shared
      embedding rationale; existing behaviour preserved).
    - ``K2G_USE_HUB=false`` → always in-process (legacy / opt-out).
    - ``K2G_USE_HUB=true`` → hub required; raise if not discoverable.
    - ``K2G_USE_HUB=auto`` (default) → forward iff hub is discoverable.

    Hub discovery: either ``K2G_HUB_URL`` env or ``hub.json`` under
    ``K2G_PROJECT_DIR``.  When both are absent and the mode is ``auto``
    or ``true``, we fall through to in-process (auto) or fail (true).
    """
    import os as _os
    from pathlib import Path as _Path

    from k2g.embedding.client import create_embedding_client
    from k2g.mcp.proxy.mode import (
        discover_hub_info,
        get_hub_url,
        get_hub_use_mode,
        is_sqlite_mode,
    )

    if not is_sqlite_mode(settings):
        return create_embedding_client(settings)

    use_mode = get_hub_use_mode()
    if use_mode == "false":
        return create_embedding_client(settings)

    project_dir_env = _os.environ.get("K2G_PROJECT_DIR", "").strip()
    project_dir = _Path(project_dir_env) if project_dir_env else None

    hub_url = get_hub_url(project_dir)
    if hub_url is None:
        if use_mode == "true":
            raise RuntimeError(
                "K2G_USE_HUB=true but no hub is discoverable. Start "
                "mweft-ui or set K2G_USE_HUB=false / K2G_HUB_URL.",
            )
        _log.info(
            "BP-75: hub not discovered (mode=auto), falling back to "
            "in-process embedding.",
        )
        return create_embedding_client(settings)

    token: str | None = None
    if project_dir is not None:
        info = discover_hub_info(project_dir)
        if info is not None:
            token = info.token

    _log.info("BP-75: using ProxyEmbeddingClient → %s", hub_url)
    from k2g.mcp.proxy.embedding_proxy import ProxyEmbeddingClient

    return ProxyEmbeddingClient(
        hub_url,
        dim=int(getattr(settings, "embedding_dim", 1536)),
        token=token,
    )


def _build_llm_client(settings: Any) -> Any:
    provider = getattr(settings, "llm_provider", "anthropic")
    try:
        if provider == "anthropic":
            import anthropic
            return anthropic.Anthropic(api_key=getattr(settings, "anthropic_api_key", None))
        if provider == "openai":
            import openai
            return openai.OpenAI(api_key=getattr(settings, "openai_api_key", None))
        if provider == "ollama":
            import openai
            base = getattr(settings, "ollama_base_url", "http://localhost:11434/v1")
            return openai.OpenAI(base_url=base, api_key="ollama")
    except Exception:  # noqa: BLE001
        return None
    return None


__all__ = ["Deps", "build_dependencies", "build_plan_deps"]
