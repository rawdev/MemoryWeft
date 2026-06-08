"""Portable embedding (de)serialization for export/import.

The entity/event embedding lives in a backend-specific column — a
``sqlite-vec`` ``serialize_float32`` BLOB under SQLite, a ``pgvector``
``vector(N)`` under Postgres. The portable archive stores it backend-neutrally
as a JSON array of floats; this module converts in both directions.

- :func:`decode_embedding` — raw column value (BLOB / pgvector text / list) →
  ``list[float]`` for the archive (export read side).
- :func:`encode_embedding_sqlite` — ``list[float]`` → ``sqlite-vec`` BLOB.
- :func:`pg_vector_literal` — ``list[float]`` → ``"[v1,v2,…]"`` text, fed to a
  ``::vector`` cast on import (matches PgVectorStore.upsert's write form).
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np


def decode_embedding(value: Any) -> list[float] | None:  # noqa: ANN401
    """Normalize a raw embedding column value to a plain ``list[float]``.

    Handles every shape the two backends return:
    - ``list`` / ``tuple`` / ``np.ndarray`` (pgvector with register_vector) →
      coerced element-wise.
    - ``bytes`` (sqlite-vec serialize_float32 = raw little-endian float32) →
      decoded via numpy.
    - ``str`` (pgvector text ``"[0.1,0.2,…]"`` — JSON-compatible) → parsed.
    Returns ``None`` for NULL / empty so the row simply carries no vector.
    """
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        return value.astype(float).tolist()
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        buf = bytes(value)
        if not buf:
            return None
        return np.frombuffer(buf, dtype="<f4").astype(float).tolist()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return [float(x) for x in json.loads(s)]
        except (ValueError, TypeError):
            inner = s.strip("[]")
            return [float(x) for x in inner.split(",") if x.strip()]
    return None


def encode_embedding_sqlite(vec: list[float] | None) -> bytes | None:
    """``list[float]`` → sqlite-vec BLOB (None passes through as NULL)."""
    if not vec:
        return None
    floats = [float(x) for x in vec]
    try:
        import sqlite_vec  # type: ignore[import-not-found]

        return sqlite_vec.serialize_float32(floats)
    except Exception:  # noqa: BLE001 — sqlite_vec absent/old: raw float32 bytes
        return np.asarray(floats, dtype="<f4").tobytes()


def pg_vector_literal(vec: list[float] | None) -> str | None:
    """``list[float]`` → pgvector text literal ``"[v1,v2,…]"`` (None → NULL).

    Fed through an explicit ``::vector`` cast on the import UPDATE, so no
    pgvector adapter needs to be registered on the importer's connection.
    """
    if not vec:
        return None
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
