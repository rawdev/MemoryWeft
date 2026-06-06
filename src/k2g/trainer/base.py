"""TrainingPhase Protocol + PhaseResult — trainer phase *contract*.

After the Producer finishes loading events/entities/groups into the graph,
the trainer sequentially runs phase plugins to create post-processing edges /
context_groups etc. This module defines only the phase *contract* —
the orchestration façade (``Trainer``) lives in ``k2g.trainer.facade``.

``Trainer`` was split into ``facade.py`` so that ``base.py`` has no global
registry dependency, keeping ``trainer.jaccard → base`` as a clean leaf edge
(excludes build-only phases from the OSS import closure).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from k2g.db_store import DbStore


@dataclass
class PhaseResult:
    name: str
    success: bool
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class TrainingPhase(Protocol):
    """Contract for a single phase executed by the Trainer."""

    name: str
    est_time_min: int

    def enabled(self, train_config: dict) -> bool: ...

    def run(
        self, db: "DbStore", *, domain: str, **kwargs: Any
    ) -> PhaseResult: ...


__all__ = ["TrainingPhase", "PhaseResult"]
