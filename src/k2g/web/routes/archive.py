"""BP-74 Phase B — archive export/import HTTP API.

Synchronous endpoints suitable for the single-user mweft-ui scenario.  Very
large domains that exceed HTTP timeout windows need an SSE/WebSocket path —
see BP-74 §10.

Endpoints:
- ``POST /api/archive/export``  — produce a .mweft.tar.gz on the server disk
- ``POST /api/archive/preview`` — read manifest.json from an existing archive
- ``POST /api/archive/import``  — apply archive rows to the target graph store
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from k2g.core.config import get_settings
from k2g.db_store.factory import _resolve_backend_mode
from k2g.portable.archive_io import ArchiveReader
from k2g.portable.exporter import DomainExporter
from k2g.portable.importer import (
    DomainImporter,
    SchemaVersionMismatch,
    TargetNotEmpty,
)
from k2g.portable.manifest import ArchiveManifest, ArchiveOptions
from k2g.web.deps import get_stores_dep, sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["archive"])


# ---------------------------------------------------------------------------
# request / response schemas
# ---------------------------------------------------------------------------

class ExportOptionsIn(BaseModel):
    include_segments: bool = True
    include_plan: bool = True
    include_vectors: bool = False
    include_audit: bool = False


class ExportRequest(BaseModel):
    domain: str = Field(..., description="Domain to export (e.g., 'ai_memory').")
    archive_path: str = Field(
        ...,
        description="Output directory or .mweft.tar.gz file path on server disk.",
    )
    options: ExportOptionsIn = Field(default_factory=ExportOptionsIn)


class PreviewRequest(BaseModel):
    archive_path: str = Field(..., description="Path to .mweft.tar.gz on server disk.")


class ImportRequest(BaseModel):
    archive_path: str
    mode: str = Field(
        default="merge",
        description="merge (legacy, per-row strategy) | restore (clone/replace).",
    )
    strategy: str = Field(
        default="skip",
        description="merge-mode PK conflict strategy: skip | overwrite | fail.",
    )
    confirm_replace: bool = Field(
        default=False,
        description="restore mode: required to wipe a non-empty target.",
    )
    dry_run: bool = False


# ---------------------------------------------------------------------------
# runtime helpers
# ---------------------------------------------------------------------------

def _runtime_meta() -> tuple[str, str, dict[str, Any]]:
    """Resolve backend / group / embedding meta from current Settings + env."""
    settings = get_settings()
    backend = _resolve_backend_mode(settings)
    group = os.environ.get("K2G_USER_MEMORY_SAVE_GROUP") or "default"
    embedding = {
        "provider": str(getattr(settings, "embedding_provider", "")),
        "model": str(getattr(settings, "embedding_model", "")),
    }
    return backend, group, embedding


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@router.post("/archive/export")
def archive_export(
    body: ExportRequest,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    backend, group, embedding = _runtime_meta()
    options = ArchiveOptions(**body.options.model_dump())
    exporter = DomainExporter(stores["graph"], backend, group=group)
    try:
        manifest = exporter.export(
            body.domain,
            body.archive_path,
            options=options,
            embedding=embedding,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("archive export failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"export failed: {exc}")

    final_path = exporter.last_archive_path or Path(body.archive_path)
    size = final_path.stat().st_size if final_path.is_file() else 0
    return sanitize({
        "archive_path": str(final_path),
        "size_bytes": size,
        "manifest": manifest.model_dump(),
    })


@router.post("/archive/preview")
def archive_preview(body: PreviewRequest) -> dict:
    p = Path(body.archive_path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"archive not found: {p}")
    try:
        with ArchiveReader(p) as r:
            manifest = ArchiveManifest.from_json(r.read_text("manifest.json"))
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail="archive has no manifest.json — not a valid .mweft archive",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid archive: {exc}")
    return sanitize({
        "archive_path": str(p),
        "size_bytes": p.stat().st_size,
        "manifest": manifest.model_dump(),
        "compatible": manifest.is_compatible(),
    })


@router.post("/archive/import")
def archive_import(
    body: ImportRequest,
    stores: dict[str, Any] = Depends(get_stores_dep),
) -> dict:
    backend, _, _ = _runtime_meta()
    p = Path(body.archive_path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"archive not found: {p}")

    importer = DomainImporter(stores["graph"], backend)
    try:
        result = importer.import_archive(
            body.archive_path,
            strategy=body.strategy,
            dry_run=body.dry_run,
            mode=body.mode,
            confirm_replace=body.confirm_replace,
        )
    except TargetNotEmpty as exc:
        # Restore into a populated DB — caller must confirm the destructive
        # replace. Structured detail drives the UI's typed-REPLACE warning.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "target_not_empty",
                "message": str(exc),
                "existing_counts": exc.counts,
            },
        )
    except SchemaVersionMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("archive import failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"import failed: {exc}")

    return sanitize({
        "dry_run": result["dry_run"],
        "mode": result.get("mode", "merge"),
        "strategy": result.get("strategy"),
        "existing_counts": result.get("existing_counts", {}),
        "manifest": result["manifest"].model_dump(),
        "results": result["results"],
    })
