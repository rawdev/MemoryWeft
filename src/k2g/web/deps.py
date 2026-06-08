"""MWeft Manager web dependency injection.

Settings -> Stores -> Adapters initialization and FastAPI Depends wiring.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time
from typing import Any, Callable

from k2g.core.config import Settings

logger = logging.getLogger(__name__)


class StoreInitError(Exception):
    """Store/embedding initialization failed in a way the user can fix.

    The whole Manager UI shares one lazy ``_initialize()`` (graph + embedding +
    schema). Any exception there used to propagate as a bare HTTP 500
    ("Internal Server Error") on *every* data endpoint (tags / search / domain
    summary), with nothing telling the user what to fix. This carries a stable
    ``code`` + an actionable ``hint`` so the web layer can return a 503 the UI
    renders as a real message instead of a blank error. ``_initialized`` is left
    False on failure, so the next request retries once the config is corrected.
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def _classify_init_error(exc: Exception) -> StoreInitError:
    """Map a store-init exception to a stable code + actionable hint."""
    from k2g.db_store.embedding_guard import EmbeddingFingerprintMismatch

    if isinstance(exc, EmbeddingFingerprintMismatch):
        return StoreInitError(
            "embedding_fingerprint_mismatch",
            str(exc),
            hint=(
                "This database was created with a different embedding "
                "model/dimension than the one currently configured. Set "
                "EMBEDDING_MODEL / EMBEDDING_DIM (and EMBEDDING_PROVIDER) to "
                "match the values it was built with, or point this project at "
                "a fresh data directory."
            ),
        )
    msg = str(exc)
    if "OPENAI_API_KEY" in msg or "EMBEDDING_ONNX_PATH" in msg:
        return StoreInitError(
            "embedding_not_configured",
            msg,
            hint=(
                "The configured embedding provider is missing a required "
                "setting (API key or model path). Configure it, or switch "
                "EMBEDDING_PROVIDER to one that needs no external resource."
            ),
        )
    if isinstance(exc, ModuleNotFoundError) and "psycopg2" in msg:
        return StoreInitError(
            "postgres_driver_missing",
            msg,
            hint=(
                "This project uses a PostgreSQL backend but the psycopg2 driver "
                "is not installed. Install PostgreSQL support "
                "(pip install 'mweft[postgres]'), or use a SQLite project. The "
                "portable bundle ships it from v0.1.11 onward."
            ),
        )
    return StoreInitError(
        "store_init_failed",
        msg,
        hint="Store initialization failed. Check the server log for details.",
    )


def register_store_init_handler(app: Any) -> None:
    """Install the StoreInitError → HTTP 503 handler on a FastAPI app.

    Both entry points must register this or a store-init failure surfaces as a
    bare 500 on that app: the standalone web app (``k2g.web.app``) AND the
    per-project / desktop app (``k2g.ui_project.cli._build_app``, which the
    desktop ASGI bridge runs). ``detail`` is a string so the existing
    ``_fetchJson`` error display surfaces it directly.
    """
    from fastapi.responses import JSONResponse

    async def _handler(_request: Any, exc: StoreInitError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": exc.message,
                "code": exc.code,
                "hint": exc.hint,
                "error": "store_init_failed",
            },
        )

    app.add_exception_handler(StoreInitError, _handler)


def sanitize(obj: Any) -> Any:  # noqa: ANN401
    """Recursively convert Neo4j native types (DateTime etc.) to JSON-serializable types."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    # neo4j.time.DateTime / Date / Time — has iso_format() method
    if hasattr(obj, "iso_format"):
        return obj.iso_format()
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    return str(obj)

# ---------------------------------------------------------------------------
# Singleton instance store
# ---------------------------------------------------------------------------
_stores: dict[str, Any] | None = None
_llm_client: Any = None
_llm_provider: str = ""
_llm_model: str = ""
_event_brancher: Any = None
_ingestion_pipeline: Any = None
_training_store: Any = None
_control_node_builder: Any = None
_projection_engine: Any = None
_plan_engine: Any = None
_template_miner: Any = None

# Lazy-init guard (C2): the heavy store/model/schema build is deferred out of
# startup() to the first accessor call, so the server binds its port and serves
# the UI shell instantly. _init_lock serializes the one-time build under the
# FastAPI threadpool's concurrent first requests.
_initialized: bool = False
_init_lock = threading.Lock()

# Desktop in-process project switch hook. The desktop launcher
# (``k2g.desktop.cli``) sets this to ``switch.rebind_project``; when set, the
# ``/projects/activate`` route rebinds the active Settings *in this process*
# instead of spawning a new per-project server. None → web (browser) behaviour
# unchanged (spawn + redirect). An internal variable, not an env var.
desktop_rebind: "Callable[[str], str] | None" = None


def get_settings() -> Settings:
    """Return the active Settings.

    Single source of truth: delegates to ``core.config.get_settings``.
    The former web-only ``@lru_cache`` copy was removed so that desktop's
    ``set_active_settings`` swap is immediately reflected in web routes
    (eliminates cache desync).
    """
    from k2g.core.config import get_settings as _core_get_settings
    return _core_get_settings()


def get_llm_client(settings: Settings) -> tuple[Any, str, str]:
    """Create the Vector2Text-specific LLM client.

    Fallback: uses default LLM settings when Vector2Text-specific
    settings are not configured.

    Returns:
        (client, provider, model)
    """
    provider = settings.llm_provider_vector2text or settings.llm_provider
    model = settings.llm_model_vector2text or settings.llm_model

    # LLM SDKs (anthropic/openai) are optional in the OSS build — they are not
    # core deps. Generation (Vector2Text) needs them, but the Manager UI's
    # browse/search/community paths do not. Degrade gracefully to no-LLM when
    # the SDK is missing so startup() does not hard-fail.
    try:
        if provider == "anthropic":
            import anthropic
            key = settings.llm_api_key_vector2text or settings.anthropic_api_key
            return anthropic.Anthropic(api_key=key), provider, model

        elif provider == "openai":
            import openai
            key = settings.llm_api_key_vector2text or settings.openai_api_key
            return openai.OpenAI(api_key=key), provider, model

        elif provider == "ollama":
            import openai
            return openai.OpenAI(
                base_url=settings.ollama_base_url + "/v1",
                api_key="ollama",
            ), provider, model
    except ImportError as exc:
        logger.warning(
            "LLM SDK for provider %r not installed (%s) — generation disabled, "
            "Manager UI runs without LLM.", provider, exc,
        )
        return None, provider, model

    logger.warning("Unsupported LLM provider: %s, running without LLM.", provider)
    return None, provider, model


# Embedding clients are CACHED across project switches, keyed by their config
# (provider/model/dim/onnx). The client + its loaded model are *DB-independent*,
# and loading a local model (BGE-M3 ≈ 14s) dominates a switch — so re-entering a
# project (or switching between two projects that share an embedding config)
# reuses the warm model instead of reloading it. Without this, every desktop
# project switch threw the model away (``_stores`` is discarded) and the next
# search paid the full load again. Cleared only at process exit.
_embedding_cache: dict[tuple, Any] = {}


def _embedding_key(settings: Settings) -> tuple:
    return (
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_dim,
        getattr(settings, "embedding_onnx_path", None),
        getattr(settings, "embedding_onnx_max_length", None),
    )


def _get_embedding_client(settings: Settings) -> Any:
    """Return a cached embedding client for ``settings`` (build + cache on miss)."""
    from k2g.embedding.client import create_embedding_client

    key = _embedding_key(settings)
    client = _embedding_cache.get(key)
    if client is None:
        client = create_embedding_client(settings)
        _embedding_cache[key] = client
    return client


def init_stores(settings: Settings) -> dict[str, Any]:
    """Initialize Graph/Vector/Content/Object + Embedding stores.

    reconstruct_module: legacy ``k2g.stores.*`` branches were absorbed into
    ``DbStore.from_settings``. Supported backends:
    settings.graph_db_provider in {postgres, sqlite}, plus per-store
    vector_store_provider / content_store_mode. Kuzu/Neo4j/Qdrant moved
    to legacy/ and are not available from this path.

    Embedding client is cached by config via ``_get_embedding_client`` to
    avoid reloading the model (BGE-M3 ~ 14s) on project switches.
    """
    from k2g.db_store import DbStore

    db = DbStore.from_settings(settings)
    for method_name in ("setup_schema", "setup_training_schema", "setup_bp30_schema"):
        fn = getattr(db.graph, method_name, None)
        if fn is None:
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph.%s skip/fail: %s", method_name, exc)

    # Ensure the active project's save-domain exists in the registry so the
    # Manager's domain picker is never empty on a brand-new DB — a first-run
    # install shows "default" instead of "no domains". Idempotent
    # (INSERT OR IGNORE / ON CONFLICT) and best-effort: a read-only DB just
    # skips it.
    try:
        default_domain = (settings.user_memory_save_domain or "default").strip()
        register = getattr(db.graph, "register_domain", None)
        if default_domain and register is not None:
            register(default_domain)
    except Exception as exc:  # noqa: BLE001
        logger.debug("default-domain register skipped: %s", exc)

    # Embedding-client init is NON-FATAL. Semantic search and ingestion need it,
    # but browse / domain summary / tags / community are pure graph/SQL and must
    # keep working when embeddings can't load — e.g. a portable Windows build on
    # a box missing the MSVC runtime, where onnxruntime's native module won't
    # import. A failure here degrades just those two features instead of 500-ing
    # the whole Manager. Genuine graph-open failures (embedding fingerprint
    # mismatch) remain fatal above in DbStore.from_settings and surface as 503.
    embedding_client = None
    embedding_error = ""
    try:
        embedding_client = _get_embedding_client(settings)
    except Exception as exc:  # noqa: BLE001
        embedding_error = str(exc)
        logger.warning(
            "Embedding client unavailable — semantic search and ingestion are "
            "disabled; browse / summary / tags / community still work. %s",
            embedding_error,
        )

    return {
        "graph": db.graph,
        "vector": db.vector,
        "content": db.content,
        "objects": db.object,
        "embedding": embedding_client,
        "embedding_error": embedding_error,
        "_db": db,  # used when calling close()
    }


# reconstruct_module: EventBrancher / IngestionPipeline factory will be
# revived after the legacy adapter chain is re-ported. Currently a stub.


# ---------------------------------------------------------------------------
# Global initialization / cleanup
# ---------------------------------------------------------------------------

def startup() -> None:
    """Lifespan hook — arm lazy init (does NOT build stores).

    The actual store/model/schema build (Postgres connect + ~100 DDL round
    trips + BGE-M3 model load) is deferred to the first data request via
    :func:`_ensure_initialized`. This lets the UI server bind its port and
    render the shell immediately — crucial because the Manager UI must open
    even when the DB/DSN is misconfigured, so the user can fix it. Read-only
    pages and ``/health`` never trigger the heavy build.
    """
    logger.info("Web stores: lazy init armed (built on first data request).")


def _ensure_initialized() -> None:
    """Build stores on first access (idempotent, thread-safe)."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        try:
            _initialize()
        except StoreInitError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Leave _initialized False so a later request retries after the
            # user fixes the config; surface an actionable 503 instead of 500.
            err = _classify_init_error(exc)
            logger.error("Store init failed [%s]: %s", err.code, err.message)
            raise err from exc
        _initialized = True


def _initialize() -> None:
    """One-time heavy init — stores + LLM + adapters. Invoked lazily."""
    global _stores, _llm_client, _llm_provider, _llm_model
    global _event_brancher, _ingestion_pipeline
    global _training_store, _control_node_builder
    global _projection_engine
    global _plan_engine, _template_miner

    settings = get_settings()
    _stores = init_stores(settings)
    _llm_client, _llm_provider, _llm_model = get_llm_client(settings)

    # reconstruct_module: TrainingLayerStore / ControlNodeBuilder / PlanEngine
    # replaced by Trainer / ControlNodePhase / PlanBuilder, so separate
    # singletons are unnecessary -- backward-compat deps remain None.
    _training_store = None
    _control_node_builder = None
    _plan_engine = None

    # EventBrancher / ProjectionEngine restored on DbStore basis.
    try:
        from k2g.adapters.event_brancher import EventBrancher

        _event_brancher = EventBrancher(
            graph_store=_stores["graph"],
            content_store=_stores["content"],
            vector_store=_stores["vector"],
            embedding_client=_stores.get("embedding"),
            llm_client=_llm_client,
            llm_provider=_llm_provider,
            llm_model=_llm_model,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("EventBrancher init failed: %s", e)
        _event_brancher = None

    try:
        from k2g.trainer.projection import ProjectionEngine

        _projection_engine = ProjectionEngine(
            graph_store=_stores["graph"],
            vector_store=_stores["vector"],
            content_store=_stores["content"],
            object_storage=_stores["objects"],
            llm_client=_llm_client,
            llm_provider=_llm_provider,
            llm_model=_llm_model,
            embedding_client=_stores.get("embedding"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ProjectionEngine init failed: %s", e)
        _projection_engine = None

    # TemplateMiner (ETG auto-mining). Not shipped in every build — its absence
    # is expected and non-fatal (the mining routes report it unavailable).
    try:
        from k2g.trainer.template_miner import TemplateMiner

        _template_miner = TemplateMiner(
            graph_store=_stores["graph"],
            llm_client=_llm_client,
            llm_provider=_llm_provider,
            llm_model=_llm_model,
        )
    except ModuleNotFoundError:
        logger.info("TemplateMiner not available in this build — skipping.")
        _template_miner = None
    except Exception as e:  # noqa: BLE001
        logger.warning("TemplateMiner init failed: %s", e)
        _template_miner = None

    # IngestionPipeline replaced by Producer/Loader -- remains None until
    # the reingest endpoint is ported.
    _ingestion_pipeline = None

    logger.info(
        "Web stores initialized (event_brancher=%s, projection=%s, "
        "template_miner=%s).",
        _event_brancher is not None,
        _projection_engine is not None,
        _template_miner is not None,
    )


def shutdown() -> None:
    """Clean up stores on server shutdown."""
    global _stores, _initialized
    _initialized = False
    if _stores:
        # If _db (DbStore) is present, a single close cleans up all backends.
        db = _stores.get("_db")
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("DbStore shutdown failed: %s", e)
        else:
            for name, store in _stores.items():
                if name.startswith("_"):
                    continue
                try:
                    if hasattr(store, "close"):
                        store.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("Store shutdown failed [%s]: %s", name, e)
    logger.info("MWeft Manager web stores shutdown complete")


def get_stores_dep() -> dict[str, Any]:
    """Return stores for FastAPI Depends."""
    _ensure_initialized()
    if _stores is None:
        raise RuntimeError("Stores not initialized. Call startup() first.")
    return _stores


def get_db_dep() -> Any:
    """Inject DbStore directly. Provides attribute access to graph/vector/content/object."""
    _ensure_initialized()
    if _stores is None:
        raise RuntimeError("Stores not initialized. Call startup() first.")
    db = _stores.get("_db")
    if db is None:
        raise RuntimeError("DbStore missing from stores -- check init_stores path")
    return db


def get_graph_dep() -> Any:
    """Inject db.graph directly -- the most commonly used path for CG/Plan queries."""
    return get_db_dep().graph


def get_embedding_client_dep() -> Any:
    """Inject embedding client directly -- used in text-to-vector paths like reingest."""
    _ensure_initialized()
    if _stores is None:
        raise RuntimeError("Stores not initialized. Call startup() first.")
    return _stores.get("embedding")


def get_llm_client_dep() -> dict[str, Any]:
    """LLM client bundle -- convenient shape for builder injection.

    Returns: ``{"client": Any, "provider": str, "model": str}``.
    When client is None the LLM is not configured -- callers should
    return 503 or an llm_required error.
    """
    _ensure_initialized()
    return {
        "client": _llm_client,
        "provider": _llm_provider,
        "model": _llm_model,
    }


# reconstruct_module: the deps below always return None until the advanced
# components (ProjectionEngine / EventBrancher / IngestionPipeline /
# TemplateMiner / PlanEngine / TrainingLayerStore / ControlNodeBuilder)
# are re-ported. Each route must detect None and return 503 or
# "not ported".


def get_event_brancher_dep() -> Any:
    _ensure_initialized()
    return _event_brancher


def get_ingestion_pipeline_dep() -> Any:
    return _ingestion_pipeline


def get_training_store_dep() -> Any:
    return _training_store


def get_control_node_builder_dep() -> Any:
    return _control_node_builder


def get_projection_engine_dep() -> Any:
    _ensure_initialized()
    return _projection_engine


def get_plan_engine_dep() -> Any:
    return _plan_engine


def get_template_miner_dep() -> Any:
    _ensure_initialized()
    return _template_miner
