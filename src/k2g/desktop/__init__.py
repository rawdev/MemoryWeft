"""Native desktop app for the MWeft Manager UI.

A thin pywebview wrapper around the existing per-project FastAPI backend
(``k2g.ui_project``): uvicorn is booted **in-process** bound to ``127.0.0.1``
(an invisible loopback server) and the same single-page frontend is rendered in
a native window instead of a browser tab.

The whole thing is **one standalone process** — no hub, no browser, no child
processes. Project switching is an **in-memory** swap of the active ``Settings``
(``k2g.desktop.switch``), not a subprocess respawn, so there are no zombie
servers and no port hand-off.

Entry points::

    mweft-app --slug <project-slug>
    python -m k2g.desktop --project-dir <folder>
"""
