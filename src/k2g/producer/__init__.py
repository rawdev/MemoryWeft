"""k2g.producer — source → staging transformation virtualization.

Consolidates DoxygenInputRunner / NovelInputRunner / VcsInputRunner /
ingest_doc_batch into a single Producer Protocol.

Public API:
    Producer (Protocol)
    Scope / ProduceResult / LoadResult
    DocBatchProducer + DocBatchUnit
    NovelProducer + NovelFileUnit (= NovelUnit alias)
        — Context Divider integration + incremental build
    VcsProducer + VcsUnit
        — VCS commit ingestion + manifest-based incremental
    DoxygenProducer + DoxygenFileUnit (= DoxygenUnit alias)
        — XML compound parsing + C2NT Path A/B + incremental build
"""

from k2g.producer.base import LoadResult, Producer, ProduceResult, Scope

# Concrete producers are imported lazily (PEP 562 ``__getattr__``).  Eagerly
# importing them here pulls the full ingestion engine — extractor / agent /
# ner / planner / context_divider — into *any* consumer of this package,
# including the thin ``web.reingest`` helper that only needs ``base`` +
# ``_shared``.  Deferring keeps that import surface minimal while preserving
# the ``from k2g.producer import NovelProducer`` call convention.
_LAZY: dict[str, str] = {
    "DocBatchProducer": "doc_batch",
    "DocBatchUnit": "doc_batch",
    "DoxygenFileUnit": "doxygen",
    "DoxygenProducer": "doxygen",
    "DoxygenUnit": "doxygen",
    "NovelFileUnit": "novel",
    "NovelProducer": "novel",
    "NovelUnit": "novel",
    "VcsProducer": "vcs",
    "VcsUnit": "vcs",
}


def __getattr__(name: str) -> object:
    submod = _LAZY.get(name)
    if submod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod = importlib.import_module(f"{__name__}.{submod}")
    return getattr(mod, name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "Producer",
    "Scope",
    "ProduceResult",
    "LoadResult",
    "DocBatchProducer",
    "DocBatchUnit",
    "NovelProducer",
    "NovelFileUnit",
    "NovelUnit",
    "VcsProducer",
    "VcsUnit",
    "DoxygenProducer",
    "DoxygenFileUnit",
    "DoxygenUnit",
]
