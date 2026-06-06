"""Warmup logic for the per-project subprocess.

Loads embedding deps (downloads the configured model from HuggingFace on
first run) and emits a probe embedding to verify dimensionality. Reuses
``k2g.mcp.factory.build_dependencies`` but skips the ``.mwf`` file check —
the UI runs with env-driven config (``K2G_DOTENV_FILE=""``).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarmupResult:
    embed_dim: int
    embed_elapsed: float
    deps_elapsed: float
    total_elapsed: float


def warmup(*, allow_hf_download: bool = True) -> WarmupResult:
    if allow_hf_download:
        for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            if os.environ.pop(var, None):
                logger.info("offline flag cleared: %s", var)

    from k2g.core.config import get_settings
    from k2g.mcp.factory import build_dependencies

    t0 = time.time()
    settings = get_settings()
    logger.info(
        "warmup start | embed=%s dim=%s",
        settings.embedding_model, settings.embedding_dim,
    )

    deps = build_dependencies()
    deps_elapsed = time.time() - t0

    t1 = time.time()
    vec = deps.embedding.embed("warmup probe")
    embed_elapsed = time.time() - t1
    if len(vec) != settings.embedding_dim:
        raise RuntimeError(
            f"embedding dim mismatch: expected={settings.embedding_dim} "
            f"actual={len(vec)}",
        )

    try:
        deps.embedding.close()
    except Exception:
        logger.debug("embedding close failed", exc_info=True)

    return WarmupResult(
        embed_dim=len(vec),
        embed_elapsed=embed_elapsed,
        deps_elapsed=deps_elapsed,
        total_elapsed=time.time() - t0,
    )
