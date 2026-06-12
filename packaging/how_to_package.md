# How to build the portable distribution

This guide builds the **portable zip** of MWeft — an "unzip and run" bundle
that installs nothing on the user's system. It carries a launcher, a static
`uv` binary, and (optionally) the bundled ONNX embedding model; on first run
the launcher creates a self-contained Python runtime inside the folder.

Run everything from the repository root. Two scripts are involved:

| Script | Role |
|---|---|
| `scripts/mweft_export_bge_onnx.py` | Build-time: export BGE-M3 to ONNX |
| `packaging/portable/build_portable.py` | Assemble the per-platform zip |

> **Cross-platform note:** build on the target OS. There is no cross
> compilation — produce the Windows zip on Windows, the macOS zip on macOS
> (a CI matrix is the usual approach).

## Step 1 — Export the ONNX model (once)

The portable bundle uses local ONNX embeddings (no API key, no PyTorch at
runtime). PyTorch is needed **only here**, at build time.

```bash
pip install 'optimum[onnxruntime]' transformers torch
python scripts/mweft_export_bge_onnx.py --out models/bge-m3-onnx --quantize --verify
```

- `--quantize` — dynamic int8, ~600 MB (omit for fp32, ~2.2 GB). Quantize is
  recommended for distribution.
- `--verify` — runs one inference and checks the output dim is 1024.

Output: `models/bge-m3-onnx/{model.onnx, tokenizer.json}`.

## Step 2 — Build the portable zip (per platform)

```bash
# Windows x64
python packaging/portable/build_portable.py --platform win-x64   --version 0.1.0 --model-dir models/bge-m3-onnx

# macOS Apple Silicon
python packaging/portable/build_portable.py --platform mac-arm64 --version 0.1.0 --model-dir models/bge-m3-onnx

# macOS Intel
python packaging/portable/build_portable.py --platform mac-x64   --version 0.1.0 --model-dir models/bge-m3-onnx
```

Output: `dist/mweft-<platform>-<version>.zip`.

The builder downloads the `uv` static binary from GitHub
(`astral-sh/uv`) automatically.

### Options

| Flag | Meaning |
|---|---|
| `--platform` (required) | `win-x64` \| `mac-arm64` \| `mac-x64` |
| `--version` (required) | e.g. `0.1.0` — written to `VERSION`, used by the launcher's upgrade detection |
| `--model-dir` | ONNX model directory to bundle (omit to ship without a model) |
| `--mode` | `online` (default) \| `offline` |
| `--pkg` | package spec to install/download (default `mweft[embed-onnx,manager]`) |
| `--out` | output directory (default `dist`) |

## online vs offline

- **online** (default): small zip. At first run, `uv` installs
  `mweft[embed-onnx,manager]` **from PyPI**. Requires the `mweft` package to
  be published to PyPI — until then, online mode will not install.
- **offline** (`--mode offline`): pre-downloads wheels into `wheels/` via
  `uv pip download`, so no PyPI access is needed at install time. Wheels are
  fetched for the **current build platform only**, so build on the same OS you
  target.

Until `mweft` is on PyPI, prefer offline:

```bash
python packaging/portable/build_portable.py --platform win-x64 --version 0.1.0 \
    --model-dir models/bge-m3-onnx --mode offline
```

## What's in the zip

```
mweft/
  start-mweft.bat / start-mweft.command   # launcher (double-click)
  bin/uv(.exe)                            # static uv binary
  models/bge-m3-onnx/                     # bundled ONNX model (if --model-dir)
  asset/mweft_sample/k2g_all_in_one.db    # first-run sample DB (if present at build)
  wheels/                                 # pre-downloaded wheels (offline mode)
  VERSION  README.txt  readme_kr.txt
```

On first run the launcher:
1. Builds a self-contained venv under `runtime/` (Python 3.11 via `uv`).
2. Installs `mweft[embed-onnx,manager]` (from `wheels/` offline, else PyPI).
3. Launches the native Manager desktop app (`mweft-app`), which manages
   projects and registers the MCP server into your AI clients (with
   `K2G_MCP_LAZY_INIT=true` so they don't time out on first connect).

> **Windows:** the native Manager window needs the WebView2 runtime
> (preinstalled on Windows 11; on Windows 10 install it from
> <https://developer.microsoft.com/microsoft-edge/webview2/>).

Data and settings live in the bundle's `data/` folder; deleting the whole
folder uninstalls cleanly.

## Releasing (GitHub Releases — do NOT commit the zip)

Build artifacts are **not** stored in git. `dist/` and `models/` are
`.gitignore`d on purpose: a multi-hundred-MB zip (or the ONNX model) would
bloat the repo permanently — git keeps blobs in history forever, and every
clone would re-download them. Distribute them as **GitHub Release assets**
instead, where users download from the Releases page:

```
https://github.com/<owner>/<repo>/releases/download/v0.1.0/mweft-win-x64-0.1.0.zip
```

### Automated (recommended)

`.github/workflows/release.yml` does the whole thing on a version tag:

```bash
# keep pyproject `version` in sync with the tag, then:
git tag v0.1.0
git push origin v0.1.0
```

It exports the ONNX model once, then builds the Windows / macOS-arm64 /
macOS-x64 zips (each on its own runner) and uploads them to the Release for
that tag. Because `mweft` is not on PyPI yet, the build uses **offline** mode:
it builds the wheel locally and passes `--wheels-from dist` so the bundle is
self-contained.

### First-run sample DB (once)

The bundle seeds an explorable sample DB on first launch (domain `sample_work`)
instead of a blank "create a database" prompt. The sample (`k2g_all_in_one.db`,
~16 MB) is **not committed** — it is a `*.db`, gitignored like the model, and
publishing real sample memories into source history would bloat it permanently.
Instead the release workflow fetches it from a pinned **`sample-data`** release
and `build_portable` bundles it (it auto-detects `asset/mweft_sample/`).

Set it up once (re-upload only when the sample changes):

```bash
# create/update a non-version release that just holds the sample DB asset
gh release create sample-data asset/mweft_sample/k2g_all_in_one.db \
    --title "First-run sample data" --notes "Bundled by release.yml" \
    --prerelease   # or: gh release upload sample-data k2g_all_in_one.db --clobber
```

The fetch is best-effort: if the `sample-data` release/asset is missing, the
build still succeeds and just ships without a sample (fresh installs then open
the blank create-DB flow). For a **local** build, drop the file at
`asset/mweft_sample/k2g_all_in_one.db` and `build_portable` picks it up.

### Manual offline build (before PyPI)

To reproduce a runner locally, build the wheel first and point the portable
builder at it:

```bash
python -m build --wheel                      # -> dist/mweft-<ver>-py3-none-any.whl
python packaging/portable/build_portable.py --platform win-x64 --version 0.1.0 \
    --model-dir models/bge-m3-onnx --mode offline --wheels-from dist
```

Once `mweft` is published to PyPI, plain `--mode online` (no `--wheels-from`)
produces a much smaller zip that fetches deps at first run.
