"""BP-77 Step 7 — ``@forwardable_write`` decorator + forward helper.

When stacked on an MCP write tool, the decorator inspects backend mode
+ ``K2G_USE_HUB`` + hub discovery and either:

- forwards the call to the launcher hub's
  ``POST /api/internal/write/{op}`` (SQLite mode + hub available), or
- calls the wrapped function inline (PG mode, ``K2G_USE_HUB=false``, or
  ``auto`` with no hub).

Failure modes:
- ``K2G_USE_HUB=true`` + no hub discovered → ``HubForwardingUnavailable``
  raised at call time (fail-loud — per BP-75 Decisions).
- Hub discovered but HTTP unreachable → same error after retries.
- Hub returns a 4xx/5xx → ``HubForwardingError`` with status + body.
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from k2g.mcp.proxy.mode import (
    discover_hub_info,
    get_hub_url,
    get_hub_use_mode,
    is_sqlite_mode,
    is_subprocess_mode,
)

logger = logging.getLogger(__name__)


class HubForwardingUnavailable(RuntimeError):
    """The hub cannot be reached (network or discovery failure)."""


class HubForwardingError(RuntimeError):
    """The hub returned an error status (4xx/5xx)."""


# Module-level client for connection reuse; tests can monkeypatch.
_forward_client: httpx.Client | None = None
_forward_timeout: float = 60.0


def _get_forward_client() -> httpx.Client:
    global _forward_client
    if _forward_client is None:
        _forward_client = httpx.Client(timeout=_forward_timeout)
    return _forward_client


def _set_forward_client_for_testing(client: httpx.Client | None) -> None:
    """Test helper — swap the module-level httpx.Client (e.g. one wired to
    a TestClient via the hub app)."""
    global _forward_client
    _forward_client = client


def _resolve_forward_target(deps: Any) -> tuple[str, str | None] | None:
    """Decide whether to forward.

    Returns ``(hub_url, token)`` to forward, or ``None`` to call inline.
    Raises ``HubForwardingUnavailable`` when forwarding is *required*
    (``K2G_USE_HUB=true``) but no hub is discoverable.
    """
    # The subprocess that runs the hub-forward target must *not* itself
    # forward — that would loop hub → subprocess → hub → ...
    if is_subprocess_mode():
        return None

    settings = getattr(deps, "settings", None)
    if settings is None or not is_sqlite_mode(settings):
        return None

    use_mode = get_hub_use_mode()
    if use_mode == "false":
        return None

    project_dir_env = os.environ.get("K2G_PROJECT_DIR", "").strip()
    project_dir = Path(project_dir_env) if project_dir_env else None

    hub_url = get_hub_url(project_dir)
    if hub_url is None:
        if use_mode == "true":
            raise HubForwardingUnavailable(
                "K2G_USE_HUB=true but no hub is discoverable. "
                "Start mweft-ui or set K2G_USE_HUB=false / K2G_HUB_URL.",
            )
        return None

    token: str | None = None
    if project_dir is not None:
        info = discover_hub_info(project_dir)
        if info is not None:
            token = info.token
    return hub_url, token


def _forward_to_hub(op_name: str, payload: dict[str, Any], hub_url: str, token: str | None) -> Any:
    headers: dict[str, str] = {}
    if token:
        headers["X-Mweft-Token"] = token
    # BP-78: tell the hub which project this MCP belongs to.  The hub's
    # registry routes the write to the matching subprocess.  Without
    # this header the hub returns 400.
    project_dir = os.environ.get("K2G_PROJECT_DIR", "").strip()
    if project_dir:
        headers["X-Mweft-Project-Dir"] = project_dir

    url = f"{hub_url.rstrip('/')}/api/internal/write/{op_name}"
    client = _get_forward_client()
    try:
        resp = client.post(url, json=payload, headers=headers, timeout=_forward_timeout)
    except (httpx.ConnectError, httpx.ReadTimeout) as exc:
        raise HubForwardingUnavailable(
            f"hub at {hub_url} unreachable for op={op_name!r}: {exc}. "
            f"Start mweft-ui or set K2G_USE_HUB=false.",
        ) from exc
    except httpx.RequestError as exc:
        raise HubForwardingUnavailable(
            f"transport error forwarding op={op_name!r} to {hub_url}: {exc}",
        ) from exc

    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except ValueError:
            detail = resp.text
        raise HubForwardingError(
            f"hub forward of {op_name!r} returned HTTP {resp.status_code}: {detail}",
        )
    try:
        return resp.json()
    except ValueError:
        return resp.text


def forwardable_write(
    op_name: str,
    *,
    deps_fetcher: Callable[[], Any] | None = None,
) -> Callable[..., Any]:
    """Decorator factory — wraps a write tool with hub-forward logic.

    Two calling conventions are supported:

    1. **Tool helpers** (``memory_tools.remember_tool`` etc.) which take
       ``deps`` as their first positional arg.  Use the default
       ``deps_fetcher=None``::

           @forwardable_write("remember")
           def remember_tool(deps, *, content, ...): ...

    2. **MCP server adapters** (``server.py``'s ``mweft_remember`` etc.)
       which do *not* take ``deps`` and instead pull it from a module
       getter like ``_get_deps()``.  Pass that getter as
       ``deps_fetcher``::

           @mcp_app.tool()
           @_telemetry_wrap("mweft_remember")
           @forwardable_write("remember", deps_fetcher=_get_deps)
           def mweft_remember(content, summary, ...):
               return remember_tool(_get_deps(), content=content, ...)

    In both cases the wrapped function is called inline when forwarding
    is disabled (PG mode / ``K2G_USE_HUB=false`` / auto + no hub).
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if deps_fetcher is None:
                if not args:
                    raise TypeError(
                        f"forwardable_write({op_name!r}): expected deps as "
                        "the first positional argument.",
                    )
                deps = args[0]
                rest_args = args[1:]
            else:
                deps = deps_fetcher()
                rest_args = args

            try:
                target = _resolve_forward_target(deps)
            except HubForwardingUnavailable:
                raise

            if target is None:
                if deps_fetcher is None:
                    return fn(deps, *rest_args, **kwargs)
                return fn(*rest_args, **kwargs)

            hub_url, token = target
            if rest_args:
                raise TypeError(
                    f"forwardable_write({op_name!r}): positional args not "
                    "supported (use keyword arguments).",
                )
            return _forward_to_hub(op_name, kwargs, hub_url, token)

        # Expose the underlying function for tests / introspection.
        wrapper._forwardable_op = op_name  # type: ignore[attr-defined]
        wrapper._wrapped_fn = fn  # type: ignore[attr-defined]
        return wrapper

    return decorator


__all__ = [
    "HubForwardingError",
    "HubForwardingUnavailable",
    "forwardable_write",
]
