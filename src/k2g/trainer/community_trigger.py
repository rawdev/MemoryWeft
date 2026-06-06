"""Fire-and-forget community-recompute trigger fired right after ingest.

Worker-less (OSS) model: the client that ingested runs ``recompute_if_stale`` on a
daemon thread **in its own process**.

- **Non-blocking ingest response**: a background daemon thread.
- **Avoids the single-connection hazard**: the background thread does not reuse the
  ingest connection — it creates, uses, and closes a **separate DbStore (dedicated
  connection)**. (Sharing a single Postgres psycopg2 connection across threads risks
  consistency; for SQLite it avoids holding the single connection for a long time.)
- **In-process debounce + in-flight guard**: per (kind, domain), skip the spawn if
  within the minimum interval / already running -> curbs connection churn and recompute
  storms.
- **Non-blocking, non-propagating**: no failure breaks the ingest flow (log only).

Applies to the **interactive ingest** path (long-lived processes such as the MCP
``remember``). Short-lived CLI/batch (producer) is not a target since the daemon thread
dies when the process exits (batch uses an explicit train step instead).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_KINDS = ("event", "entity")

_lock = threading.Lock()
_last_spawn: dict[tuple[str, str], float] = {}   # (kind, domain) -> monotonic
_inflight: set[tuple[str, str]] = set()


def _key(kind: str, domain: str | None) -> tuple[str, str]:
    return (kind, domain or "")


def trigger_recompute_async(
    domain: str | None,
    *,
    kinds: tuple[str, ...] = _KINDS,
    min_interval_sec: float | None = None,
    seed: int | None = None,
    resolution: float | None = None,
) -> list[str]:
    """Called right after ingest — background-spawns only kinds that pass the debounce/in-flight guard.

    Returns: the list of kinds actually spawned (empty if none). All exceptions are absorbed.
    """
    spawned: list[str] = []
    try:
        from k2g.core.config import get_settings

        if min_interval_sec is None:
            try:
                min_interval_sec = float(
                    get_settings().community_recompute_min_interval_sec
                )
            except Exception:  # noqa: BLE001
                min_interval_sec = 30.0
        now = time.monotonic()
        with _lock:
            for kind in kinds:
                k = _key(kind, domain)
                if k in _inflight:
                    continue
                if now - _last_spawn.get(k, 0.0) < min_interval_sec:
                    continue
                _last_spawn[k] = now
                _inflight.add(k)
                spawned.append(kind)
        if not spawned:
            return []
        try:
            threading.Thread(
                target=_run_bg,
                args=(domain, tuple(spawned), seed, resolution),
                name=f"community-recompute-{domain or 'all'}",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001 — release in-flight on spawn failure
            logger.debug("recompute thread spawn failed (ignored): %s", exc)
            with _lock:
                for kind in spawned:
                    _inflight.discard(_key(kind, domain))
            return []
        return spawned
    except Exception as exc:  # noqa: BLE001 — never break ingest
        logger.debug("trigger_recompute_async failed (ignored): %s", exc)
        return spawned


def _run_bg(
    domain: str | None,
    kinds: tuple[str, ...],
    seed: int | None,
    resolution: float | None,
) -> None:
    """Run recompute_if_stale per kind on a dedicated DbStore (separate connection), then close.

    seed/resolution=None → recompute_if_stale reads analysis_param (Manager-saved values)
    via cascade (domain → global → default).
    """
    db: Any = None
    try:
        from k2g.core.config import get_settings
        from k2g.db_store import DbStore
        from k2g.trainer.community_runner import recompute_if_stale

        db = DbStore.from_settings(get_settings())
        for kind in kinds:
            try:
                res = recompute_if_stale(
                    db, kind, domain=domain, seed=seed, resolution=resolution,
                    triggered_by="auto",
                )
                logger.info(
                    "auto recompute kind=%s domain=%s -> %s",
                    kind, domain,
                    {k: res.get(k) for k in ("skipped", "ran", "advanced", "run_id")},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto recompute failed kind=%s domain=%s: %s",
                    kind, domain, exc,
                )
            finally:
                with _lock:
                    _inflight.discard(_key(kind, domain))
    except Exception as exc:  # noqa: BLE001 — setup (connection creation) failure etc.
        logger.warning("_run_bg setup failed domain=%s: %s", domain, exc)
        with _lock:
            for kind in kinds:
                _inflight.discard(_key(kind, domain))
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["trigger_recompute_async"]
