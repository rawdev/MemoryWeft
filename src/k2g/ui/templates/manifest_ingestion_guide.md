# Guide — AI Manifest Ingestion (MWeft BP-95)

> **Audience**: **any AI** that wants to ingest documents into MWeft memory at
> file/folder granularity (any agent that can write files + run a shell).
> **Contract**: the manifest JSON schema (§3) + the CLI command (§4). Honor just
> those two and it works without MCP/Skill.

---

## 1. When is it a manifest — decision branch (read first)

Manifest ingestion is strictly for **AI value-added ingestion**:

- ✅ **Use a manifest**: summaries/excerpts/restructurings of conversational
  context; split a long document into semantic units and store each chunk with
  an AI summary + entities. (Summary/NER *already performed by the AI*.)
- ❌ **Not a manifest → single save**: for a single short item, use the
  `mweft_remember` MCP tool. A manifest must carry **your own AI summaries +
  entities** — do not dump raw, unsummarized text through it (chunk and
  summarize first).

Key point: a manifest makes **zero K2G-side LLM calls**. Summaries/entities are
trusted as-is from the AI output the manifest carries. Only the (local)
embeddings are computed by K2G. Ingested content accumulates in the same
group/tag structure as `mweft_remember`.

---

## 2. Input modes

- **File mode** (1 file → many chunks): split on semantic boundaries → each
  chunk is one item. Chunks are chained in order via `source: "chunk_order"`.
- **Folder mode** (many files): walk files, splitting into chunks. At each
  **file boundary**, pass the previous ingestion's last event_id as the next
  manifest's `prev_event_id` to extend the chain. Cross-file source is
  `file_name` / `folder_name`.

> One manifest = one atomic transaction. If a folder is large, split into
> per-file/per-batch manifests and chain them with `prev_event_id` (total caps §5).

---

## 3. Manifest schema (`*.manifest.json`)

```jsonc
{
  "schema": "k2g.manifest.v1",      // fixed — rejected if different
  "working_folder": null,           // save-root. omit → server env (K2G_USER_MEMORY_SAVE_GROUP)
  "tag": "docs/intro",              // shared default tag (item.tag takes precedence)
  "source": "chunk_order",          // 7-enum: chunk_order|file_name|folder_name|
                                    //         thread|topic_segment|user_manual|"version up"
  "prev_event_id": null,            // last event_id of the prior batch → chains this batch's first event
  "file_path": null,                // (optional) source-document identity — the key for --incremental re-ingest of changes only
  "complete": false,                // false while authoring. **true when done** — the CLI only runs on true
  "items": [
    {
      "content": "original chunk (required, ≤ 50000 chars)",
      "summary": "AI summary — the embedding target (required, ≤ 500 chars)",
      "entities": [{"name": "PlanNode", "type": "concept"}],   // AI NER (default [])
      "tag": null,                  // this item's sub-tag (omit → shared tag)
      "timestamp": null             // ISO8601 (omit → NOW)
    }
  ]
}
```

- **Do not use a `domain` field** — the ingestion domain is enforced by the
  server env (`K2G_USER_MEMORY_SAVE_DOMAIN`). Hard-coding it in the manifest is ignored.

---

## 4. CLI execution

```bash
k2g-ingest-manifest path/to/ingest.manifest.json
# remove the temp manifest after ingest:
k2g-ingest-manifest path/to/ingest.manifest.json --remove-after
# re-ingest an updated document — changed items only (manifest needs file_path):
k2g-ingest-manifest path/to/ingest.manifest.json --incremental
```

> **If `k2g-ingest-manifest` / `k2g-manifest-check` is "command not found"**:
> these are console scripts inside the **mweft bundle's venv**, which the shell's
> `PATH` may not include (portable bundles are not installed system-wide). Do
> **not** give up or silently fall back to per-event `mweft_remember` — **ask the
> user for the bundle location**, then call the script by its absolute path:
> - Windows: `<bundle>\runtime\venv\Scripts\k2g-ingest-manifest.exe`
> - macOS / Linux: `<bundle>/runtime/venv/bin/k2g-ingest-manifest`
>
> The MWeft MCP server you are connected to runs from that same venv, so its
> directory is the reference for where these scripts live.

- **Re-run safe (dedup)**: event_id is a deterministic hash of
  `(domain, working_folder, content)` — re-ingesting the same manifest yields
  zero duplicate events/chains.
- **`--source <path>` (recommended for re-ingest)**: MWeft decides change by the
  *raw byte* hash of the original (robust to LLM chunking drift). Unchanged →
  skip everything. The manifest needs `file_path`.

### 4.1 Document build (curated) — separate conversation memory from provenance (BP-96 §9.4)

When ingesting **documents/corpora** (external documents that may have a
different author, not knowledge that arose in conversation), do not inherit the
conversation-memory defaults. Passing `--tag` switches to **curated mode**:

```bash
k2g-ingest-manifest docs.manifest.json \
  --working-folder project_docs \      # docs-only root (avoid polluting the conversational ai_memory)
  --tag ProjectX --tag Design          # user-confirmed tags (repeatable)
```

How curated mode differs:
- **No autotag** — does not create per-item `tag` (category cascade).
- **No session forced-tag inheritance** — does not attach env
  `K2G_USER_MEMORY_SAVE_TAGS` (the forced tags of *this AI session*, e.g.
  org/user), since the author may differ.
- **Attaches the `--tag` values as `forced` (human-declared = Discovery)** —
  human-confirmed ground-truth tags.

> **Rule (important)**: the `--tag` value **must be confirmed by asking the
> user**. The LLM must not invent it — the point of a curated tag is that it is a
> "human-confirmed label". If a suitable tag isn't obvious, propose one to the
> user and pass it once approved.
> **`--working-folder`** should also be set for document builds (if omitted it
> falls back to the conversation-memory root → prints a warning).

### Pre-check *before* chunking (save LLM cost)

Authoring a manifest (summary/NER) costs LLM tokens. **Before chunking on a
re-ingest**, first:

```bash
k2g-manifest-check --source path/to/original --file-path docs/original.md
# "unchanged" (exit 3) → skip authoring/ingest (zero LLM cost)
# "changed"   (exit 0) → chunk → author manifest → k2g-ingest-manifest --source ...
```

- **Who runs it?** The AI runs the shell **directly** after finishing the
  manifest (`complete:true`). The user's ingestion instruction is itself the
  consent — no separate manual approval required.
- domain / community recompute runs with the **same server config** as
  `mweft_remember`. working_folder / tag default to the same structure as
  `mw save`, but a **document build separates from conversation memory and
  distinguishes provenance via `--tag` / `--working-folder` (§4.1 curated)**.
- Success output: the `[manifest]` line reporting `N/M events (domain=...)`. On
  failure, non-zero exit + reason. No partial ingest (all-or-nothing, DB rollback).

> In a development repo (source checkout), `python scripts/k2g_ingest_manifest.py
> <manifest>` works identically in place of the console script.

---

## 5. Guards / limits (honor while authoring)

| Item | Rule |
|---|---|
| **complete gate** | No execution before `complete:true` (protection during append). |
| **all-or-nothing** | If any single item has a missing content/summary, length overflow, source-enum violation, or embedding failure, the **whole batch is rejected**. No partial ingest. |
| per-item | `content ≤ 50000`, `summary ≤ 500` chars. |
| total | `items ≤ 2000`, `Σcontent ≤ ~5MB`. Over the limit → split the manifest + `prev_event_id` chain. |
| no entity hallucination | Only entities that actually appear in the text. type is a domain type (concept/person/org…). |
| entity count | If one chunk has ~50+ entities, that chunk's co-occurrence signal is lost. Keep the entity count per chunk reasonable. |
| output-token truncation | Author the manifest by **incremental append** (write the first chunk → append the rest). Dumping one giant JSON at once gets cut by the output cap. |

---

## 6. Standard workflow (steps)

1. **Decision branch** (§1) — confirm manifest ingestion is the right fit.
2. **Fix the scope** — file/folder, chunk-splitting policy.
3. **Decide tag/working_folder** — for conversational knowledge, leave
   working_folder to the server env and omit it. **For documents/external
   corpora, use curated (§4.1)**: ask the user for the tag and pass `--tag`,
   `--working-folder` to point at a docs-only root.
4. **Read files → split into chunks** — semantic boundaries first, respect the
   per-item limits (§5).
5. **Produce `{content, summary, entities}` per chunk** — summary only in
   `summary`; keep the original text in `content`.
6. **Incrementally append the manifest** — write to the file cumulatively. When
   fully written, set `complete:true`.
7. **Run the CLI** (§4) — directly via shell. Verify success.
8. **Verify/clean up** — confirm the success output, remove the temp manifest
   with `--remove-after`.
