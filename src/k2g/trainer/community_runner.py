"""Reusable leidenalg community batch runner (entity + event).

Single source of truth for one community training run, shared by the CLI
(``scripts/train_community.py``) and the web recompute route
(``k2g.web.routes.community``). Writes a ``train_run`` row
(kind=leiden_entity / leiden_event) and the matching
``entity_community_assignment`` / ``event_community_assignment`` rows.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from k2g.db_store import DbStore

_KIND = {"entity": "leiden_entity", "event": "leiden_event"}


def run_community(
    db: "DbStore",
    target: str,
    *,
    domain: str | None = None,
    seed: int = 42,
    resolution: float = 1.0,
    theta_e: float = 0.4,
    dry_run: bool = False,
    triggered_by: str = "manual",
) -> dict[str, Any]:
    """Run one leiden community detection for ``target`` (entity|event).

    Records a ``train_run`` row regardless of outcome (completed / failed) and,
    unless ``dry_run``, upserts the community assignments. Returns a JSON-able
    summary; raises on engine failure (after marking the run failed).
    """
    if target not in _KIND:
        raise ValueError(f"target must be 'entity' or 'event', got {target!r}")

    from k2g.graph import leiden_community as lc

    stats = db.graph.get_statistics(domain)
    event_count = int(stats.get("events", 0))
    entity_count = int(stats.get("entities", 0))

    run_id = db.graph.train_run_start(
        kind=_KIND[target],
        domain=domain,
        params={
            "seed": seed,
            "resolution": resolution,
            "theta_e": theta_e,
            "algorithm": "leidenalg.RBConfigurationVertexPartition",
            "target": target,
        },
        triggered_by=triggered_by,
        event_count=event_count,
        entity_count=entity_count,
    )

    try:
        t0 = time.perf_counter()
        result = lc.detect(
            db, target, domain=domain, seed=seed, resolution=resolution,
            theta_e=theta_e,
        )
        dt = time.perf_counter() - t0

        metrics = {
            "modularity": result.modularity,
            "num_communities": result.num_communities,
            "top5_sizes": result.top_sizes,
            "leiden_seconds": round(dt, 3),
            "node_count": result.node_count,
            "edge_count": result.edge_count,
        }

        if dry_run:
            metrics["dry_run"] = True
            db.graph.train_run_complete(run_id, metrics=metrics, rows_written=0)
            return {
                "run_id": run_id, "target": target, "domain": domain,
                "metrics": metrics, "rows_written": 0, "dry_run": True,
            }

        if target == "entity":
            rows = db.graph.upsert_entity_community_assignments(
                run_id, result.assignments,
            )
        else:
            rows = db.graph.upsert_event_community_assignments(
                run_id, result.assignments,
            )
        db.graph.train_run_complete(run_id, metrics=metrics, rows_written=rows)
        return {
            "run_id": run_id, "target": target, "domain": domain,
            "metrics": metrics, "rows_written": rows, "dry_run": False,
        }
    except Exception as exc:  # noqa: BLE001
        db.graph.train_run_fail(run_id, str(exc))
        raise


def recompute_if_stale(
    db: "DbStore",
    target: str,
    *,
    domain: str | None = None,
    seed: int | None = None,
    resolution: float | None = None,
    theta_e: float | None = None,
    triggered_by: str = "auto",
    force: bool = False,
) -> dict[str, Any]:
    """Recompute the community snapshot only when stale, then advance the OCC pointer.

    The entry point an active client can safely call in a worker-less environment.

    1. **gate**: compare the derived version against the ``community_state`` pointer.
       If not stale, skip the computation entirely (no expensive edge read).
       ``force=True`` ignores the gate.
    2. **read V_read**: the snapshot baseline the computation is based on (read
       *before* computing so it under-stamps — regression-safe; never over-stamp).
    3. ``run_community(...)`` — leiden computation + train_run/assignment write.
    4. **CAS**: ``advance_pointer(V_read)`` — if a newer result already exists the
       pointer does not advance (``advanced=False``); this train_run/assignment is
       still kept but the "valid run" pointer does not point at it.

    Returns: ``{ran, skipped, advanced, run_id, base_count, base_max_ts, kind,
    domain, reason?, metrics?}``. A computation failure is raised by run_community —
    the caller (ingest hook, etc.) isolates it with try/except.

    NOTE: wrapping the worker read in a REPEATABLE READ snapshot is a follow-up.
    For now V_read is read just before computing, guaranteeing only under-stamp safety.
    """
    from k2g.trainer import community_freshness as cf

    if target not in _KIND:
        raise ValueError(f"target must be 'entity' or 'event', got {target!r}")
    conn = getattr(db.graph, "_conn", None)
    if conn is None:
        return {"skipped": True, "ran": False, "advanced": False,
                "reason": "graph store does not expose _conn",
                "kind": target, "domain": domain}

    if not force and not cf.is_stale(conn, target, domain):
        return {"skipped": True, "ran": False, "advanced": False,
                "reason": "fresh", "kind": target, "domain": domain}

    # Herd guard. Cross-process best-effort lock so simultaneous ingest clients
    # don't each run the (expensive) leiden compute for the same kind+domain.
    # try-lock fail = another process is already recomputing -> skip (it will
    # advance the pointer). PG-only; SQLite path is an always-acquire no-op.
    # Correctness still rests on advance_pointer's OCC CAS regardless.
    with cf.recompute_lock(conn, target, domain) as got_lock:
        if not got_lock:
            return {"skipped": True, "ran": False, "advanced": False,
                    "reason": "herd-skip", "kind": target, "domain": domain}
        # double-checked gate: a holder may have finished + advanced the pointer
        # between the outer is_stale and our acquiring the lock.
        if not force and not cf.is_stale(conn, target, domain):
            return {"skipped": True, "ran": False, "advanced": False,
                    "reason": "fresh-after-lock", "kind": target, "domain": domain}

        # When seed/resolution are unset, use the values the Manager saved in
        # analysis_param (domain -> global -> default cascade). Explicit values are
        # used as-is. (Read only once the gate passes = only when actually computing.)
        params_scope = "explicit"
        if seed is None or resolution is None or theta_e is None:
            lp = cf.read_leiden_params(conn, domain)
            params_scope = lp.get("scope", "default")
            if seed is None:
                seed = int(lp["seed"])
            if resolution is None:
                resolution = float(lp["resolution"])
            if theta_e is None:
                theta_e = float(lp.get("theta_e", 0.4))

        base_count, base_max_ts = cf.derive_version(conn, domain)
        result = run_community(
            db, target, domain=domain, seed=seed, resolution=resolution,
            theta_e=theta_e, triggered_by=triggered_by,
        )
        run_id = result.get("run_id")
        advanced = False
        if run_id:
            advanced = cf.advance_pointer(
                conn, target, domain, run_id=run_id,
                base_count=base_count, base_max_ts=base_max_ts,
            )
        return {
            "skipped": False, "ran": True, "advanced": advanced,
            "run_id": run_id, "base_count": base_count, "base_max_ts": base_max_ts,
            "kind": target, "domain": domain, "metrics": result.get("metrics"),
            "seed": seed, "resolution": resolution, "theta_e": theta_e,
            "params_scope": params_scope,
        }
