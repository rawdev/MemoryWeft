# Prompt Guide

MCP gives the LLM a set of tools. **Whether they get used well depends on
what the LLM was told.** This guide is the prompt that turns MWeft from
"a tool that exists" into "a tool the LLM reaches for."

Drop the snippet below into your `CLAUDE.md`, `GEMINI.md`, or `.cursorrules`.
Adjust to taste.

## Contents
- [The snippet](#the-snippet)
- [Why these heuristics](#why-these-heuristics)
- [The `hint` and `reason` tags](#the-hint-and-reason-tags)
- [Save discipline](#save-discipline)
- [Bulk / document ingestion](#bulk--document-ingestion)
- [Gotchas](#gotchas)

## The snippet

Copy this into your CLAUDE.md / GEMINI.md / .cursorrules:

````markdown
## MWeft memory

You have access to a graph-based memory called MWeft. Use it to recall
prior context before you grep the filesystem or guess.

### Search path

1. Default to `mweft_search(query="<core terms>", mode="hybrid", top_k=5)`.
   Hybrid is semantic and crosses naming variants — prefer it for
   conceptual or ambiguous queries.
2. Use `mweft_entity_lookup(name)` only for precise entity enumeration.
   It is substring match — try camelCase and spaced variants
   (`PlanNode` and `plan node`) since fragmented names get missed.
3. Use `mweft_sql_query` for exact counts and table facts
   ("how many X?", "does table Y have rows?"). SELECT/WITH only.
4. Sort results by `created_at` (or `timestamp`), newest first;
   on conflict prefer the newest.

### Recency questions ("latest / most recent")

A term-anchored search can miss the latest content if the topic was
*renamed*. Before answering "what's the most recent X?":

- Run a term-agnostic sweep with `mweft_sql_query`:
  `SELECT * FROM events ORDER BY created_at DESC LIMIT 20`
- Reconcile with the term search. If the term search's newest hit is
  older than the sweep's, a successor concept likely exists — follow it
  through `connections.similar` or `hint.entities`.

### Reading the `hint` block

`mweft_search` returns a `hint` connection map alongside flat `hits`.
Read both — the hint tells you *why* hits surfaced and which neighbors
to follow up.

Per-hit `reason` tags (fixed vocabulary, categorical — judge by tag, not
by score):

- `similar-embedding` — semantically near a query concept; a likely
  **rename / synonym**. For "latest / most recent / related" questions,
  the answer may live under that renamed concept — follow it.
- `contents-chain` — adjacent document chunk (same doc, prev/next).
- `closed-jaccard` — entity/group footprint overlap inside the hit set.
- `co-occurrence` — 1-hop entity neighbors.
- `shared-context` — same ContextGroup.
- `shared-category` — same user-defined category/group.
- `spanning-entity` — an entity that appears in ≥2 hits in the result.

`hint.threads` / `entities` / `context_groups` / `categories` are
cross-hit connection maps — use them to pivot to a related cluster
without spawning extra tool calls.

### Save path

On an explicit `mw save` / `mweft save` / `memoryweft save` from the
user, call `mweft_remember`:

```python
mweft_remember(
    summary="<1–2 line summary>",
    entities=[{"name": "...", "type": "person|concept|component|..."}, ...],
    tag="<topic tag>",   # see below
    # working_folder defaults from K2G_USER_MEMORY_SAVE_GROUP env
    content="<original conversation excerpt>",  # ⚠️ ALWAYS LAST — long content can truncate the arg right after it
)
```

Picking `tag`:

- Use a short topic path: `"work"`, or `"work/auth"` for a sub-topic.
- Match against existing tags in the previous `mweft_remember`
  response's `tag_tree`. Reuse the closest fit. Create a new
  tag only when nothing fits — this prevents fragmenting tags
  into near-duplicates.
- On the first save of a session, propose your best tag from the
  conversation topic. The response confirms the resolved `tag` plus
  the full `tag_tree` for later saves.

After the save, surface what was recorded:

> Saved to tag **work/auth**. Recorded as Alice (person, new),
> Bob (person, existing). Tell me if any entity is a hallucination or
> the tag is off.

If the user flags a hallucination:
`mweft_remember_edit(event_id=<from response>, remove_entities=["Bob"])`

### Document ingestion (bulk write — files/folders)

`mweft_remember` is one conversational save. To ingest a whole **file or
folder** with summaries + entities you already produced, use **manifest
ingestion** — zero MWeft-side LLM calls (you supply summary/NER; MWeft only
embeds + links), landing in the same group/tag structure as `mw save`.

- **Decide first.** Manifest = AI value-add (your summaries / semantic chunks
  of a document). For one short note use `mweft_remember`. A manifest must
  carry *your* summaries + entities — don't dump raw, unsummarized text into
  it; chunk and summarize first.
- **Author** a `*.manifest.json` (`"schema": "k2g.manifest.v1"`): split the
  source on semantic boundaries; each chunk is one item —
  `{content (≤50000), summary (≤500, the embed target), entities: [{name,type}]}`.
  Append **incrementally** (a giant one-shot JSON gets cut by the output cap);
  set `"complete": true` only when fully written.
- **Run** it yourself via shell:
  ```bash
  k2g-ingest-manifest path/to/ingest.manifest.json --remove-after
  ```
  **Backend/domain auto-resolve — you normally pass neither.** This CLI is a
  subprocess that does not inherit the MCP env, so it resolves the project config
  by the manifest's directory: first K2G's client-agnostic project registry
  (`~/.mweft/mweft_manager.json`, keyed by project dir — applies SQLite/Postgres,
  DATA_DIR and write domain exactly like the MCP), then the client's `.mcp.json`
  as fallback. Overrides only when the project isn't registered or to force a
  target: `--mcp-config <path>` / `--domain <domain>` / `--data-dir <dir>`
  (`--data-dir` is SQLite-only; Postgres is decided by the DSN). If nothing
  resolves, it falls back to a new local SQLite under `./data` (cwd) — usually
  wrong, so ensure the project is registered or pass an override.
  All-or-nothing (one bad item rejects the batch); re-running is dedup-safe.
- **External documents** (corpus with a different author): switch to *curated*
  mode — `--working-folder <docs-root> --tag <Label>` with a **user-confirmed**
  tag (don't invent it). Keeps doc provenance out of conversational memory.
- **Large input → author compaction-safe.** Don't hold the whole document in
  context and emit `content`+`summary` together — if you compact mid-authoring,
  the verbatim `content` can drift. Instead keep the text on disk and work in
  three stages: a small script slices the source into `chunks/<id>.txt` (no LLM);
  you write `chunks/<id>.ann.json` (summary + entities only); then merge them
  byte-exact with the shipped CLI — no LLM, no DB:
  ```bash
  k2g-manifest-assemble ./chunks -o ./ingest.manifest.json --tag <tag>
  k2g-ingest-manifest   ./ingest.manifest.json --remove-after
  ```
- Limits: `items ≤ 2000`, `Σcontent ≤ ~5MB` — over that, split into per-batch
  manifests chained via `prev_event_id`.

### Recommend a save when

Append a single line at the end of a response when:

- A design decision / agreement was reached ("let's go with that")
- A conclusion after a long discussion (≥3 turns + resolution)
- Hard-to-reproduce info surfaced (root cause / non-obvious fact)
- Intent / rationale ("why") that won't survive in code

> 💾 Worth saving with `mw save` — <one-line reason>

Don't suggest on every turn (cap 1–2 per session) and don't auto-call
`mweft_remember` without user consent.

### Hard prohibitions

- Do NOT call `mweft_remember` on bare "remember this" / "save". The
  `mw` prefix is required.
- Do NOT interpret "summarize this" as a save request — produce only
  your own summary.
````

## Why these heuristics

### Hybrid search first

MWeft's `mweft_search` mixes event and entity matches and returns
connection neighbors in one shot. Pure entity lookup is brittle when
naming varies (`PlanNode` vs `plan node` vs `plan_node`) — hybrid
crosses that via embedding similarity.

### Recency sweep before term-anchored search

Memory naturally drifts: a concept gets renamed, deprecated, or
absorbed into a successor. Term-anchored search anchors on the *old*
name and misses the latest version. A term-agnostic recency sweep
(`ORDER BY created_at DESC`) reveals successors, which you then verify
against the term search via `connections.similar` (rename bridge).

### Tags with reuse

`tag` is how saves are clustered into navigable buckets later.
Two different LLMs trying to save the same kind of conversation
shouldn't produce `work-auth` and `auth/work` and `authentication` —
that fragments the index. The `tag_tree` response field exists
specifically to enable reuse-or-create discipline.

## The `hint` and `reason` tags

The hint surface is what makes MWeft *navigable* without N extra tool
calls. Two layers:

| Layer | What it tells you |
|---|---|
| Per-hit `reason: list[str]` | *Why* this hit surfaced — semantic match, neighbor in graph, sibling in document, member of a cluster |
| Top-level `hint` | Cross-hit aggregates — threads (sequential chains), entities spanning multiple hits, shared context groups, shared categories |

The cardinal rule: **tags are categorical**. No score, no threshold.
Don't try to rank or filter by hint count — *act* on the presence of a
tag. If a hit has `similar-embedding`, that's a rename signal, follow it.

## Save discipline

The save model assumes the LLM does NER (extracts `entities`) at the
moment of saving and lets MWeft do the rest (vector, graph edges, group
membership, tag placement). The LLM has more context than the
storage layer, so the LLM is the right place to do extraction.

Two failure modes to avoid:

1. **Hallucinated entities.** If the conversation didn't establish a
   thing as a real entity (a person's name, a concrete component, a
   defined concept), don't invent one. Better to record fewer entities
   than to pollute the graph with fictional ones. MWeft's edit tool
   exists to recover from this, but prevention is cheaper.
2. **Wrong save trigger.** Don't auto-save after every response. Only
   save on explicit user signal (`mw save`). Suggest a save when a
   conversation has *produced something durable* — a decision, a root
   cause, an agreement — not after each task completion.

## Bulk / document ingestion

`mweft_remember` saves one conversational unit. The second write path is
**manifest ingestion** — for loading a whole file or folder where the LLM has
already produced the summaries and entities. MWeft makes no LLM calls during
the load: it trusts the manifest's summary/NER and only computes embeddings,
so a large document lands in the same group/tag structure as `mw save` at a
fraction of the cost.

Reach for it when you've split a document into semantic chunks and want each
chunk stored with its own summary + entities. For a single short note, stay
with `mweft_remember`. (This distribution has no server-side LLM, so the
summaries and entities come from you — there is no raw-corpus auto-summarize
path here.) The contract is just two things — the `*.manifest.json` schema and
the `k2g-ingest-manifest` CLI — so it works from any agent that can write a
file and run a shell. The full reference (schema fields, `--incremental` /
curated `--tag` flags, guards) ships as `manifest_ingestion_guide.md` with the
Manager.

## Gotchas

- **Domain casing.** The save domain comes from
  `K2G_USER_MEMORY_SAVE_DOMAIN`. The *value* is case-sensitive: `K2G` and
  `k2g` end up in different domain buckets. Pick one and stick with it.
- **SQLite boolean columns.** Some columns return as `"f"` / `"t"`
  strings, not `0`/`1`. `WHERE deprecated = 0` may silently drop rows —
  use `WHERE deprecated IN (0, 'f', 'false')` or omit the filter.
- **`mweft_sql_query` is read-only.** SELECT / WITH only. DDL and DML
  are blocked. Use it for exploration, not for mutation.
- **One in-progress task at a time.** If you're building todos for a
  multi-step session, keep one in-progress task and move on as they
  complete — this matches MWeft's recording model where each turn is a
  complete-able unit.
- **The `mw` prefix.** A bare "save this" / "remember" doesn't trigger
  save — the prefix `mw` (or `mweft` / `memoryweft`) is the explicit
  signal. This prevents auto-save from chatter.
