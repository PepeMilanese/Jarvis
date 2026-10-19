#!/usr/bin/env python3
r"""Transcribe one audio file offline with faster-whisper (CPU, int8). Run by `python -m jarvis stt`.

  stt/.venv/<bin>/python -I transcribe.py <file> [--model large-v3-turbo] [--language auto|en|...]
                                          [--candidates en,de] [--max-minutes 10] [--models-dir DIR]

stdout: the transcript as plain text, then ONE final line
        [stt: model=<m> lang=<code> ... dur=<s>s wall=<s>s load=<s>s words=<n>]
        lang=en(forced)      the language was given, not detected. Forced, Whisper TRANSLATES other speech
                             into it, fluently and without a word of warning - so every 30 s window is still
                             asked what Whisper itself hears, and a window it confidently hears as another
                             language is appended as heard=<code>: "this may be a translation".
        lang=de p=0.97       auto-detected (afresh for every 30 s window, so a memo that switches languages
                             reads lang=de+en), restricted to --candidates when given; whisper=<code> = what
                             Whisper itself would have picked when the restriction overruled it.
        words=0 and no text above it = no speech heard.
stderr: on failure, ONE line `stt: error: <reason>` and a non-zero exit:
        2 = bad arguments / file missing or empty, 3 = audio could not be decoded,
        4 = model or venv missing or unloadable, 5 = transcription itself failed,
        6 = longer than --max-minutes (0 = no limit), refused before the model loads.

Never downloads anything: the model must already be in <models-dir>/<name> (fetch_model.py puts it there).
Temp files go to tmp/<pid>/ and are removed on exit; folders left by killed runs are cleared after a day.

Decoder settings, each one a finding from the benchmark (see stt/README.md):
- a fresh language decision per 30 s window (multilingual) - one decision per file lost or translated the
  other language of a memo that switched;
- no conditioning on the previous window's text - across a language switch it produced word salad;
- NO initial prompt and NO hotwords - a glossary fixed some names but, on one clip in five, made Whisper
  append a plausible instruction nobody said. A dispatcher that acts on the transcript at once must
  never receive an invented task; a misspelt name it can read for meaning.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = "large-v3-turbo"
HEARD_MIN_P = 0.5     # a forced-language window counts as "heard as X" from this probability

for _s in (sys.stdout, sys.stderr):   # -I ignores PYTHONIOENCODING; non-Latin text must survive the pipe
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def fail(code: int, reason: str) -> int:
    print(f"stt: error: {' '.join(str(reason).split())}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="transcribe.py")
    ap.add_argument("path")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="folder name under the models dir (default %(default)s)")
    ap.add_argument("--language", default="auto", help="ISO code such as en, or auto (default %(default)s)")
    ap.add_argument("--candidates", default="", help="languages auto-detection may choose from, comma-separated (default: all)")
    ap.add_argument("--max-minutes", type=float, default=10.0, help="refuse longer audio with exit 6; 0 = no limit")
    ap.add_argument("--models-dir", default=os.path.join(HERE, "models"))
    # physical cores: on a 6-core/12-thread CPU, 12 threads (and 4) were both slower than 6
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--beam", type=int, default=5)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else fail(2, "bad arguments (see --help)")

    path = os.path.abspath(a.path)
    if not os.path.isfile(path):
        return fail(2, f"file not found: {a.path}")
    if os.path.getsize(path) == 0:
        return fail(2, f"file is empty: {a.path}")
    model_dir = os.path.join(a.models_dir, a.model)
    if not os.path.isfile(os.path.join(model_dir, "model.bin")):
        return fail(4, f"model '{a.model}' is not installed (run: python -m jarvis stt-setup --model {a.model})")

    sweep_stale(os.path.join(HERE, "tmp"))
    tmp = os.path.join(HERE, "tmp", str(os.getpid()))
    os.makedirs(tmp, exist_ok=True)
    tempfile.tempdir = tmp
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = tmp
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        return run(a, path, model_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sweep_stale(root: str, max_age_s: float = 86400) -> None:
    try:
        for e in os.scandir(root):
            if e.is_dir() and time.time() - e.stat().st_mtime > max_age_s:
                shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


def run(a: argparse.Namespace, path: str, model_dir: str) -> int:
    t0 = time.perf_counter()
    try:   # a venv built for another Python, or a half-finished setup, fails right here
        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio
        from faster_whisper.tokenizer import _LANGUAGE_CODES
    except ImportError as e:
        return fail(4, f"the stt venv is broken ({type(e).__name__}: {e}) - rerun: python -m jarvis stt-setup")

    lang = a.language.strip().lower()
    lang = None if lang in ("", "auto") else lang
    if lang and lang not in _LANGUAGE_CODES:
        return fail(2, f"unknown language code '{a.language}' (use an ISO code such as en, or auto)")
    cands = [c.strip().lower() for c in a.candidates.split(",") if c.strip() and c.strip().lower() != "all"]
    bad = [c for c in cands if c not in _LANGUAGE_CODES]
    if bad:
        return fail(2, f"unknown language code(s) in --candidates: {','.join(bad)}")

    try:
        audio = decode_audio(path, sampling_rate=16000)
    except Exception as e:   # PyAV raises InvalidDataError & co. for anything that is not audio
        return fail(3, f"cannot decode {os.path.basename(path)}: {type(e).__name__}: {e}")
    dur = len(audio) / 16000.0
    if dur < 0.1:
        return fail(3, f"no audio stream in {os.path.basename(path)}")
    if a.max_minutes and dur > a.max_minutes * 60:
        return fail(6, f"the memo is {dur / 60:.1f} min long, over the {a.max_minutes:g}-minute limit "
                       f"(it would take about {dur * 0.35 / 60 + 1:.0f} min); --max-minutes 0 lifts it")

    try:
        t1 = time.perf_counter()
        model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=a.threads, local_files_only=True)
        load = time.perf_counter() - t1
    except Exception as e:
        return fail(4, f"model '{a.model}' failed to load: {type(e).__name__}: {e}")

    picker = OnlyCandidates(model.model, cands, watch=bool(lang))
    model.model = picker
    try:
        segments, info = model.transcribe(audio, language=lang, multilingual=lang is None, beam_size=a.beam,
                                          vad_filter=True, condition_on_previous_text=False, word_timestamps=False)
        text = " ".join(s.text.strip() for s in segments if s.text.strip())
    except Exception as e:
        return fail(5, f"transcription failed: {type(e).__name__}: {e}")
    wall = time.perf_counter() - t0

    if lang:
        lang_s = f"{lang}(forced)"
        heard = picker.heard_other(lang)
        if heard:
            lang_s += f" heard={heard}"
    else:
        lang_s = f"{picker.languages() or info.language} p={info.language_probability:.2f}"
        if picker.overruled():
            lang_s += f" whisper={picker.overruled()}"
    if text:
        print(text)
    print(f"[stt: model={a.model} lang={lang_s} dur={dur:.0f}s wall={wall:.0f}s load={load:.0f}s words={len(text.split())}]")
    sys.stdout.flush()
    return 0


class OnlyCandidates:
    """Stands in for the CTranslate2 Whisper inside WhisperModel so that every language decision - the first
    one and the per-window ones of multilingual mode - picks among `cands` only (all when empty),
    renormalised. Whisper does not know every language: unrestricted, it hears some (Macedonian, for one) as
    a neighbour (Bulgarian, Croatian) and writes it in the wrong script. Restricting it to the languages you
    actually speak fixes that. With `watch` (a forced language, where faster-whisper asks nothing) it also
    asks Whisper which language each window is in - one decoder step on an encoder output that is computed
    anyway - and keeps the answer, so a forced run can say `heard=`. Everything else is delegated."""

    def __init__(self, inner, cands: list[str], watch: bool = False):
        self._inner, self._cands, self.picks = inner, set(cands), []   # picks: (chosen, Whisper's own top)
        self._watch, self.heard = watch, []                            # heard: (top language, probability)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def encode(self, *args, **kwargs):
        out = self._inner.encode(*args, **kwargs)
        if self._watch:
            self.heard += [(probs[0][0][2:-2], probs[0][1]) for probs in self._inner.detect_language(out)]
        return out

    def heard_other(self, forced: str) -> str:
        return "+".join(dict.fromkeys(c for c, p in self.heard if c != forced and p >= HEARD_MIN_P))

    def detect_language(self, *args, **kwargs):
        out = []
        for probs in self._inner.detect_language(*args, **kwargs):   # one list per batch item, best first
            keep = [(t, p) for t, p in probs if not self._cands or t[2:-2] in self._cands] or probs
            total = sum(p for _, p in keep) or 1.0
            keep = [(t, p / total) for t, p in keep]
            self.picks.append((keep[0][0][2:-2], probs[0][0][2:-2]))
            out.append(keep)
        return out

    def languages(self) -> str:
        return "+".join(dict.fromkeys(c for c, _ in self.picks))

    def overruled(self) -> str:
        return "+".join(dict.fromkeys(t for c, t in self.picks if t != c))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
