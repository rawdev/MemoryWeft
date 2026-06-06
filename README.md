# MWeft

**English** | [한국어](README.KR.md)

> Local-first graph memory for MCP — where **events** *are* the edges.

MWeft is an embedded knowledge-graph memory layer that ships as a single
stdio MCP server backed by one SQLite file. No server to provision, no
remote API key required, no graph database to install — just a Python
process and a file.

It's designed for memory whose value lives in **relational nuance**, not
discrete facts.

## What makes it different

Most graph memory systems (Graphiti, mem0, …) ingest episodes and
**extract typed entity-entity relations** from them
(`A ─[KNOWS]─ B`, `A ─[WORKS_AT]─ C`). That extraction commits the model's
interpretation at write time, quantizes relational nuance into a discrete
label, and adds an extra hallucination surface.

**MWeft takes the opposite stance: the event itself is the edge.** Two
entities are "related" if they co-participate in an event, and the
relationship's content lives in the event's vector + summary. There is no
typed-relation extraction step, no relation schema to maintain, no
quantization at ingestion.

| | MWeft | Graphiti / mem0 |
|---|---|---|
| Relation model | event = edge (vector + summary) | typed extracted edges |
| Nuance preservation | full (continuous embedding) | lossy (discrete label) |
| Schema / drift | none | requires ontology |
| Multi-hop reasoning | semantic (soft-typed) | precise (hard-typed) |
| "Is it still true?" | query-time synthesis | edge invalidation |
| Sweet spot | narrative, design, evolving meaning | factual KB, time-changing facts |

MWeft's model can *approximate* typed retrieval at query time (soft-typed
semantic hops). The reverse — recovering nuance from already-labeled
edges — isn't possible. That makes MWeft the right fit when relationships
are textured rather than discrete: writing, design context, project
history with rationale, anything where "why" matters as much as "what".

## Architecture in 30 seconds

Three node kinds — **entities**, **events**, **tags** — and a small set
of edges:

- `participated_in` (entity ↔ event)
- `event_sequential_next` (event → event — document order / threads)
- `event_member_of` (event → tag)
- `entity_connection` (entity ↔ entity — co-occurrence count)
- `event_jaccard_connected` (event ↔ event — shared entity/tag footprint)

Both entities and events carry vector embeddings (BGE-M3 by default,
embedded in process). Search returns hits **with a connection-map hint** —
a structure pointing the LLM at adjacent events, shared entities,
co-occurrence neighbors, and semantically similar events, all without
forcing extra tool calls.

On top of the raw graph, MWeft runs **Leiden community detection** over
both the entity and event graphs to surface emergent clusters
("auto-tags") with no manual labeling. The `mweft_auto_tag_*` and
`mweft_community_*` tools let the LLM summarize the cluster structure and
drill into a community's members.

The whole graph lives in one SQLite file (`sqlite-vec` for the vector
index). Postgres + pgvector is supported as an alternative backend.

## What's in this distribution

The MCP surface exposes a focused set of read/store tools:

| | |
|---|---|
| **Search / read** | `mweft_search`, `mweft_entity_lookup`, `mweft_get_event_content` |
| **Graph traversal** | `mweft_neighbors`, `mweft_relations`, `mweft_temporal_flow` |
| **Communities / auto-tags** | `mweft_auto_tag_summarize`, `mweft_auto_tag_list`, `mweft_auto_tag_members`, `mweft_auto_tag_detail`, `mweft_auto_tag_of`, `mweft_community_explore`, `mweft_community_residual` |
| **Write** | `mweft_remember`, `mweft_remember_edit` |
| **Free SQL** | `mweft_sql_query`, `mweft_describe_schema`, `mweft_explain_query` |

Hint surface (returned with every search):

- `connections.sequential` — adjacent document chunks
- `connections.similar` — entity embedding neighbors
- `connections.connected` — co-occurrence neighbors
- `hint.threads` / `entities` / `context_groups` / `categories` — cross-hit
  connection map with fixed `reason` tags

## Status

**Pre-release.** The core memory model and search surface are stable.

Not in this distribution (yet):
- The full K2G build pipeline (heavy LLM ingestion). MWeft accepts memory
  via `mweft_remember`; large-scale build is a separate concern.

## Install

The recommended setup uses local **ONNX** embeddings — no API key, and no
PyTorch at runtime:

```bash
pip install -e .[embed-onnx]
```

Export the BGE-M3 model to ONNX once (`optimum-cli export onnx -m
BAAI/bge-m3 ./models/bge-m3-onnx`) and point `EMBEDDING_ONNX_PATH` at it.
Prefer a zero-setup API key, or PyTorch-managed weights instead? See the
embedding options in [install.md](install.md#embedding--onnx-openai-or-pytorch).

Then register the server with your MCP client — all configuration goes in
the client's `env` block, no separate config file. Example for Claude Code
(`~/.claude.json` or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "mweft": {
      "command": "k2g-mcp",
      "env": {
        "DATA_DIR": "/absolute/path/to/mweft_data",
        "EMBEDDING_PROVIDER": "onnx",
        "EMBEDDING_ONNX_PATH": "/absolute/path/to/models/bge-m3-onnx",
        "EMBEDDING_DIM": "1024"
      }
    }
  }
}
```

Full setup — client matrix (Claude Code / Desktop, Cursor, Gemini CLI),
SQLite vs PostgreSQL backends, and all env knobs — see
[install.md](install.md).

An optional native desktop app (`mweft-app`) gives you a window to browse
domains, tags, entities, and search. Install the `manager` extra
(`pip install -e .[manager]`) and see
[install.md](install.md#manager-desktop-app-mweft-app).

For getting the LLM to use MWeft well (search heuristics, save triggers,
the connection-map hint), see [prompt_guide.md](prompt_guide.md) — drop
the snippet into your `CLAUDE.md` / `GEMINI.md` / `.cursorrules`.

## First run & antivirus

The very first run — or the first connection right after a reboot — can be slow.
Antivirus / security software (Windows Defender and others) scans the embedding
model, runtime libraries, and DB files on **first access**; once scanned, startup
is fast. If every cold boot is slow, add the **install folder and your `DATA_DIR`
to your antivirus's real-time scan exclusions** to greatly speed up cold start.
On macOS, Gatekeeper / quarantine checks can cause a similar one-time first-run
delay. This is a scan cost, not a hang — the server still comes up, just slower on
the first cold access.

## License

[Apache License 2.0](LICENSE).

---

**ECKG context.** MWeft sits in the *event-centric knowledge graph* (ECKG)
space that gained traction in 2024–2026. Where most ECKG work focuses on
*extraction quality*, MWeft asks whether extraction is the right move at
all when relations are textured.
