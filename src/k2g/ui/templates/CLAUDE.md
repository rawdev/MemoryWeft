## MWeft (MemoryWeft) — Memory System

You are connected to the MWeft MCP server. It is a system for storing and
retrieving important conversation content as long-term memory.

**Principles**:
- **Read**: use `mweft_search` / `mweft_entity_lookup` / `mweft_auto_tag_detail`.
- **Summarize clusters**: use `mweft_auto_tag_summarize` (leiden community summaries).
- **Write (single)**: use `mweft_remember`. No AI-only saves / no auto per-turn logging.
- **Write (documents / bulk)**: to ingest a file/folder *with summaries + NER
  attached*, use manifest ingestion — author a JSON manifest, then run
  `k2g-ingest-manifest <manifest>`. Procedure:
  `.claude/skills/manifest-ingestion/GUIDE.md` (Skill: `manifest-ingestion`).

### Search path

Follow these rules when answering user questions.

1. Check the current session and auto-memory first.

2. Prefer `mweft` search over `grep` / `glob` / `read` tool calls.

3. Do not scope the search yourself. Never pass `domain` or
   `search_targets` to narrow a query — MWeft searches every permitted
   target (resolved from env / access policy). Narrowing the search
   *input* silently drops data. If the user explicitly asks to restrict
   to one domain, filter the *returned results*, not the search call.

4. Default search sequence:
   - Start with `mweft_search(query="<core>", mode="hybrid", top_k=5)`.
     Hybrid search is semantic and crosses naming variants — prefer it
     for conceptual or ambiguous queries.
   - Use `mweft_entity_lookup(name)` for precise entity *or tag* enumeration
     (BP-92 lexical unification). substring (`LIKE`) match on `entities.name` and
     `groups.name` — try variants (camelCase and spaced, e.g. `PlanNode`
     and `plan node`) or fragmented names are missed. Response has both
     `matches` (entity, `kind="entity"`) and `tag_matches` (tag,
     `kind="tag"`). SQL equivalents: `entities`/`groups WHERE LOWER(name)
     LIKE '%x%'`, the entity side JOINs `participated_in` -> `events`, the
     tag side JOINs `event_member_of` -> `events`.
   - Use `mweft_sql_query` for exact counts and table facts
     ("how many X", "does table Y have rows"). Do not add
     `WHERE domain = ...` unless the user asked to restrict.
   - Sort `mweft_search` / `mweft_sql_query` results by `created_at`
     (or `timestamp`), newest first; on conflict prefer the newest.

5. Recency questions ("latest / most recent"): the newest
   content is often filed under a *renamed* concept, so a term search
   anchored on the question's noun will miss it. Before answering:
   - Run a term-agnostic recency sweep with `mweft_sql_query`:
     `events ORDER BY created_at DESC` and
     `SELECT domain, MAX(created_at) ... GROUP BY domain`.
   - Reconcile it with the term search. If the term search's newest hit
     is older than the sweep's, a successor concept likely exists —
     chase it through `connections.similar` / `hint.entities`.

6. Read the `hint` connection map and each hit's `reason` tags — do not
   synthesize only from flat `hits`:
   - `reason` — per-hit fixed tags = *why* the hit surfaced. Categorical,
     no relevance scores — judge by tag, not by number.
     - `similar-embedding` — semantically near a query concept; a likely
       **rename / synonym**. For "latest / most recent / related"
       questions the answer may live under that renamed concept — follow it.
     - `contents-chain` — adjacent document chunk.
     - `closed-jaccard` / `co-occurrence` / `shared-context` /
       `shared-category` / `spanning-entity` — topic / graph neighbors.
   - `hint.context_groups` / `categories` — auto-tags / user tags shared
     across hits; drill in via `mweft_auto_tag_detail`. (The `context_groups`
     field name is the internal identifier — the same thing as the
     user-facing "auto-tag".)
   - `connections.sequential` — for procedure / multi-hop / "whole procedure"
     questions, traverse a prev/next chain to rebuild cut document context.
   - `connections.similar` / `connected` / `profile` — entity detail to follow.
   - `hint.available_channels` — which `reason` tags appear in the result.

7. If `mw` / `mweft` / `memoryweft` prefix appears, use MWeft search /
   lookup / auto_tag_detail explicitly.

### Community / auto-tag summaries (event / entity clusters)

Requests like "summarize the mw event clusters" / "community summary" /
"auto-tag summary" / "show me the whole cluster structure" = a **leiden
community structure summary**. This is a **read/summarize, not a
`mweft_remember` (save)** — neither the `mw`-prefix save rule nor the
"summarize this → do not save" clause applies to community summaries.
(auto-tag = the public name for a system cluster.)

- **Full summary (default entry point)**: `mweft_auto_tag_summarize(kind, domain)`
  — returns, in one call, the Leiden settings the Manager saved (resolution/seed)
  + the latest run's cluster distribution (count / size stats / top members of
  the largest clusters).
- `kind` mapping: **"event clusters" → `kind="event"`**, **"entity clusters" →
  `kind="entity"`**. If unclear, call both (`event`+`entity`) and present a comparison.
- `domain`: **community runs are stored per domain**, so **pass the active
  project's domain** (default save domain = env `K2G_USER_MEMORY_SAVE_DOMAIN`).
  `domain=None` looks for a cross-domain (NULL) run, which usually doesn't exist
  and returns empty — unlike the search "do not specify domain" principle
  (#3 above), **for summaries you must specify the domain**.
- Deeper: `mweft_auto_tag_list(kind, domain)` (lists each cluster's size + top
  members) → `mweft_auto_tag_members(auto_tag_id, kind)` (all members of one
  cluster) → `mweft_auto_tag_of(node_id, kind)` (which cluster a given
  entity/event belongs to).
- If the result is `"no completed ... run"`, the community hasn't been computed
  yet — advise that it recomputes automatically after ingest (BP-94) or that a
  Manager recompute is needed.
- **`explore_hints` discovery signal (BP-96 §6)**: the summary response also
  carries `explore_worth` (0–1) + `explore_hints` (Top-K) + `explore_axes`.
  **Only when `explore_worth > 0.6`**, append *one line* at the end of the
  summary conveying the top hint and ask whether to look deeper (e.g. "note:
  cluster 0 is only 62% explained by its label — analyze further?"). **Cap 1–2
  times per session**, opt-in — only on the user's consent, drill in via the
  hint's `suggested_view` (node/scope/resolution). If `explore_worth ≤ 0.6` or
  there are no hints, **say nothing** (no spam).

### Gotchas

In SQLite, `entities.deprecated` may return as `"f"` / `"t"` strings —
`= 0` can silently drop rows. Omit the filter or compare against multiple
forms (`IN (0, 'f', 'false')`).

If auto-search returns hit > 0, prepend a single line at the top of the
session's first response:
`📎 Referenced N memories: [domain1, ...]`. If hit = 0, show nothing.

### Save path

On an explicit `mw` / `mweft` / `memoryweft` utterance, call
`mweft_remember` immediately:

```python
mweft_remember(
    summary="<1-2 line summary>",
    entities=[{"name": "...", "type": "person|org|concept|..."}, ...],
    tag="<topic tag path>",  # decide every save — see below
    working_folder=None,   # env K2G_USER_MEMORY_SAVE_GROUP applies automatically
    content="<original text>",  # ⚠️ ALWAYS LAST — long content can truncate the arg right after it
)
```

**Tag** — a required step of every save, like `entities`:

- Pick a short topic tag for this conversation. A slash-separated path
  is allowed — `"work"`, or `"work/auth"` for a nested tag.
- Match it against the existing tags: the previous `mweft_remember`
  response's `tag_tree` lists every sub-tag path under the working_folder
  root — reuse the closest fit. Create a new tag (or sub-tag) **only**
  when nothing fits. This keeps tags from fragmenting into near-duplicates.
- First save of a session (no `tag_tree` seen yet): propose your best
  tag from the conversation topic. The response confirms the resolved
  `tag` and the full `tag_tree` for later saves.

Surface the response's `saved_entities` and `tag` to the user:

> Saved to tag **work/auth**. Recorded as Alice (person, new),
> Bob (person, existing). Tell me if an entity is a hallucination or
> the tag is off.

If the user flags a hallucination:
`mweft_remember_edit(event_id=<response above>, remove_entities=["Bob"])`

### Recommend path

When any of the conditions below hold, append a single line at the end
of the response:
> 💾 Recommend saving this conversation with `mw save` — <one-line reason>

Conditions (OR):
- Design decision / agreement reached (e.g. "let's go with that", "confirmed")
- Conclusion after a long discussion (≥3 turns + resolution)
- Hard-to-reproduce information surfaced (root cause / non-obvious fact)
- Intent / rationale ("why") that won't survive in code or files

**Never**:
- Auto-call `mweft_remember` after the suggestion without user response
- Suggest on every turn (cap at 1–2 times per session)
- Suggest on trivial task completion ("created the file")

### Hard prohibitions

- Do **not** call `mweft_remember` on bare "remember this" / "save" /
  "remember" utterances (the `mw` prefix is required).
- Do **not** interpret "summarize this" as a save request (emit only
  Claude's own summary).
- The AI must **not** write to MWeft automatically without the user's
  consent.
