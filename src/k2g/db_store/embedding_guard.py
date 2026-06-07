"""Embedding fingerprint guard.

A vector store silently corrupts search if it is opened with a different
embedding *dimension* or *model* than the one its stored vectors were built
with. To prevent that, the graph store stamps the embedding fingerprint
(dim + model) into a tiny ``k2g_meta`` table on first creation and hard-blocks
any later open with an incompatible config.

Provider is intentionally NOT part of the fingerprint: the same model under the
``local`` (torch) and ``onnx`` providers shares the embedding space, so swapping
providers is allowed. Only dimension and model are hard constraints.
"""

from __future__ import annotations

# k2g_meta keys holding the embedding fingerprint.
DIM_KEY = "embedding_dim"
MODEL_KEY = "embedding_model"


class EmbeddingFingerprintMismatch(RuntimeError):
    """Raised when a DB is opened with an embedding config that would corrupt it."""


def fingerprint_problems(
    stored_dim: str | None,
    stored_model: str | None,
    cur_dim: str,
    cur_model: str,
) -> list[str]:
    """Return human-readable mismatch reasons; an empty list means compatible.

    A missing stored value (legacy/never-stamped DB) is treated as compatible so
    callers can back-fill it instead of blocking.
    """
    problems: list[str] = []
    if stored_dim is not None and str(stored_dim) != str(cur_dim):
        problems.append(f"dim {stored_dim} != {cur_dim}")
    if stored_model and cur_model and stored_model != cur_model:
        problems.append(f"model {stored_model!r} != {cur_model!r}")
    return problems


def mismatch_message(
    stored_dim: str | None,
    stored_model: str | None,
    cur_dim: str,
    cur_model: str,
    problems: list[str],
) -> str:
    return (
        "Embedding config mismatch: this database was built with "
        f"[dim={stored_dim}, model={stored_model}] but is being opened with "
        f"[dim={cur_dim}, model={cur_model}] ({'; '.join(problems)}). "
        "Refusing to open to avoid corrupting vector search. Use the original "
        "embedding model/dimension, or point this project at a fresh data directory."
    )
