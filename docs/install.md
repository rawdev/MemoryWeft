# Install

MWeft ships as a **local stdio MCP server** — your MCP client (Claude
Code, Claude Desktop, Cursor, Gemini CLI, etc.) spawns it as a child
process and talks JSON-RPC over stdin/stdout. No network port, no remote
auth, no separate daemon.

> Note on ChatGPT: ChatGPT can't spawn local stdio servers, so MWeft v1 is
> not used from ChatGPT. HTTP transport is on the roadmap.

## Contents
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Manager desktop app (`mweft-app`)](#manager-desktop-app-mweft-app)
- [Register with your MCP client](#register-with-your-mcp-client)
- [Backend — SQLite or PostgreSQL](#backend--sqlite-or-postgresql)
- [Embedding — ONNX, OpenAI, or PyTorch](#embedding--onnx-openai-or-pytorch)
- [Environment reference](#environment-reference)
- [Verifying & troubleshooting](#verifying--troubleshooting)

## Prerequisites

- Python 3.11+
- (Optional) PostgreSQL 14+ with `pgvector` if you want the PG backend
- (Optional) ~2GB disk if you choose local BGE-M3 embeddings instead of
  OpenAI

## Installation

This is a source-only release. `pyproject.toml` is included so a local
editable install works:

```bash
git clone <repo-url> mweft && cd mweft
pip install -e .                      # SQLite + OpenAI embeddings
pip install -e .[postgres]            # + psycopg2 + pgvector
pip install -e .[embed-local]         # + sentence-transformers + torch
pip install -e .[postgres,embed-local]
```

PyPI / `uvx` / `.mcpb` / Docker packaging is on the roadmap.

## Manager desktop app (`mweft-app`)

`mweft-app` is an **optional native desktop UI** for managing your
projects (domains, tags, entities, search, settings) in a window. It is
*not* required to use MWeft as an MCP server — your AI client talks to
`k2g-mcp` directly. The app is just a convenient front end over the same
local data.

It runs the per-project app **in-process via pywebview — no HTTP server,
no listening socket**. Project switching is an in-memory config swap.

Install the `manager` extra (adds `pywebview`, `fastapi`, `uvicorn`):

```bash
pip install -e .[manager]                 # + OpenAI embeddings
pip install -e .[embed-onnx,manager]      # + local ONNX embeddings (no torch)
pip install -e .[postgres,manager]        # + PostgreSQL backend
```

> **Windows:** the native window needs the **WebView2 runtime**
> (preinstalled on Windows 11; otherwise get it from
> <https://developer.microsoft.com/microsoft-edge/webview2/>).

Launch it:

```bash
mweft-app --project-dir /absolute/path/to/project   # open (and seed) a project folder
mweft-app --slug <project-slug>                      # open a registered project
mweft-app                                            # reopen the last-active project
mweft-app --no-window                                # headless self-test (no window/socket)
```

The first `--project-dir` run creates the folder and registers it in
`~/.mweft/mweft_manager.json`; later runs can use `--slug` or the
last-active default.

## Register with your MCP client

MWeft is configured entirely through the **`env` block of your MCP client
config** — the same place you declare the server. There is no separate
config file to manage: every setting is just an environment variable
handed to the `k2g-mcp` process (`DATA_DIR`, the embedding keys, an
optional Postgres DSN, save-time defaults).

The recommended setup is SQLite + local ONNX embeddings (no API key, no
torch). Export the BGE-M3 model once first — see
[Embedding](#embedding--onnx-openai-or-pytorch).

The MCP spec uses the same `command` / `args` / `env` shape everywhere;
only *where the config file lives* differs per client.

### Claude Code (CLI)

Project-level `.mcp.json` (global `~/.claude.json` is identical):

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

Prefer OpenAI? Swap the embedding keys (everything else is the same shape):

```json
"env": {
  "DATA_DIR": "/absolute/path/to/mweft_data",
  "EMBEDDING_PROVIDER": "openai",
  "EMBEDDING_MODEL": "text-embedding-3-small",
  "EMBEDDING_DIM": "1536",
  "OPENAI_API_KEY": "sk-..."
}
```

The other clients below use the **same `env` keys** — only the file
location and syntax differ.

### Claude Desktop

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

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

### Cursor

- Global: `~/.cursor/mcp.json`
- Project: `.cursor/mcp.json`

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

### Gemini CLI

`~/.gemini/settings.json`:

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

### Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.mweft]
command = "k2g-mcp"

[mcp_servers.mweft.env]
DATA_DIR = "/absolute/path/to/mweft_data"
EMBEDDING_PROVIDER = "onnx"
EMBEDDING_ONNX_PATH = "/absolute/path/to/models/bge-m3-onnx"
EMBEDDING_DIM = "1024"
```

> **Secrets:** keys like `OPENAI_API_KEY` or a Postgres DSN live in this
> `env` block. If your client config is committed to version control, keep
> secrets out of it (e.g. use a private/global client config, not a
> repo-tracked one).

## Backend — SQLite or PostgreSQL

### SQLite (default)

Nothing else to do. `sqlite-vec` is bundled via pip. The whole graph
lives in `DATA_DIR/k2g_all_in_one.db`. Suited for single-user, single-host
memory at any size you can reasonably keep in one SQLite file.

### PostgreSQL + pgvector

Install the optional dep:

```bash
pip install -e .[postgres]
```

Make sure `pgvector` is installed in your DB (one-time, by a DB owner):

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Add the DSN to your MCP client's `env` block (alongside the embedding
keys); a non-empty DSN is all it takes to switch to PG mode:

```json
"env": {
  "DATA_DIR": "/absolute/path/to/mweft_data",
  "K2G_POSTGRES_DSN": "postgresql://user:pass@host:5432/dbname",
  "EMBEDDING_PROVIDER": "onnx",
  "EMBEDDING_ONNX_PATH": "/absolute/path/to/models/bge-m3-onnx",
  "EMBEDDING_DIM": "1024"
}
```

That single DSN drives the graph, the vector index, and the content
store. To split them onto different databases, set `POSTGRES_GRAPH_DSN`
and `POSTGRES_VECTOR_DSN` instead.

Backend selection is automatic: a non-empty DSN flips MWeft to PG mode;
otherwise SQLite. You can also be explicit with `GRAPH_DB_PROVIDER=postgres`.

Schema (tables, indexes, vector columns) is created on first start. If
you want to skip that (read-only environment behind a pooler that doesn't
permit DDL), set `K2G_DB_SKIP_SCHEMA_SETUP=true`.

## Embedding — ONNX, OpenAI, or PyTorch

MWeft embeds entities and events at write time and queries at read time.
Pick one provider; mixing them poisons the index (different vector spaces).

Three backends, in recommended order:

### Local — BGE-M3 via ONNX (recommended: no API key, no torch)

The leanest local option — CPU `onnxruntime`, no PyTorch, no API key, no
per-token cost. This is the recommended default for self-hosting.

```bash
pip install -e .[embed-onnx]
```

You supply the BGE-M3 model in ONNX form: a directory containing
`model.onnx` + `tokenizer.json`. Export it once with `optimum`
(this is a **build-time** step — `torch` is needed only here, never at
runtime):

```bash
pip install 'optimum[onnxruntime]' transformers torch
optimum-cli export onnx -m BAAI/bge-m3 ./models/bge-m3-onnx
```

Then set these in your MCP client's `env` block:

```json
"env": {
  "DATA_DIR": "/absolute/path/to/mweft_data",
  "EMBEDDING_PROVIDER": "onnx",
  "EMBEDDING_ONNX_PATH": "/absolute/path/to/models/bge-m3-onnx",
  "EMBEDDING_DIM": "1024"
}
```

`EMBEDDING_ONNX_PATH` may be the directory (it looks for `model.onnx` +
`tokenizer.json` inside) or the `model.onnx` file directly.

### OpenAI (zero-setup, needs an API key)

```ini
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
OPENAI_API_KEY=sk-...
```

`text-embedding-3-large` (3072 dim) also works — set `EMBEDDING_DIM=3072`.

### Local — BGE-M3 via PyTorch

```bash
pip install -e .[embed-local]
```

```ini
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIM=1024
```

First run downloads the model into your transformers cache (~2GB). Use
this if you'd rather let `sentence-transformers` manage the model than
export an ONNX file yourself.

## Environment reference

Most users set 4–6 keys. Full list below.

### Required
- `DATA_DIR` — root for SQLite DBs, object cache, logs
- `EMBEDDING_PROVIDER` — `onnx` | `openai` | `local` | `dummy` (test only)
- `EMBEDDING_MODEL`, `EMBEDDING_DIM` — must match the provider
- `EMBEDDING_ONNX_PATH` — iff `EMBEDDING_PROVIDER=onnx` (dir or `model.onnx`)
- `OPENAI_API_KEY` — iff `EMBEDDING_PROVIDER=openai`

### Backend
- `K2G_POSTGRES_DSN` — flips to PG mode when set
- `POSTGRES_GRAPH_DSN`, `POSTGRES_VECTOR_DSN` — split backends (optional)
- `GRAPH_DB_PROVIDER` — `sqlite` (default) | `postgres` (explicit override)
- `K2G_DB_SKIP_SCHEMA_SETUP` — `true` to skip DDL on startup

### Save-time defaults
- `K2G_USER_MEMORY_SAVE_DOMAIN` — fallback domain for `mweft_remember`
  when the caller doesn't pass one. Default `ai_memory`. Case-sensitive
  on the value (`K2G` ≠ `k2g`)
- `K2G_USER_MEMORY_SAVE_GROUP` — fallback working_folder for
  `mweft_remember`
- `K2G_USER_SEARCH_TARGETS` — CSV of `domain[:group]` for search defaults

### Bootstrap & runtime
- `K2G_MCP_LAZY_INIT` — `true` to defer dep building until first tool call
  (use if your client has a short startup timeout)
- `DEBUG_MODE` — `true` writes `mcp_debug.log` to `DATA_DIR`

### Hub / proxy (optional)
- `K2G_USE_HUB` — `auto` (default) | `true` | `false`
- `K2G_HUB_URL` — override hub URL
- `K2G_SUBPROCESS_MODE` — set by the launcher hub; do not set manually

Anything not listed here is read by MWeft but rarely useful in single-user
stdio mode; `python -c "from k2g.core.config import Settings;
print(Settings.model_fields)"` shows the full list.

## Verifying & troubleshooting

### Smoke test on the command line

```bash
k2g-mcp
```

You should see a startup log line ending in `mcp_server_start`. Hit
Ctrl-C — MWeft is meant to be driven by an MCP client, not interactively.

### "MWeft can't find my memories"

Common cause: two different `DATA_DIR` paths in different runs. MWeft
creates its DB on demand — pointing at a new path silently creates an
empty one. Confirm with:

```bash
ls -l $DATA_DIR/k2g_all_in_one.db
```

### "Embeddings look wrong / queries return weird results"

Usually a provider/model/dimension mismatch — the index was built with
one embedding and queried with another. Re-ingest after fixing
`EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM`.

### "Tool calls hang on first use"

If your MCP client has a tight startup timeout (~30s), the eager init of
embedding + graph deps can race it. Set `K2G_MCP_LAZY_INIT=true` to defer
dep building to first tool call.

### Debug log

`DEBUG_MODE=true` writes a verbose `mcp_debug.log` under `DATA_DIR`. Tail
it while reproducing the issue.
