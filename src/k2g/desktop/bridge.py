"""In-process ASGI bridge — the desktop app's transport, **no HTTP socket**.

The browser Manager talks to its backend over loopback HTTP. The desktop app
removes the socket entirely: the SPA document loads from disk via ``file://`` and
every ``fetch()`` is intercepted (``static/desktop-bridge.js``) and routed through
pywebview's ``js_api`` into this bridge, which runs the **same FastAPI app**
in-process with ``httpx.ASGITransport``.

Design:

* A daemon thread owns a private asyncio loop. On it we open one reusable
  ``httpx.AsyncClient`` over ``ASGITransport`` and hold the app's real
  **lifespan** open (``app.router.lifespan_context`` → ``deps.startup()`` /
  ``deps.shutdown()``) for the window's lifetime.
* ``request()`` is the ``js_api`` method pywebview calls (on a worker thread). It
  submits the coroutine to the loop with ``run_coroutine_threadsafe`` and blocks
  on the result — bodies base64 both ways so any content is binary-safe.

No ``listen()`` anywhere: not uvicorn, not pywebview's bundled bottle server
(the launcher passes ``http_server=False`` + a ``file://`` URL).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Response headers that must not survive the bridge: httpx has already decoded
# the body (``r.content``), so a stale framing/encoding header would make the
# browser-side ``Response`` misread it. Content-Type is kept (``r.json()`` etc.).
_DROP_HEADERS = frozenset({
    "content-encoding", "content-length", "transfer-encoding", "connection",
})

# An internal, fixed base — ASGITransport ignores the host, but httpx needs an
# absolute base_url to resolve the request paths the frontend sends ("/api/…").
_BASE_URL = "http://mweft.local"


class AsgiBridge:
    """Run a FastAPI app in-process and expose ``request`` as a pywebview js_api."""

    def __init__(self, app: "FastAPI") -> None:
        self._app = app
        self._loop = asyncio.new_event_loop()
        self._client: httpx.AsyncClient | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------
    def start(self, timeout: float = 30.0) -> None:
        """Boot the loop thread and block until lifespan startup completes."""
        self._thread = threading.Thread(
            target=self._run, name="mweft-asgi", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError("ASGI bridge did not start within timeout")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url=_BASE_URL, timeout=None,
        ) as client:
            # Run the app's declared lifespan (startup → deps.startup(),
            # shutdown → deps.shutdown()). hub_info=None so hub/sweep paths no-op.
            async with self._app.router.lifespan_context(self._app):
                self._client = client
                self._ready.set()
                await self._stop.wait()
            self._client = None

    def stop(self, timeout: float = 10.0) -> None:
        """Signal lifespan shutdown and join the loop thread."""
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # --- js_api ----------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body_b64: str | None = None,
    ) -> dict[str, Any]:
        """JS ``fetch`` → in-process ASGI → JS. Called by pywebview (worker thread).

        ``body_b64`` is the request body base64-encoded (or ``None``). Returns
        ``{status, headers: [[k, v], …], body_b64}``.
        """
        if self._client is None:
            return {"status": 503, "headers": [["content-type", "text/plain"]],
                    "body_b64": base64.b64encode(b"bridge not ready").decode("ascii")}
        content = base64.b64decode(body_b64) if body_b64 else None
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._client.request(
                    method, path, headers=headers or {}, content=content,
                ),
                self._loop,
            )
            resp = fut.result(timeout=120)
        except Exception as exc:  # noqa: BLE001 — surface to the UI, don't crash
            logger.exception("ASGI bridge request failed: %s %s", method, path)
            msg = f"bridge error: {exc}".encode()
            return {"status": 500, "headers": [["content-type", "text/plain"]],
                    "body_b64": base64.b64encode(msg).decode("ascii")}
        out_headers = [
            [k, v] for k, v in resp.headers.items()
            if k.lower() not in _DROP_HEADERS
        ]
        return {
            "status": resp.status_code,
            "headers": out_headers,
            "body_b64": base64.b64encode(resp.content).decode("ascii"),
        }
