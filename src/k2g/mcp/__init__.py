"""K2G ChronoGraph MCP — L3 episodic memory read surface.

Read (BP-29 rev):

- :func:`k2g.mcp.factory.build_dependencies` — assemble graph/vector/embedding
- :func:`k2g.mcp.tools.search_tool` — hybrid / events / entities vector search
- :func:`k2g.mcp.tools.entity_lookup_tool` — entity → connected events
- :func:`k2g.mcp.tools.context_detail_tool` — one CG's narrative + hierarchy + events

Write path: no MCP write tool. BP-29 rev uses AI-proposed md curation with
user consent → :mod:`scripts.ingest_text_batch`. The Stop hook pipeline
(:mod:`k2g.hooks.cache_turn`, :mod:`k2g.cli.build_memory`) is deprecated.
The stdio server is started via ``python -m k2g.mcp`` (see ``__main__.py``).
"""

from k2g.mcp.factory import Deps, build_dependencies
from k2g.mcp.tools import (
    context_detail_tool,
    entity_lookup_tool,
    search_tool,
)

__all__ = [
    "Deps",
    "build_dependencies",
    "search_tool",
    "entity_lookup_tool",
    "context_detail_tool",
]
