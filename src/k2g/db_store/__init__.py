"""k2g.db_store — unified abstraction for graph + vector + content + object backends.

Public API:
    DbStore                — facade (graph/vector/content/object composition)
    DbStore.from_settings  — Settings-based assembly
    GraphStoreProtocol     — graph backend contract
    VectorStoreProtocol    — vector backend contract
    ContentBackend         — content backend contract
    ObjectBackend          — object backend contract

Concrete implementations live in sub-modules:
    k2g.db_store.postgres.{graph,vector,content}
    k2g.db_store.sqlite.{graph,vector,content}
    k2g.db_store.object.local
"""

from k2g.db_store.base import DbStore
from k2g.db_store.content_backend import ContentBackend
from k2g.db_store.graph_backend import GraphStoreProtocol
from k2g.db_store.object_backend import ObjectBackend
from k2g.db_store.vector_backend import VectorStoreProtocol

__all__ = [
    "DbStore",
    "GraphStoreProtocol",
    "VectorStoreProtocol",
    "ContentBackend",
    "ObjectBackend",
]
