#!/usr/bin/env python3
"""MWeft portable zip builder.

Assembles a per-platform portable zip. Layout:

    mweft/
      start-mweft.bat / start-mweft.command   # launcher (copied from this folder)
      bin/uv(.exe)                             # per-platform uv static binary (downloaded)
      models/bge-m3-onnx/                      # (optional) bundled ONNX model
      wheels/                                  # (offline) pre-downloaded wheels
      VERSION  README.txt

Modes:
  online  — bin/uv + launcher + (optional) model. uv downloads Python+deps at
            runtime. Small zip. Requires a PyPI release (or --wheels).
  offline — additionally fills wheels/ via `uv pip download` (runs on the
            current platform). Bundling a standalone Python is a follow-up
            (the launcher needs a --python override).

run (on each OS's CI runner, for that platform):
    python packaging/portable/build_portable.py --platform win-x64   --version 0.1.0
    python packaging/portable/build_portable.py --platform mac-arm64 --version 0.1.0 \
        --model-dir models/bge-m3-onnx --mode offline
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

# platform -> (uv release asset, launcher file, uv binary name)
UV_ASSET = {
    "win-x64": ("uv-x86_64-pc-windows-msvc.zip", "start-mweft.bat", "uv.exe"),
    "mac-arm64": ("uv-aarch64-apple-darwin.tar.gz", "start-mweft.command", "uv"),
    "mac-x64": ("uv-x86_64-apple-darwin.tar.gz", "start-mweft.command", "uv"),
}
UV_BASE = "https://github.com/astral-sh/uv/releases/latest/download"


def fetch_uv(platform: str, bin_dir: Path) -> None:
    asset, _, uv_name = UV_ASSET[platform]
    url = f"{UV_BASE}/{asset}"
    print(f"[uv] download {url}")
    data = urllib.request.urlopen(url).read()  # noqa: S310
    bin_dir.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.endswith(uv_name):
                    (bin_dir / uv_name).write_bytes(z.read(n))
    else:  # tar.gz
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            for m in t.getmembers():
                if m.name.endswith(uv_name):
                    src = t.extractfile(m)
                    assert src is not None
                    (bin_dir / uv_name).write_bytes(src.read())
    if not uv_name.endswith(".exe"):
        (bin_dir / uv_name).chmod(0o755)
    print(f"[uv] -> {bin_dir / uv_name}")


def download_wheels(wheels: Path, pkg: str, find_links: str | None = None) -> None:
    # uv has no `pip download` subcommand, so use the BUILD host's pip to
    # populate wheels/. The bundled uv is only used at *runtime* by the launcher
    # (`uv pip install --no-index --find-links wheels`).
    wheels.mkdir(parents=True, exist_ok=True)
    # Include setuptools + wheel so the offline `uv pip install --no-index` can
    # build any sdist-only deps (e.g. proxy_tools, a pywebview dep on Windows).
    cmd = [sys.executable, "-m", "pip", "download", pkg, "setuptools", "wheel",
           "-d", str(wheels)]
    if find_links:
        # Extra wheel source — e.g. a locally built `mweft` wheel before it is
        # published to PyPI. The remaining deps come from PyPI as usual.
        cmd += ["--find-links", str(Path(find_links).resolve())]
    print(f"[wheels] pip download {pkg} -> {wheels}"
          + (f" (+find-links {find_links})" if find_links else ""))
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="MWeft portable zip builder")
    ap.add_argument("--platform", required=True, choices=list(UV_ASSET))
    ap.add_argument("--version", required=True)
    ap.add_argument("--mode", choices=["online", "offline"], default="online")
    ap.add_argument("--model-dir", default=None, help="ONNX model directory to bundle")
    ap.add_argument("--pkg", default="mweft[embed-onnx,manager]", help="install/download package spec")
    ap.add_argument("--wheels-from", default=None,
                    help="offline: extra --find-links dir (e.g. dist/ with a locally built wheel)")
    ap.add_argument("--out", default="dist", help="zip output directory")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    _, launcher, uv_name = UV_ASSET[args.platform]
    stage = (REPO / args.out / f"mweft-{args.platform}-{args.version}").resolve()
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # 1) launcher + README (English default + bundled Korean readme_kr.txt)
    shutil.copy2(HERE / launcher, stage / launcher)
    shutil.copy2(HERE / "README.txt", stage / "README.txt")
    if (HERE / "readme_kr.txt").is_file():
        shutil.copy2(HERE / "readme_kr.txt", stage / "readme_kr.txt")
    (stage / "VERSION").write_text(args.version + "\n", encoding="utf-8")

    # 2) uv binary
    fetch_uv(args.platform, stage / "bin")

    # 3) (optional) bundle the ONNX model
    if args.model_dir:
        md = Path(args.model_dir).resolve()
        if not (md / "model.onnx").is_file():
            sys.exit(f"error: {md}/model.onnx missing — run mweft_export_bge_onnx.py first")
        shutil.copytree(md, stage / "models" / "bge-m3-onnx")
        print(f"[model] bundled {md}")

    # 4) (offline) pre-download wheels — for the current platform
    if args.mode == "offline":
        download_wheels(stage / "wheels", args.pkg, args.wheels_from)
        print("[offline] bundling a standalone Python is a follow-up (launcher --python override needed)")

    # 5) zip  (do not use with_suffix — it mistakes the version's ".0" for a suffix)
    out_zip = stage.parent / f"{stage.name}.zip"
    if out_zip.exists():
        out_zip.unlink()
    print(f"[zip] {out_zip}")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in stage.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(stage.parent))
    size = out_zip.stat().st_size / 1e6
    print(f"\n=== {out_zip.name} ({size:.0f} MB) — mode={args.mode} ===")


if __name__ == "__main__":
    main()
