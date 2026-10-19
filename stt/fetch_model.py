#!/usr/bin/env python3
r"""Download one faster-whisper model into <models-dir>/<name> (once per model; the only network step).

  stt/.venv/<bin>/python -I fetch_model.py large-v3-turbo [--models-dir DIR]

After this the model loads with no network at all: transcribe.py never downloads. The default model is
PINNED to the Hugging Face commit that was benchmarked, and its model.bin is checked against the sha256
Hugging Face publishes for that file - a fresh install gets exactly those weights or fails loudly (a
mismatch is moved aside as model.bin.rejected, so transcribe.py reports the model missing instead of
silently using it). Other models are fetched by name, unpinned.

Why a download and not a file in the repo: model.bin is ~1.6 GB, far over GitHub's 100 MB file limit, and
Hugging Face is already the canonical, versioned host - pin + checksum gives the same guarantee a release
asset would, without re-hosting someone else's weights.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
# name -> (Hugging Face commit, sha256 of model.bin), as downloaded and benchmarked
PINS = {
    "large-v3-turbo": ("0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
                       "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    from faster_whisper.utils import _MODELS, download_model

    ap = argparse.ArgumentParser(prog="fetch_model.py")
    ap.add_argument("name", choices=sorted(_MODELS))
    ap.add_argument("--models-dir", default=os.path.join(HERE, "models"))
    a = ap.parse_args()
    revision, want = PINS.get(a.name, (None, None))
    out = os.path.join(a.models_dir, a.name)
    os.makedirs(out, exist_ok=True)
    print(f"{a.name}: downloading{' pinned commit ' + revision[:10] if revision else ''} into {out} ...", flush=True)
    path = download_model(a.name, output_dir=out, revision=revision)
    if want:
        got = sha256(os.path.join(path, "model.bin"))
        if got != want:
            os.replace(os.path.join(path, "model.bin"), os.path.join(path, "model.bin.rejected"))
            print(f"{a.name}: model.bin sha256 {got} does not match the pin {want}; moved aside as "
                  "model.bin.rejected", file=sys.stderr)
            return 3
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)
    print(f"{a.name}: {path} ({size / 1e6:.0f} MB{', pinned and sha256-verified' if want else ', NOT pinned'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
