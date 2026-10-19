"""Offline speech-to-text (stt/transcribe.py + jarvis/stt.py).

  python -m unittest tests.test_stt -v

The language restriction and the heard= watch are tested with a stand-in model (no download needed).
The end-to-end tests run only when `python -m jarvis stt-setup` has been done: they transcribe
tests/fixtures/hello.ogg - a synthetic voice (Windows System.Speech) saying "Hello Jarvis. Please check
the nightly build, and tell me if anything failed." - and check every error path's exit code.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis import config  # noqa: E402
from jarvis.stt import venv_python  # noqa: E402

spec = importlib.util.spec_from_file_location("transcribe_under_test", ROOT / "stt" / "transcribe.py")
T = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(T)

FIXTURE = ROOT / "tests" / "fixtures" / "hello.ogg"


def tok(code: str) -> str:
    return f"<|{code}|>"


class FakeWhisper:
    """What CTranslate2's Whisper returns from detect_language: per batch item, (token, p) best first."""

    def __init__(self, windows: list[list[tuple[str, float]]]) -> None:
        self.windows = windows

    def detect_language(self, *_a, **_k):
        return [[(tok(c), p) for c, p in w] for w in self.windows]

    def encode(self, *_a, **_k):
        return "features"


class Restriction(unittest.TestCase):
    def test_candidates_overrule_whisper_and_renormalise(self) -> None:
        # Whisper hears Macedonian as Bulgarian; restricted to mk/en the right language wins
        o = T.OnlyCandidates(FakeWhisper([[("bg", 0.70), ("mk", 0.20), ("en", 0.05)]]), ["mk", "en"])
        (probs,) = o.detect_language("features")
        self.assertEqual(probs[0][0], tok("mk"))
        self.assertAlmostEqual(probs[0][1], 0.20 / 0.25)
        self.assertEqual(o.languages(), "mk")
        self.assertEqual(o.overruled(), "bg")

    def test_no_candidates_means_whisper_decides(self) -> None:
        o = T.OnlyCandidates(FakeWhisper([[("de", 0.9), ("en", 0.1)]]), [])
        (probs,) = o.detect_language("features")
        self.assertEqual(probs[0][0], tok("de"))
        self.assertEqual(o.overruled(), "")

    def test_per_window_switch_is_reported(self) -> None:
        o = T.OnlyCandidates(FakeWhisper([[("de", 0.9)], [("en", 0.8)]]), [])
        o.detect_language("features")
        self.assertEqual(o.languages(), "de+en")

    def test_forced_language_watch_flags_a_translation(self) -> None:
        o = T.OnlyCandidates(FakeWhisper([[("de", 0.92), ("en", 0.05)]]), [], watch=True)
        o.encode("audio")
        self.assertEqual(o.heard_other("en"), "de", "forced English on German audio = a silent translation")
        quiet = T.OnlyCandidates(FakeWhisper([[("de", 0.40), ("en", 0.35)]]), [], watch=True)
        quiet.encode("audio")
        self.assertEqual(quiet.heard_other("en"), "", "below p=0.5 it is not called")


class Wrapper(unittest.TestCase):
    def test_missing_venv_is_exit_4_with_one_line(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="jarvis-stt-"))

        class NoVenv(config.Config):
            @property
            def stt_dir(self) -> Path:
                return tmp

        from jarvis.stt import cmd_stt
        cfg = NoVenv(config.DEFAULTS, None, tmp)
        args = type("A", (), {"path": str(FIXTURE), "model": None, "language": None, "candidates": None, "max_minutes": None})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cmd_stt(cfg, args), 4)
        self.assertTrue(err.getvalue().startswith("stt: error: no stt venv"), err.getvalue())


CFG = config.load()
VENV = venv_python(CFG)
MODEL = CFG.stt_models / CFG.stt["model"] / "model.bin"


@unittest.skipUnless(VENV.exists(), "run `python -m jarvis stt-setup` to enable the end-to-end stt tests")
class EndToEnd(unittest.TestCase):
    def jarvis_stt(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "jarvis", "stt", *args], cwd=str(ROOT), capture_output=True,
                              text=True, encoding="utf-8", timeout=540)

    def test_error_codes(self) -> None:
        r = self.jarvis_stt(str(ROOT / "nope.ogg"))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "stt: error: file not found: " + str(ROOT / "nope.ogg"))
        bogus = Path(tempfile.mkdtemp()) / "not-audio.ogg"
        bogus.write_text("this is not audio", encoding="utf-8")
        r = self.jarvis_stt(str(bogus))
        self.assertIn(r.returncode, (3, 4), r.stderr)   # 4 when no model is installed: checked before decoding
        self.assertTrue(r.stderr.strip().splitlines()[-1].startswith("stt: error: "))

    @unittest.skipUnless(MODEL.exists(), "the Whisper model is not installed")
    def test_transcribes_the_fixture_offline(self) -> None:
        r = self.jarvis_stt(str(FIXTURE), "--language", "en")
        self.assertEqual(r.returncode, 0, r.stderr)
        text, stats = r.stdout.strip().rsplit("\n", 1)
        self.assertIn("nightly build", text.lower())
        self.assertIn("jarvis", text.lower())
        self.assertTrue(stats.startswith("[stt: model=") and "lang=en(forced)" in stats, stats)
        self.assertNotIn("heard=", stats)


if __name__ == "__main__":
    unittest.main()
