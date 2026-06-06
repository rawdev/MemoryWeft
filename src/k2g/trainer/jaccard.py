"""JaccardPhase — thin wrapper around ``compute_jaccard_connected``
already implemented in the db_store graph backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from k2g.trainer.base import PhaseResult

if TYPE_CHECKING:
    from k2g.db_store import DbStore


class JaccardPhase:
    name: str = "jaccard"
    est_time_min: int = 2

    def enabled(self, train_config: dict) -> bool:
        return bool(train_config.get("jaccard", True))

    # 1:1 with backend ``compute_jaccard_connected`` signature. All defaults
    # are defined in backend / DomainConfig.get_jaccard_config — this class
    # only forwards sentinel values.
    _PASSTHROUGH_KEYS: tuple[str, ...] = (
        "scope",
        "theta_e",
        "theta_g",
        "min_group_intersection",
        "stop_groups",
        "max_entity_degree",
        "max_group_degree",
        "work_mem",
        "exclude_stopwords",
    )

    def run(
        self, db: "DbStore", *, domain: str, **kwargs: Any,
    ) -> PhaseResult:
        # Backend signature compat — work_mem is Postgres-only (no SQLite support).
        # Inspect signature and pass only keys the backend actually accepts.
        import inspect
        sig = inspect.signature(db.graph.compute_jaccard_connected)
        passthrough: dict[str, Any] = {}
        for key in self._PASSTHROUGH_KEYS:
            if key in kwargs and key in sig.parameters:
                passthrough[key] = kwargs[key]

        n = db.graph.compute_jaccard_connected(domain, **passthrough)
        return PhaseResult(
            name=self.name, success=True, counts={"edges": int(n)},
        )

    def incremental(
        self, db: "DbStore", *, domain: str, event_id: str,
    ) -> PhaseResult:
        """Single-event ingestion-time incremental jaccard.

        Separate entry point from batch ``run()`` — called by ingestion paths
        (e.g. ``mweft_remember``) that bypass the trainer. Batch ``run()`` is
        unchanged.
        """
        try:
            n = db.graph.compute_jaccard_for_event(domain, event_id)
        except Exception as exc:  # noqa: BLE001
            return PhaseResult(
                name=self.name, success=False,
                error=f"compute_jaccard_for_event failed: {exc}",
            )
        return PhaseResult(
            name=self.name, success=True, counts={"edges": int(n)},
        )


__all__ = ["JaccardPhase"]
