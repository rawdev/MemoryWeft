"""K2G Vector2Text Web Agent — FastAPI application.

Router registration, CORS middleware, and static file serving.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles


class _Neo4jSafeEncoder(json.JSONEncoder):
    """JSON-serialize Neo4j native types (DateTime etc.)."""

    def default(self, o: Any) -> Any:  # noqa: ANN401
        # neo4j.time.DateTime / Date / Time → iso_format()
        if hasattr(o, "iso_format"):
            return o.iso_format()
        if isinstance(o, (datetime, date, time)):
            return o.isoformat()
        return super().default(o)


class SafeJSONResponse(JSONResponse):
    """JSONResponse that safely serializes Neo4j types."""

    def render(self, content: Any) -> bytes:  # noqa: ANN401
        return json.dumps(
            content,
            cls=_Neo4jSafeEncoder,
            ensure_ascii=False,
        ).encode("utf-8")

from k2g.web.deps import shutdown, startup

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    """Startup / Shutdown events."""
    startup()
    yield
    shutdown()


app = FastAPI(
    title="K2G Vector2Text Agent",
    description="K2G graph exploration and conditional event generation web agent",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=SafeJSONResponse,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
from k2g.web.routes import archive, browse, community, control, entity_admin, event, generate, persona, predefine, search, search_explorer, tag, train  # noqa: E402
try:  # Plan Studio — internal asset, absent in the OSS distribution.
    from k2g.web.routes import plan  # noqa: E402
except ImportError:  # pragma: no cover
    plan = None

app.include_router(search.router, prefix="/api")
app.include_router(browse.router, prefix="/api")
app.include_router(event.router, prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(tag.router, prefix="/api")
app.include_router(control.router, prefix="/api")
app.include_router(persona.router, prefix="/api")
if plan is not None:
    app.include_router(plan.router, prefix="/api")
app.include_router(train.router, prefix="/api")
app.include_router(community.router, prefix="/api")
app.include_router(entity_admin.router, prefix="/api")
app.include_router(predefine.router, prefix="/api")
app.include_router(search_explorer.router, prefix="/api")
app.include_router(archive.router, prefix="/api")

# Static file serving (index.html)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


def main() -> None:
    """CLI entry point."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("k2g.web.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
