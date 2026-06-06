---
name: manifest-ingestion
description: >-
  Ingest documents (file or folder) into MWeft memory as AI-summarized chunks
  via a JSON manifest + the k2g-ingest-manifest CLI — no LLM call inside K2G.
  Use when the user asks to save/ingest a file or folder's content as memory
  with your summaries and entities (not a single short note, not raw bulk
  corpus). Triggers: "ingest this manifest", "organize and save this
  file/folder into memory", "ingest this doc as memory".
---

# Manifest Ingestion (MWeft BP-95) — thin wrapper

This Skill is **ergonomics only**. The canonical, AI-agnostic procedure lives in
`.claude/skills/manifest-ingestion/GUIDE.md` (installed alongside this file) —
**read it and follow it**. Do not duplicate the workflow here (single source of
truth → avoids drift).

## What this Skill adds

`file=<path>` / `folder=<path>` convenience: given a target, produce a
`*.manifest.json` and run the CLI. Everything else (schema, guards, CLI flags)
is defined in the guide.

## Steps

1. **Load the guide**: read `.claude/skills/manifest-ingestion/GUIDE.md`
   (the contract: schema §3, CLI §4, guards §5, workflow §6).
2. **Judge fit** (guide §1): manifest is for *AI-summarized* ingestion. A single
   short note → use the `mweft_remember` MCP tool instead. Raw bulk corpus →
   `k2g-build`.
2.5. **Pre-check on re-ingest** (saves LLM cost): if re-ingesting an existing
   source file, run `k2g-manifest-check --source <path> --file-path <id>` BEFORE
   chunking. If it prints `unchanged` (exit 3), stop — the source is already
   ingested; do not chunk or write a manifest. If `changed` (exit 0), proceed.
3. **Build the manifest** to a temp path (e.g. `./.k2g_tmp/ingest.manifest.json`):
   - file mode: split one file into semantic chunks (`source: "chunk_order"`).
   - folder mode: walk files; chain file boundaries via `prev_event_id`
     (`source: "file_name"`/`"folder_name"`).
   - For each chunk write `{content, summary, entities}` — content = original
     text verbatim, summary = your summary, entities = real entities only.
   - Write incrementally (write first chunk → append) to avoid output-token
     truncation. Respect per-item (`content ≤ 50000`, `summary ≤ 500`) and total
     (`items ≤ 2000`, `Σcontent ≤ ~5MB`) caps.
   - Set `"complete": true` only when fully written.
4. **Run the CLI** (the user's ingestion request is the consent):
   ```bash
   k2g-ingest-manifest ./.k2g_tmp/ingest.manifest.json --remove-after
   # re-ingesting a source file? add --source for robust change detection:
   k2g-ingest-manifest ./.k2g_tmp/ingest.manifest.json --source <orig> --remove-after
   ```
5. **Verify** the `[manifest]` success line reporting `N/M events`; report counts to the user.

## Notes

- `domain` / `working_folder` are server-enforced via env — do **not** put a
  `domain` field in the manifest. Omit `working_folder` to use the env default.
- All-or-nothing: any invalid item rejects the whole batch (no partial load).
- Group/tag/forced-tag/community behavior matches `mweft_remember` — manifest
  saves land in the same structure as `mw save`.
- **Document builds (curated, guide §4.1)**: pass `--working-folder` + `--tag`.
  This skips autotag + the AI session's forced tags and attaches the `--tag` as
  `forced` (human-confirmed). **Ask the user for the tag** — never invent it.
