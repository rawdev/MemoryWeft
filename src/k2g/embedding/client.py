"""Embedding client — protocol-based interface with swappable backends.

Backends:
- OpenAI text-embedding-3-small (default)
- sentence-transformers (local, optional dependency — torch)
- onnxruntime (local, optional dependency — no torch, portable deployment)
- dummy (debug / benchmark)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from k2g.core.config import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol (Interface)
# ---------------------------------------------------------------------------

@runtime_checkable
class EmbeddingClient(Protocol):
    """Embedding client interface."""

    @property
    def dim(self) -> int:
        """Return the vector dimension."""
        ...

    def embed(self, text: str) -> list[float]:
        """Embed a single text into a vector."""
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed multiple texts."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------

class OpenAIEmbeddingClient:
    """Embedding client using the OpenAI Embeddings API.

    Default model: text-embedding-3-small (dim=1536).
    """

    def __init__(self, api_key: str, model: str, embedding_dim: int) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "openai package required: pip install openai"
            ) from e

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._dim = embedding_dim
        logger.info("OpenAI embedding client initialized: model=%s, dim=%d", model, embedding_dim)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        """Embed a single text. Empty strings return a zero vector."""
        if not text.strip():
            logger.warning("Empty text requested for embedding; returning zero vector.")
            return [0.0] * self._dim

        response = self._client.embeddings.create(
            model=self._model,
            input=text,
            encoding_format="float",
        )
        vector = response.data[0].embedding

        # Dimension check
        if len(vector) != self._dim:
            logger.warning(
                "Embedding dimension mismatch: expected=%d, actual=%d", self._dim, len(vector)
            )

        return vector

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed up to 2048 texts."""
        if not texts:
            return []

        # Filter empty texts
        non_empty_indices: list[int] = []
        non_empty_texts: list[str] = []
        for i, t in enumerate(texts):
            if t.strip():
                non_empty_indices.append(i)
                non_empty_texts.append(t)

        results: list[list[float]] = [[0.0] * self._dim] * len(texts)

        if non_empty_texts:
            # OpenAI API batch limit: 2048 items
            batch_size = 100
            all_embeddings: list[list[float]] = []
            for i in range(0, len(non_empty_texts), batch_size):
                batch = non_empty_texts[i : i + batch_size]
                response = self._client.embeddings.create(
                    model=self._model,
                    input=batch,
                    encoding_format="float",
                )
                batch_embeddings = [item.embedding for item in response.data]
                all_embeddings.extend(batch_embeddings)

            for idx, embedding in zip(non_empty_indices, all_embeddings):
                results[idx] = embedding

        return results

    def close(self) -> None:
        """No explicit cleanup needed for the OpenAI client."""
        logger.debug("OpenAI embedding client closed")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Local sentence-transformers backend
# ---------------------------------------------------------------------------

class LocalEmbeddingClient:
    """Local embedding client using sentence-transformers.

    Optional dependency: pip install 'mweft[embed-local]'
    """

    def __init__(self, model_name: str, embedding_dim: int) -> None:
        # Lazy: loading the model (e.g. BAAI/bge-m3, ~2GB) takes several seconds
        # and must NOT happen at construction — the Manager UI / MCP server build
        # this client at startup but may never embed. Defer the load to the first
        # embed() call so the server binds its port immediately. The missing
        # optional dep is still surfaced (at first use) with the same message.
        self._model_name = model_name
        self._dim = embedding_dim
        self._model: Any = None
        logger.info("Local embedding client ready (model lazy-load): %s, dim=%d",
                    model_name, embedding_dim)

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                raise ImportError(
                    "sentence-transformers required: pip install 'mweft[embed-local]' "
                    "(or use the onnx backend: EMBEDDING_PROVIDER=onnx)"
                ) from e
            logger.info("Loading local embedding model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
            logger.info("Local embedding model loaded: %s", self._model_name)
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            return [0.0] * self._dim
        vector: np.ndarray = self._ensure_model().encode(
            text, convert_to_numpy=True, show_progress_bar=False,
        )
        return vector.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: np.ndarray = self._ensure_model().encode(
            texts, convert_to_numpy=True, batch_size=32, show_progress_bar=False,
        )
        return vectors.tolist()

    def close(self) -> None:
        logger.debug("Local embedding client closed")
        # sentence-transformers model is left to GC

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Local ONNX backend (no torch — portable deployment)
# ---------------------------------------------------------------------------

class OnnxEmbeddingClient:
    """Compute BGE-M3 dense embeddings via onnxruntime + tokenizers.

    Runs on CPU onnxruntime only, without torch / sentence-transformers —
    suitable for portable zip deployments. Optional dependency:
    ``pip install k2g[embed-onnx]`` (onnxruntime + tokenizers).

    Model path must be a directory containing ``model.onnx`` +
    ``tokenizer.json``. Dense embedding uses [CLS] (first token) pooling
    from last_hidden_state followed by L2 normalization — standard BGE
    dense retrieval practice. If the model outputs already-pooled 2D
    tensors, they are used as-is.
    """

    @staticmethod
    def _register_bundled_dll_dirs(model_path: str) -> None:
        """Make the bundled MSVC runtime visible to onnxruntime's native module.

        onnxruntime's ``*.pyd`` depends on the Visual C++ runtime
        (vcruntime140 / msvcp140), which its wheel does NOT carry — on a clean
        Windows box without the VC++ Redistributable, ``import onnxruntime``
        fails with a DLL-load error. The portable zip ships the runtime under
        ``<bundle>/vc_runtime``; adding that directory to the DLL search path
        *before* the import lets the native module resolve it, so no system
        install is needed. No-op off Windows / outside the portable bundle.
        """
        if not sys.platform.startswith("win"):
            return
        import ctypes
        import os

        candidates: list[Path] = []
        env_dir = os.environ.get("MWEFT_VC_RUNTIME_DIR")
        if env_dir:
            candidates.append(Path(env_dir))
        # Fallback: derive <bundle>/vc_runtime from the bundled model path
        # (<bundle>/models/bge-m3-onnx) when the launcher env var is absent.
        try:
            for up in Path(model_path).resolve().parents:
                candidates.append(up / "vc_runtime")
        except Exception:  # noqa: BLE001
            pass

        # The MSVC runtime DLLs onnxruntime's native module links against. The
        # order matters only in that all should be present together.
        vc_dlls = (
            "vcruntime140.dll", "vcruntime140_1.dll",
            "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        )
        seen: set[str] = set()
        for d in candidates:
            key = str(d).lower()
            if key in seen:
                continue
            seen.add(key)
            if not d.is_dir():
                continue
            try:
                os.add_dll_directory(str(d))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not add DLL dir %s: %s", d, exc)
            # add_dll_directory ALONE is unreliable: when another component
            # (e.g. a conda base env) has already registered an *older*
            # vcruntime/msvcp directory, the loader searches user dirs in
            # registration order and binds the stale copy first -> onnxruntime's
            # native module fails with WinError 1114 ("DLL init routine failed").
            # Force the bundled runtime into the process by absolute path; once a
            # module is loaded, later dependencies bind to it by base name
            # regardless of search order, so the stale copy can no longer win.
            preloaded = 0
            for name in vc_dlls:
                dll = d / name
                if dll.is_file():
                    try:
                        ctypes.WinDLL(str(dll))
                        preloaded += 1
                    except OSError as exc:
                        logger.debug("Could not preload %s: %s", dll, exc)
            logger.debug(
                "Registered bundled VC runtime dir %s (preloaded %d DLL(s))",
                d, preloaded,
            )
            if preloaded:
                return

    def __init__(
        self, model_path: str, embedding_dim: int, max_length: int = 512,
    ) -> None:
        # Register the bundled MSVC runtime before importing onnxruntime so its
        # native module loads on a clean Windows box (portable zip case).
        self._register_bundled_dll_dirs(model_path)
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            detail = str(e)
            # A bundled onnxruntime that fails its *native* import (rather than
            # being absent) is almost always a missing MSVC runtime on Windows —
            # the wheel does not carry vcruntime140/msvcp140; it relies on the
            # system Visual C++ Redistributable. Tell the user that, not a bogus
            # "pip install" (the package is already there in a portable build).
            if "DLL load failed" in detail or "_pybind11_state" in detail:
                raise ImportError(
                    "onnxruntime is installed but its native library failed to "
                    "load. On Windows this means the Microsoft Visual C++ "
                    "Redistributable (x64) is missing — install it from "
                    "https://aka.ms/vs/17/release/vc_redist.x64.exe and restart. "
                    f"(original: {detail})"
                ) from e
            raise ImportError(
                "onnxruntime + tokenizers required: "
                "pip install 'mweft[embed-onnx]' "
                f"(original: {detail})"
            ) from e

        p = Path(model_path)
        if p.is_dir():
            onnx_file = p / "model.onnx"
            tok_file = p / "tokenizer.json"
        else:
            onnx_file = p
            tok_file = p.parent / "tokenizer.json"
        if not onnx_file.is_file():
            raise FileNotFoundError(f"ONNX model not found: {onnx_file}")
        if not tok_file.is_file():
            raise FileNotFoundError(f"tokenizer.json not found: {tok_file}")

        logger.info("Loading ONNX embedding model: %s", onnx_file)
        self._session = ort.InferenceSession(
            str(onnx_file), providers=["CPUExecutionProvider"],
        )
        self._tok = Tokenizer.from_file(str(tok_file))
        self._tok.enable_truncation(max_length=max_length)
        self._tok.enable_padding()  # Pad variable-length batches to longest
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._output_name = self._session.get_outputs()[0].name
        self._dim = embedding_dim
        self._max_length = max_length
        logger.info(
            "ONNX embedding client initialized: dim=%d, inputs=%s",
            embedding_dim, sorted(self._input_names),
        )

    @property
    def dim(self) -> int:
        return self._dim

    def _encode(self, texts: list[str]) -> list[list[float]]:
        encs = self._tok.encode_batch(texts)
        input_ids = np.array([e.ids for e in encs], dtype=np.int64)
        attention_mask = np.array(
            [e.attention_mask for e in encs], dtype=np.int64,
        )
        feeds: dict[str, np.ndarray] = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        out = self._session.run([self._output_name], feeds)[0]
        emb = out[:, 0] if out.ndim == 3 else out  # CLS pooling or already pooled
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        emb = (emb / norms).astype(np.float32)
        if emb.shape[1] != self._dim:
            logger.warning(
                "ONNX embedding dimension mismatch: expected=%d, actual=%d "
                "(check EMBEDDING_DIM setting)", self._dim, emb.shape[1],
            )
        return emb.tolist()

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            return [0.0] * self._dim
        return self._encode([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Empty strings get zero vectors — skip the model
        non_empty_idx: list[int] = []
        non_empty: list[str] = []
        for i, t in enumerate(texts):
            if t.strip():
                non_empty_idx.append(i)
                non_empty.append(t)
        results: list[list[float]] = [[0.0] * self._dim for _ in texts]
        if non_empty:
            for idx, vec in zip(non_empty_idx, self._encode(non_empty)):
                results[idx] = vec
        return results

    def close(self) -> None:
        logger.debug("ONNX embedding client closed")
        self._session = None  # type: ignore[assignment]

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dummy backend (debug / benchmark)
# ---------------------------------------------------------------------------

class DummyEmbeddingClient:
    """Return deterministic L2-normalized vectors from hash seeds, no LLM call.

    Same input produces the same vector (verifiable / reproducible), different
    inputs produce different vectors (avoids cluster collapse). Empty strings
    return a zero vector.

    Purpose: profile time and memory for the produce / load pipeline excluding
    LLM. Eliminates sentence-transformers / OpenAI API loading and latency.
    """

    def __init__(self, embedding_dim: int) -> None:
        self._dim = embedding_dim
        logger.info("Dummy embedding client initialized: dim=%d", embedding_dim)

    @property
    def dim(self) -> int:
        return self._dim

    def _vector_for(self, text: str) -> list[float]:
        if not text.strip():
            return [0.0] * self._dim
        import hashlib
        # blake2b 16B → numpy seed (uint32 4 words)
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
        seed = int.from_bytes(digest[:8], "little", signed=False) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(self._dim).astype(np.float32)
        # L2 normalization (assumes cosine metric)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def embed(self, text: str) -> list[float]:
        return self._vector_for(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vector_for(t) for t in texts]

    def close(self) -> None:
        logger.debug("Dummy embedding client closed")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_embedding_client(settings: Settings) -> EmbeddingClient:
    """Create the appropriate EmbeddingClient backend based on Settings.

    Args:
        settings: Global settings.

    Returns:
        An EmbeddingClient implementation.

    Raises:
        ValueError: If the provider is unsupported.
        ImportError: If required packages are missing.
    """
    provider = settings.embedding_provider

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set."
            )
        return OpenAIEmbeddingClient(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )

    elif provider == "local":
        return LocalEmbeddingClient(
            model_name=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )

    elif provider == "onnx":
        if not settings.embedding_onnx_path:
            raise ValueError(
                "EMBEDDING_PROVIDER=onnx but EMBEDDING_ONNX_PATH is not set."
            )
        return OnnxEmbeddingClient(
            model_path=settings.embedding_onnx_path,
            embedding_dim=settings.embedding_dim,
            max_length=settings.embedding_onnx_max_length,
        )

    elif provider == "dummy":
        return DummyEmbeddingClient(embedding_dim=settings.embedding_dim)

    else:
        raise ValueError(f"Unsupported embedding provider: {provider!r}")
