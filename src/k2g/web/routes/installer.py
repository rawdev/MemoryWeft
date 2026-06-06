"""BP-76 — Web routes for the multi-client MCP installer."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field

from k2g.installer import (
    apply as installer_apply,
    available_clients,
    installer_status,
    preview as installer_preview,
    uninstall as installer_uninstall,
)
from k2g.web.deps import sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["installer"])


class InstallerRequest(BaseModel):
    clients: list[str] | None = Field(
        default=None,
        description="Slugs to act on; None = all known clients.",
    )
    project_dir: str | None = Field(
        default=None,
        description="Per-project scope. Defaults to env K2G_PROJECT_DIR.",
    )
    slug: str | None = Field(
        default=None,
        description="Registry entry slug — pins which entry's env is written when "
                    "multiple entries share the same project_dir.",
    )
    server_block: dict[str, Any] | None = Field(
        default=None,
        description="Override the mweft server entry (advanced).",
    )


def _resolve_project_dir(req: InstallerRequest) -> str | None:
    if req.project_dir:
        return req.project_dir
    env_dir = os.environ.get("K2G_PROJECT_DIR", "").strip()
    return env_dir or None


@router.get("/installer/clients")
def list_clients() -> dict[str, Any]:
    return sanitize({"clients": available_clients()})


@router.get("/installer/status")
def status(
    project_dir: str | None = Query(
        None, description="Per-project scope; defaults to env K2G_PROJECT_DIR."),
) -> dict[str, Any]:
    """Per-client install status (installed / scope / config path) for the UI list."""
    pd = project_dir or os.environ.get("K2G_PROJECT_DIR", "").strip() or None
    return sanitize({"project_dir": pd, "clients": installer_status(project_dir=pd)})


@router.get("/installer/global-targets")
def global_targets_route() -> dict[str, Any]:
    """Read-only matrix: each global AI client and the project it points at."""
    from k2g.installer import global_targets
    return sanitize({"clients": global_targets()})


@router.post("/installer/preview")
def preview(body: InstallerRequest = Body(default_factory=InstallerRequest)) -> dict[str, Any]:
    report = installer_preview(
        body.clients,
        project_dir=_resolve_project_dir(body),
        server_block=body.server_block,
        entry_slug=body.slug,
    )
    return sanitize(report.to_dict())


@router.post("/installer/preflight")
def preflight(body: InstallerRequest = Body(default_factory=InstallerRequest)) -> dict[str, Any]:
    """Pre-install conflict probe. For each requested client, report whether an
    existing on-disk mweft block points at a *different* project — so the UI can
    warn before the install silently overwrites it."""
    from k2g.installer import install_conflict
    pd = _resolve_project_dir(body)
    out = [
        install_conflict(s, project_dir=pd, entry_slug=body.slug)
        for s in (body.clients or [])
    ]
    return sanitize({"clients": out})


@router.post("/installer/apply")
def apply(body: InstallerRequest = Body(default_factory=InstallerRequest)) -> dict[str, Any]:
    report = installer_apply(
        body.clients,
        project_dir=_resolve_project_dir(body),
        server_block=body.server_block,
        entry_slug=body.slug,
    )
    return sanitize(report.to_dict())


@router.post("/installer/uninstall")
def uninstall(body: InstallerRequest = Body(default_factory=InstallerRequest)) -> dict[str, Any]:
    report = installer_uninstall(
        body.clients,
        project_dir=_resolve_project_dir(body),
        entry_slug=body.slug,
    )
    return sanitize(report.to_dict())
