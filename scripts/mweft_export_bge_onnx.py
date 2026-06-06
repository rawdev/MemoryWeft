#!/usr/bin/env python3
"""BGE-M3 -> ONNX export — produces the embedding model for portable builds.

Builds the layout that ``OnnxEmbeddingClient`` (src/k2g/embedding/client.py)
consumes:

    <out>/
      model.onnx          # last_hidden_state output (CLS pooling done by the client)
      tokenizer.json      # for the tokenizers library

This is a *build-time* tool — it needs optimum + torch + onnx (not at runtime).
The output is bundled into the portable zip as ``models/bge-m3-onnx``, or
downloaded on first run.

Size / fidelity:
- fp32 (default): ~2.2GB. Numerically equivalent to torch BGE-M3 — safest.
- --quantize (dynamic int8): ~600MB. Slight quality loss possible. A fresh
  portable install builds its DB with this ONNX model from the start, so there
  is no cross-compat with a DB built by a different provider — only intra-ONNX
  consistency (build == query model) matters, and that is automatic. So
  quantize is practical for new distributions.

run:
    python scripts/mweft_export_bge_onnx.py --out models/bge-m3-onnx
    python scripts/mweft_export_bge_onnx.py --out models/bge-m3-onnx --quantize
    python scripts/mweft_export_bge_onnx.py --model BAAI/bge-m3 --verify
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def export(model: str, out: Path, opset: int) -> None:
    try:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from transformers import AutoTokenizer
    except ImportError:
        _die(
            "optimum + transformers + torch are required (build-time): "
            "pip install 'optimum[onnxruntime]' transformers torch"
        )
    print(f"[export] {model} -> ONNX (opset={opset}) ...")
    m = ORTModelForFeatureExtraction.from_pretrained(model, export=True)
    out.mkdir(parents=True, exist_ok=True)
    m.save_pretrained(out)
    AutoTokenizer.from_pretrained(model).save_pretrained(out)
    # optimum saves as model.onnx. Confirm tokenizer.json exists.
    if not (out / "model.onnx").is_file():
        # Some versions use a different name -> normalize the first .onnx to model.onnx
        onnx_files = list(out.glob("*.onnx"))
        if not onnx_files:
            _die("no .onnx file after export.")
        if onnx_files[0].name != "model.onnx":
            shutil.copy2(onnx_files[0], out / "model.onnx")
    if not (out / "tokenizer.json").is_file():
        _die("tokenizer.json missing (slow tokenizer? --use-fast may be needed).")
    print(f"[export] done -> {out}")


def quantize(out: Path) -> None:
    try:
        from optimum.onnxruntime import ORTQuantizer
        from optimum.onnxruntime.configuration import AutoQuantizationConfig
    except ImportError:
        _die("quantize requires optimum[onnxruntime].")
    print("[quantize] dynamic int8 (avx512_vnni, per-channel) ...")
    q = ORTQuantizer.from_pretrained(out, file_name="model.onnx")
    qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=True)
    q.quantize(save_dir=out, quantization_config=qconfig)
    # Replace model.onnx with the quantized result (model_quantized.onnx)
    qfile = out / "model_quantized.onnx"
    if qfile.is_file():
        (out / "model.onnx").unlink(missing_ok=True)
        qfile.rename(out / "model.onnx")
    # The int8 model.onnx has self-contained weights, so the fp32 external-data
    # (model.onnx_data) is orphaned. Remove it before bundling (saves ~2.2GB).
    for orphan in ("model.onnx_data", "model_quantized.onnx_data"):
        f = out / orphan
        if f.is_file():
            f.unlink()
            print(f"[quantize] removed orphan external-data: {orphan}")
    print("[quantize] done — model.onnx replaced with int8 (self-contained)")


def verify(out: Path, max_length: int = 512) -> None:
    """Run one inference through the same path as OnnxEmbeddingClient to check dim."""
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError:
        _die("verify requires onnxruntime + tokenizers.")
    sess = ort.InferenceSession(
        str(out / "model.onnx"), providers=["CPUExecutionProvider"],
    )
    tok = Tokenizer.from_file(str(out / "tokenizer.json"))
    tok.enable_truncation(max_length=max_length)
    names = {i.name for i in sess.get_inputs()}
    enc = tok.encode("Hello. This is a sample sentence for MWeft embedding verification.")
    ids = np.array([enc.ids], dtype=np.int64)
    mask = np.array([enc.attention_mask], dtype=np.int64)
    feeds = {}
    if "input_ids" in names:
        feeds["input_ids"] = ids
    if "attention_mask" in names:
        feeds["attention_mask"] = mask
    if "token_type_ids" in names:
        feeds["token_type_ids"] = np.zeros_like(ids)
    out_arr = sess.run([sess.get_outputs()[0].name], feeds)[0]
    emb = out_arr[:, 0] if out_arr.ndim == 3 else out_arr
    print(f"[verify] inputs={sorted(names)} output_shape={out_arr.shape} dim={emb.shape[1]}")
    if emb.shape[1] != 1024:
        print(f"[verify] WARNING: BGE-M3 dim should be 1024 — got {emb.shape[1]}")
    else:
        print("[verify] OK — dim 1024 (set EMBEDDING_DIM=1024)")


def _dir_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser(description="BGE-M3 ONNX export for portable MWeft")
    ap.add_argument("--model", default="BAAI/bge-m3", help="HF model id or local path")
    ap.add_argument("--out", default="models/bge-m3-onnx", help="output directory")
    ap.add_argument("--opset", type=int, default=17)
    ap.add_argument("--quantize", action="store_true", help="dynamic int8 (smaller)")
    ap.add_argument("--verify", action="store_true", help="run one inference after export")
    ap.add_argument("--skip-export", action="store_true", help="skip export; quantize/verify only")
    args = ap.parse_args()

    # Avoid UnicodeEncodeError when printing non-ASCII (em dash etc.) on a
    # Windows cp949 console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    out = Path(args.out).resolve()
    if not args.skip_export:
        export(args.model, out, args.opset)
    if args.quantize:
        quantize(out)
    if args.verify:
        verify(out)

    print(f"\n=== {out} ({_dir_size_mb(out):.0f} MB) ===")
    for f in sorted(out.iterdir()):
        if f.is_file():
            print(f"  {f.name:28s} {f.stat().st_size/1e6:8.1f} MB")
    print("\nBundle into the portable zip: models/bge-m3-onnx/  + set EMBEDDING_ONNX_PATH")


if __name__ == "__main__":
    main()
