# MWeft

**English** | [한국어](README.KR.md)

MemoryWeft is a knowledge-graph memory that implements the **Event-Centric
Knowledge Graph (ECKG)** idea.

It runs as a stdio MCP server backed by a single SQLite file, or Postgres +
pgvector.


**MemoryWeft is shared memory. Multiple AIs can access one memory at the same
time.**

**AIs running separately on your laptop and desktop — and the AIs each teammate
uses on their own — share context out of a single memory.**

**Save an important conversation with your AI using a command, or convert a whole
document into memory in one shot.**

**Stored data can be recalled anytime with a command, and the AI will go on to
search for related content by context and fold it into the results.**

Just use commands like "mw save", "mw search", or "save all these documents to
mw".

## Architecture

MemoryWeft splits information into **entities** (objects) and **events**
(descriptions of how objects relate), storing each separately, and computes and
stores alongside them the values it needs for text, RAG, and graph relations.

The resulting graph relations are defined and handled in the two ways below:

1. Entity-to-entity relations are defined from the set of events the entities
   co-participate in.

2. Event-to-event relations are defined fluidly from their shared entities.

Because it's shared memory, an event once added should not be deleted if at all
possible.

MemoryWeft offers two ways to store — MCP functions and bulk document ingestion —
both fully controllable by an AI through tool calls. Stored data can then be
searched from many angles — keyword, RAG, and graph relations — and returns rich
results.

Node kinds
- `entities`
- `events`
- `tags`

Node relations
- `participated_in` (entity ↔ event)
- `event_sequential_next` (event → event — document order / threads)
- `event_member_of` (event → tag)
- `entity_connection` (entity ↔ entity — co-occurrence count)
- `event_jaccard_connected` (event ↔ event — shared entity/tag footprint)

Both entities and events carry vector embeddings (BGE-M3 by default, embedded in
process).

Search returns not only the matched content but also a **connection-map hint**.

The connection-map hint points the LLM at adjacent events, shared entities,
co-occurrence neighbors, and semantically similar events, to guide its next move.

MemoryWeft runs **Leiden community detection** over both the entity and event
graphs to surface emergent clusters automatically.

The `mweft_auto_tag_*` and `mweft_community_*` tools let the LLM summarize the
cluster structure and drill into a specific community's members.

## Manager

A bundled **Manager** app helps you install the MCP and switch databases, so you
can easily pick which DB to use.

![MemoryWeft Manager — Domain Summary](asset/1.png)

*The bundled **Manager** app shows a domain at a glance — entity / event /
connection counts, Leiden communities, and the top hub entities.*

![Manager — Analysis: Leiden community graph](asset/3.png)

*The Manager's **Analysis** tab renders the Leiden entity / event graph; click a
node to open its event detail.*

## What makes it different

Most graph-memory systems extract **typed entity-entity relations**
(`A ─[KNOWS]─ B`, `A ─[WORKS_AT]─ C`). That extraction commits the model's
interpretation at write time.

**MemoryWeft doesn't pre-decide: the event itself is the edge.** Two entities
are "related" when they co-participate in an event, and the relationship's
content lives in that event's vector + summary. There is no typed-relation
extraction step, and no relation schema to maintain.

| | MemoryWeft | Other systems |
|---|---|---|
| Relation model | event = edge (vector + summary) | typed extracted edges |
| Nuance preservation | full (continuous embedding) | lossy (discrete label) |
| Schema / drift | none | requires ontology |
| Multi-hop reasoning | semantic (soft-typed) | precise (hard-typed) |
| "Is it still true?" | query-time synthesis | edge invalidation |
| Sweet spot | narrative, design, evolving meaning | factual KB, time-changing facts |

MemoryWeft's model can *approximate* typed retrieval at query time. That makes
it the right fit when relationships are textured rather than discrete: writing,
design context, project history with rationale — anything where "why" matters
as much as "what".

## What's in this distribution

The MCP surface exposes a focused set of read/store tools:

| | |
|---|---|
| **Search / read** | `mweft_search`, `mweft_entity_lookup`, `mweft_get_event_content` |
| **Graph traversal** | `mweft_neighbors`, `mweft_relations`, `mweft_temporal_flow` |
| **Communities** | `mweft_auto_tag_summarize`, `mweft_auto_tag_list`, `mweft_auto_tag_members`, `mweft_auto_tag_detail`, `mweft_auto_tag_of`, `mweft_community_explore`, `mweft_community_residual` |
| **Write** | `mweft_remember`, `mweft_remember_edit` |
| **Document CLI** | `k2g-ingest-manifest`, `k2g-manifest-check` |
| **Free SQL** | `mweft_sql_query`, `mweft_describe_schema`, `mweft_explain_query` |

Hints returned with search:

- `connections.sequential` — adjacent document chunks
- `connections.similar` — entity embedding neighbors
- `connections.connected` — co-occurrence neighbors
- `hint.threads` / `entities` / `context_groups` / `categories` — cross-hit
  connection map with fixed `reason` tags

## Status

**Pre-release.** The core features are stable.

Some conveniences are still missing — for example, hiding the Postgres DSN from
the MCP configuration.

## Quickstart — portable app (no Python)

The easiest way — no Python, no pip, no config files:

1. Download the `mweft-<platform>-<version>.zip` for your OS from the
   **[Releases](https://github.com/rawdev/MemoryWeft/releases/latest)** page:
   - **Windows** — `mweft-win-x64-<version>.zip`
   - **macOS (Apple Silicon)** — `mweft-mac-arm64-<version>.zip`
   - **macOS (Intel)** — `mweft-mac-x64-<version>.zip` (not currently supported.)
2. Unzip it — it's extract-and-run; nothing is installed system-wide.
   - **macOS: do NOT unzip into `Downloads`, `Desktop`, or `Documents`.** Those
     folders are protected by macOS privacy (TCC), so your AI client (Claude,
     etc.) is blocked from launching the bundled memory server out of them. Put
     it in your **home folder** instead, e.g. `~/mweft`. (Alternatively, grant
     the AI client Full Disk Access in System Settings → Privacy & Security.)
     Keep your **memory/data folder** out of those protected locations too.
3. Start the launcher:
   - **Windows** — double-click **`start-mweft.bat`**
   - **macOS** — run **`start-mweft.command`** (first time: right-click → Open to clear Gatekeeper)
4. The **Manager** window opens.
   - Create a project and choose where your memory lives (a local SQLite folder, or a Postgres DSN).
   - Enter a **domain name** — the isolation key within the DB.
   - You can delete and recreate projects anytime.
   - After starting, in Settings, click **Install MCP** for the AI client you use (Claude Desktop, Cursor, Claude Code, …).

   ![Manager — Settings: Install MCP](asset/2.png)

   *Settings → **Install MCP** registers the mweft server into your AI client (per-project or global).*

5. **Restart that AI client.** Done — just say `mw search …` / `mw save …`.

![Using MemoryWeft from an AI client](asset/4.png)

*Once it's installed, just talk to your AI — `mw save …` to store, `mw search …` to recall.*

## Uninstall

Remove MemoryWeft in this order:

1. **Uninstall from your AIs** — in the Manager's **Settings** tab, click **Remove** for each connected AI client, then **restart** any global AIs (Claude Desktop, etc.). Do this first so the `mweft` entry is cleanly removed from each AI's config.
2. **Delete the mweft folder** — delete the whole unzipped portable folder (runtime, model, and the `data/` memory). The `data/` folder holds your SQLite memory DB, so back it up first if you need it.
3. **Delete `.mweft` in your home folder** — `%USERPROFILE%\.mweft` (Windows) / `~/.mweft` (macOS). It holds global config such as the project list (`mweft_manager.json`).

The portable build ships an **`uninstall-mweft`** script (`.bat` / `.command`) that does steps 2 & 3 for you — step 1 (AI removal) stays manual in the Manager for safety. If you used **Postgres**, the memory DB lives on that server; delete it separately.

## Install via pip (developers)

```bash
pip install -e .[embed-onnx]
```

Export the BGE-M3 model to ONNX once (`optimum-cli export onnx -m
BAAI/bge-m3 ./models/bge-m3-onnx`) and point `EMBEDDING_ONNX_PATH` at it.
Prefer a zero-setup API key, or PyTorch-managed weights instead? See the
embedding options in [install.md](docs/install.md#embedding--onnx-openai-or-pytorch).

Then register the server with your MCP client — all configuration goes in the
client's `env` block, no separate config file. Example for Claude Code
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
[install.md](docs/install.md).

An optional native desktop app (`mweft-app`) gives you a window to browse
domains, tags, entities, and search. Install the `manager` extra
(`pip install -e .[manager]`) and see
[install.md](docs/install.md#manager-desktop-app-mweft-app).

For getting the LLM to use MemoryWeft well (search heuristics, save triggers,
the connection-map hint), see [prompt_guide.md](docs/prompt_guide.md) — drop
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
