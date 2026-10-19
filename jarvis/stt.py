"""Offline speech-to-text for voice memos: `python -m jarvis stt <file>` and `python -m jarvis stt-setup`.

faster-whisper (CTranslate2, CPU, int8) runs in its own venv, stt/.venv, so its pinned packages never
touch the Python that runs Jarvis. Nothing leaves the machine and nothing is downloaded at run time;
stt-setup is the one step that needs the network (packages + the pinned model, ~1.6 GB).

Exit codes of `stt` (one `stt: error: <reason>` line on stderr for every failure):
  0 ok · 2 bad arguments / file missing · 3 not decodable audio · 4 model or venv missing or broken ·
  5 transcription failed or the transcriber crashed · 6 longer than max_minutes (refused before loading)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config, JarvisError

KNOWN_CODES = (0, 2, 3, 4, 5, 6)


def venv_python(cfg: Config) -> Path:
    v = cfg.stt_dir / ".venv"
    return v / "Scripts" / "python.exe" if os.name == "nt" else v / "bin" / "python"


def cmd_stt(cfg: Config, a) -> int:
    py = venv_python(cfg)
    if not py.exists():
        print(f"stt: error: no stt venv at {py} - run: python -m jarvis stt-setup", file=sys.stderr)
        return 4
    s = cfg.stt
    argv = [str(py), "-I", str(cfg.stt_dir / "transcribe.py"), a.path,
            "--model", a.model or s["model"],
            "--language", a.language or s["language"],
            "--max-minutes", str(a.max_minutes if a.max_minutes is not None else s["max_minutes"]),
            "--models-dir", str(cfg.stt_models)]
    cands = a.candidates if a.candidates is not None else ",".join(s.get("candidates") or [])
    if cands:
        argv += ["--candidates", cands]
    sys.stdout.flush()
    rc = subprocess.run(argv).returncode   # not captured: the child writes straight to our stdout/stderr
    if rc not in KNOWN_CODES:
        print(f"stt: error: the transcriber exited with code {rc} (a broken venv or a crash; rerun stt-setup)", file=sys.stderr)
        return 5
    return rc


def _run(argv: list[str], what: str) -> None:
    print(f"$ {' '.join(argv)}", flush=True)
    r = subprocess.run(argv)
    if r.returncode != 0:
        raise JarvisError(f"{what} failed (exit {r.returncode})")


def cmd_stt_setup(cfg: Config, a) -> int:
    venv = cfg.stt_dir / ".venv"
    py = venv_python(cfg)
    uv = shutil.which("uv")
    if not py.exists():
        if uv:
            _run([uv, "venv", str(venv), "--python", sys.executable], "creating the venv")
        else:
            _run([sys.executable, "-m", "venv", str(venv)], "creating the venv")
    req = str(cfg.stt_dir / "requirements.txt")
    if uv:
        _run([uv, "pip", "install", "--python", str(py), "-r", req], "installing the packages")
    else:
        _run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", req], "installing the packages")
    models = a.model or [cfg.stt["model"]]
    if not a.skip_model:
        for m in models:
            if (cfg.stt_models / m / "model.bin").exists():
                print(f"model {m} already present in {cfg.stt_models / m}")
                continue
            _run([str(py), "-I", str(cfg.stt_dir / "fetch_model.py"), m, "--models-dir", str(cfg.stt_models)],
                 f"fetching model {m}")
    (cfg.stt_dir / "tmp").mkdir(exist_ok=True)
    print("stt ready" + (" (no model fetched: --skip-model)" if a.skip_model else ""))
    return 0
