"""k2g.trainer — Graph post-processing phase plugins.

Public API:
    TrainingPhase (Protocol), PhaseResult, Trainer (façade)
    registry: register_phase / registered_phases / get_phase / all_phase_names
    Built-in phases: JaccardPhase / HdbscanPhase / ControlNodePhase / EntityVectorPhase

Lazy re-export (PEP 562). Previously this ``__init__`` eager-imported all phases
and called ``_register_builtins()`` at import time — importing just
``trainer.jaccard`` pulled the entire package (hdbscan etc.) into the import
closure. Now re-exports go through ``__getattr__`` (lazy), and built-in phase
registration is deferred to the first ``registry`` access
(``registry._ensure_builtins``). Existing calls like
``from k2g.trainer import JaccardPhase`` still work.
"""

from __future__ import annotations

import importlib
from typing import Any

# Public name → defining module. Each module is imported only on attribute access.
_LAZY: dict[str, str] = {
    "TrainingPhase": "k2g.trainer.base",
    "PhaseResult": "k2g.trainer.base",
    "Trainer": "k2g.trainer.facade",
    "JaccardPhase": "k2g.trainer.jaccard",
    "HdbscanPhase": "k2g.trainer.hdbscan",
    "ControlNodePhase": "k2g.trainer.control_node",
    "EntityVectorPhase": "k2g.trainer.entity_vector",
    "register_phase": "k2g.trainer.registry",
    "registered_phases": "k2g.trainer.registry",
    "get_phase": "k2g.trainer.registry",
    "all_phase_names": "k2g.trainer.registry",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:  # PEP 562 — lazy re-export
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return sorted(__all__)
