"""Thread-local logger context.

``JsonLinesFormatter`` (``logging_config.py``) already extracts extra
``LogRecord`` attributes (``build_id`` / ``session_id`` / ``domain`` /
``owner_id``) as top-level JSON fields.  This module provides a
``logging.Filter`` that auto-attaches those fields from the *active
context* of the current thread / asyncio task, so callers do not need
to pass ``extra={...}`` on every logger call.

Usage -- set once at the top-level entry point::

    from k2g.observability.log_context import build_log_context

    with build_log_context(
        build_id=build_id,
        session_id=session_id,
        domain=args.domain,
        owner_id=DEV_OWNER_ID,
    ):
        logger.info("build_start")          # auto-attached, no extra
        logger.warning("tier_fallback",      # explicit extra wins
                       extra={"persona": "ner", ...})
"""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Iterator

_KEYS: tuple[str, ...] = ("build_id", "session_id", "domain", "owner_id")

_LOG_CONTEXT: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("k2g_log_context", default=None)
)


@contextmanager
def build_log_context(
    *,
    build_id: str | None = None,
    session_id: str | None = None,
    domain: str | None = None,
    owner_id: str | None = None,
) -> Iterator[None]:
    """Activate thread/task-local logger context for the ``with`` block.

    Nested calls merge onto the parent context in a stack -- keys
    specified by the child override; unspecified keys are inherited
    from the parent.  Based on ``contextvars.ContextVar``, so it is
    isolated across threads and asyncio tasks.
    """
    parent = _LOG_CONTEXT.get() or {}
    merged: dict[str, str] = dict(parent)
    if build_id is not None:
        merged["build_id"] = build_id
    if session_id is not None:
        merged["session_id"] = session_id
    if domain is not None:
        merged["domain"] = domain
    if owner_id is not None:
        merged["owner_id"] = owner_id
    token = _LOG_CONTEXT.set(merged)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def current_log_context() -> dict[str, str]:
    """Return a copy of the current active context dict (for testing)."""
    ctx = _LOG_CONTEXT.get()
    return dict(ctx) if ctx else {}


def set_log_context(
    *,
    build_id: str | None = None,
    session_id: str | None = None,
    domain: str | None = None,
    owner_id: str | None = None,
) -> None:
    """Set context at process scope without ``with`` (one-shot CLI entry).

    When the process exits, contextvars are GC'd so no reset is
    needed.  Intended for long ``main()`` functions where deeply
    indented ``with`` blocks
    are inconvenient.  Merges with parent context -- only explicitly
    specified keys are overridden.
    """
    parent = _LOG_CONTEXT.get() or {}
    merged: dict[str, str] = dict(parent)
    if build_id is not None:
        merged["build_id"] = build_id
    if session_id is not None:
        merged["session_id"] = session_id
    if domain is not None:
        merged["domain"] = domain
    if owner_id is not None:
        merged["owner_id"] = owner_id
    _LOG_CONTEXT.set(merged)


class LogContextFilter(logging.Filter):
    """Inject thread-local log context into ``LogRecord`` attributes.

    If the ``record`` already has an attribute of the same name (i.e.
    the logger call explicitly passed ``extra={"build_id": ...}``),
    that value takes precedence -- the filter only fills in missing
    keys.  Always returns ``True`` (never blocks records).

    Must be attached to handlers so it also applies to records that
    arrive via propagation (``configure_logging`` auto-attaches to
    all handlers).
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        ctx = _LOG_CONTEXT.get()
        if not ctx:
            return True
        for key in _KEYS:
            if not hasattr(record, key):
                value = ctx.get(key)
                if value is not None:
                    setattr(record, key, value)
        return True


__all__ = [
    "LogContextFilter",
    "build_log_context",
    "current_log_context",
    "set_log_context",
]
