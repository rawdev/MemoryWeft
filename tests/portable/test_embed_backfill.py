"""Embedding backfill — the repair path for an archive imported without vectors.

The failure this guards against is silent: search filters
``embedding IS NOT NULL`` before anything else, so rows that arrive with a NULL
embedding are invisible to every query and the user just sees "no results".
These tests assert the *observable* consequence — search goes from 0 hits to
hits — not merely that a column got written.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from k2g.portable.embed_backfill import backfill_embeddings, count_missing

DIM = 64


class _StubEmbedding:
    """Deterministic, direction-bearing fake embeddings.

    Real BGE-M3 is not available in CI and is beside the point here — what
    matters is that the backfill wires text → vector → store correctly and that
    similar text lands nearer than dissimilar text. Word-hash bag-of-words gives
    that with no model.
    """

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for text in texts:
            v = [0.0] * DIM
            for word in str(text).lower().split():
                v[hash(word) % DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


@pytest.fixture()
def stores(tmp_path: Path):
    from k2g.db_store.sqlite.graph import SqliteGraphStore
    from k2g.db_store.sqlite.vector import SqliteVectorStore

    graph = SqliteGraphStore(str(tmp_path / "g.db"), embedding_dim=DIM)
    vector = SqliteVectorStore(str(tmp_path / "g.db"), dim=DIM)
    yield graph, vector
    for s in (graph, vector):
        try:
            s._conn.close()
        except Exception:
            pass


_SEED = [
    ("mem_1", "postgres vector index tuning"),
    ("mem_2", "sqlite embedding storage layout"),
    ("mem_3", "kimchi stew recipe with tofu"),
]


def _seed_events(graph, domain: str = "K2G") -> None:
    """Events with a summary but no embedding — exactly the imported shape."""
    for i, (vid, summary) in enumerate(_SEED):
        graph.create_event(vid, domain, None, i, summary=summary)
    graph._conn.commit()


def _search(vector, embedding, query: str, domain: str = "K2G") -> list:
    return vector.search(
        query_vector=embedding.embed(query), filter_domain=domain, limit=10,
    )


# --- the silent failure -----------------------------------------------------

def test_search_is_dark_before_backfill(stores) -> None:
    """The whole reason this module exists: no error, just nothing."""
    graph, vector = stores
    emb = _StubEmbedding()
    _seed_events(graph)
    assert _search(vector, emb, "postgres vector") == []


def test_count_missing_reports_the_gap(stores) -> None:
    graph, vector = stores
    _seed_events(graph)
    assert count_missing(graph, "K2G")["events"] == len(_SEED)


# --- repair -----------------------------------------------------------------

def test_backfill_makes_events_searchable(stores) -> None:
    graph, vector = stores
    emb = _StubEmbedding()
    _seed_events(graph)

    res = backfill_embeddings(graph, vector, emb, domain="K2G")
    assert res["events"] == len(_SEED)
    assert res["failed"] == 0

    hits = _search(vector, emb, "postgres vector index tuning")
    assert hits, "search still returns nothing after backfill"
    assert hits[0]["summary"] == "postgres vector index tuning"
    assert count_missing(graph, "K2G")["events"] == 0


def test_backfill_is_idempotent(stores) -> None:
    """Re-running must not re-embed rows that already have a vector — the
    Manager offers this after every import, so a second run has to be cheap."""
    graph, vector = stores
    emb = _StubEmbedding()
    _seed_events(graph)
    backfill_embeddings(graph, vector, emb, domain="K2G")
    calls_after_first = emb.calls

    again = backfill_embeddings(graph, vector, emb, domain="K2G")
    assert again["events"] == 0
    assert emb.calls == calls_after_first, "re-embedded already-vectored rows"


def test_backfill_is_domain_scoped(stores) -> None:
    graph, vector = stores
    emb = _StubEmbedding()
    _seed_events(graph, "K2G")
    graph.create_event("mem_other", "other", None, 0, summary="unrelated note")
    graph._conn.commit()

    backfill_embeddings(graph, vector, emb, domain="K2G")
    assert count_missing(graph, "other")["events"] == 1


def test_rows_without_summary_are_skipped(stores) -> None:
    """Nothing to embed from — must not count as failed, and must not crash."""
    graph, vector = stores
    emb = _StubEmbedding()
    graph.create_event("mem_empty", "K2G", None, 0, summary="")
    graph._conn.commit()

    res = backfill_embeddings(graph, vector, emb, domain="K2G")
    assert res == {"domain": "K2G", "events": 0, "entities": 0, "failed": 0}


# --- resilience -------------------------------------------------------------

def test_failing_batch_does_not_abort_the_run(stores) -> None:
    """One bad batch must not strand the whole import."""
    graph, vector = stores
    _seed_events(graph)

    class _FlakyEmbedding(_StubEmbedding):
        def embed_batch(self, texts):
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("model unavailable")
            return super().embed_batch(texts)

    emb = _FlakyEmbedding()
    res = backfill_embeddings(graph, vector, emb, domain="K2G", batch=1)
    assert res["failed"] == 1
    assert res["events"] == len(_SEED) - 1


def test_progress_callback_reports_totals(stores) -> None:
    graph, vector = stores
    emb = _StubEmbedding()
    _seed_events(graph)
    seen: list[tuple[str, int, int]] = []
    backfill_embeddings(
        graph, vector, emb, domain="K2G", batch=2,
        progress=lambda phase, done, total: seen.append((phase, done, total)),
    )
    assert seen[0] == ("events", 0, len(_SEED))
    assert seen[-1][1] == seen[-1][2] == len(_SEED)


def test_entity_phase_skipped_without_projection(stores) -> None:
    """No ProjectionEngine ⇒ events still get vectors (event search works)."""
    graph, vector = stores
    emb = _StubEmbedding()
    _seed_events(graph)
    res = backfill_embeddings(graph, vector, emb, domain="K2G", projection=None)
    assert res["events"] == len(_SEED)
    assert res["entities"] == 0
