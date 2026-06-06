"""BP-75 — ``ProxyEmbeddingClient``.

An :class:`EmbeddingClient` implementation that forwards calls to the
launcher hub's ``POST /api/internal/embed``.  Used by MCP processes (and
the project subprocess in SQLite mode) so a single BGE-M3 instance lives
in the hub process and is shared by every MCP client.

Fallback policy (BP-75 §10):

- Default: **fail-loud**.  If the hub is unreachable, raise
  ``ProxyEmbeddingUnavailable`` — the MCP client tells the user to start
  ``mweft-ui``.
- Opt-in (``K2G_EMBED_FALLBACK_LOCAL=true``): on hub failure, fall back to
  an in-process :class:`LocalEmbeddingClient`.  Defeats the memory-saving
  goal of BP-75 — use only for CI / dev escape hatch.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FALLBACK_ENV = "K2G_EMBED_FALLBACK_LOCAL"


class ProxyEmbeddingUnavailable(RuntimeError):
    """Raised when the hub cannot be reached and no fallback is permitted."""


class ProxyEmbeddingClient:
    """``EmbeddingClient`` Protocol-compatible HTTP forwarder."""

    def __init__(
        self,
        hub_url: str,
        *,
        dim: int,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 0.5,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._hub_url = hub_url.rstrip("/")
        self._dim = dim
        self._timeout = timeout
        self._retries = max(0, retries)
        self._backoff = backoff
        self._token = token
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._fallback: Any | None = None  # lazy-loaded LocalEmbeddingClient

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        payload = {"text": text}
        vectors = self._post_embed(payload)
        if not vectors:
            return [0.0] * self._dim
        return vectors[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"texts": list(texts)}
        return self._post_embed(payload)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                logger.exception("ProxyEmbeddingClient: httpx close failed")
        if self._fallback is not None:
            try:
                self._fallback.close()
            except Exception:  # noqa: BLE001
                pass

    # --- internal -------------------------------------------------------

    def _post_embed(self, payload: dict[str, Any]) -> list[list[float]]:
        url = f"{self._hub_url}/api/internal/embed"
        headers: dict[str, str] = {}
        if self._token:
            headers["X-Mweft-Token"] = self._token

        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"hub returned {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                body = resp.json()
                dim = int(body.get("dim", self._dim))
                if dim != self._dim:
                    raise ProxyEmbeddingUnavailable(
                        f"hub dim={dim} mismatch with expected {self._dim}",
                    )
                return [list(map(float, v)) for v in body.get("vectors", [])]
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._retries:
                    delay = self._backoff * (2 ** attempt)
                    logger.warning(
                        "ProxyEmbeddingClient: %s (attempt %d/%d); retry in %.2fs",
                        exc, attempt + 1, self._retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
            except httpx.RequestError as exc:
                last_exc = exc
                break

        return self._handle_failure(payload, last_exc)

    def _handle_failure(
        self,
        payload: dict[str, Any],
        exc: Exception | None,
    ) -> list[list[float]]:
        if _is_fallback_enabled():
            logger.warning(
                "ProxyEmbeddingClient: hub unreachable, falling back to "
                "LocalEmbeddingClient (K2G_EMBED_FALLBACK_LOCAL=true). "
                "This defeats BP-75 memory savings — opt-out for production.",
            )
            local = self._get_fallback()
            if "texts" in payload:
                return local.embed_batch(payload["texts"])
            return [local.embed(payload.get("text", ""))]

        raise ProxyEmbeddingUnavailable(
            f"hub embedding endpoint unreachable at {self._hub_url}: {exc}. "
            "Start mweft-ui or set K2G_EMBED_FALLBACK_LOCAL=true for an "
            "in-process fallback.",
        ) from exc

    def _get_fallback(self) -> Any:
        if self._fallback is None:
            from k2g.core.config import get_settings
            from k2g.embedding.client import create_embedding_client

            self._fallback = create_embedding_client(get_settings())
        return self._fallback


def _is_fallback_enabled() -> bool:
    return os.environ.get(_FALLBACK_ENV, "").strip().lower() in ("1", "true", "yes")


__all__ = [
    "ProxyEmbeddingClient",
    "ProxyEmbeddingUnavailable",
]
