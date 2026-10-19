"""The supervisor's self-healing guards, each one driven by tests/fake_claude.py in a temp folder.

  python -m unittest tests.test_supervisor -v

Nothing here touches the real Telegram plugin: the "channel" state dir is temporary, poller sweeping and
Bot-API alerts are switched off, and `claude` is the fake. Timings are shrunk from minutes to seconds.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis import config, schedule  # noqa: E402
from jarvis.supervisor import RC_AUTH, RC_DEAF, Supervisor  # noqa: E402

FAKE = ROOT / "tests" / "fake_claude.py"
FAST = {"poller_wait_s": 15, "poller_settle_s": 0.2, "init_wait_s": 6, "poller_gone_s": 1.5, "rival_grace_s": 1,
        "recheck_s": 1, "watch_tick_s": 0.2, "schedule_tick_s": 0.3, "idle_settle_s": 0.3, "auth_poll_s": 0.3,
        "deaf_backoff_s": [0.1]}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-sup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tg = self.tmp / "telegram"
        self.tg.mkdir()
        (self.tg / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["1000001"]}))
        data = copy.deepcopy(config.DEFAULTS)
        data["telegram"]["state_dir"] = str(self.tg)
        self.cfg = config.Config(data, None, self.tmp / "state")
        self.cfg.ensure_dirs()
        self.env = mock.patch.dict(os.environ, {"FAKE_BOT_DIR": str(self.tg)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.helpers: list[subprocess.Popen] = []
        self.addCleanup(lambda: [p.kill() for p in self.helpers])

    def sup(self, **kw) -> Supervisor:
        return Supervisor(self.cfg, claude_argv=[sys.executable, str(FAKE)], sweep=False, alerts=False, prewarm=False, echo=False,
                          timings=FAST, check_auth=kw.pop("check_auth", False), **kw)

    def log(self) -> str:
        return (self.cfg.logs / "dispatcher.log").read_text(encoding="utf-8")

    def received(self) -> list[dict]:
        p = self.tg / "received.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def run_bg(self, s: Supervisor) -> dict:
        out: dict = {}
        th = threading.Thread(target=lambda: out.setdefault("rc", s.run_once()), daemon=True)
        th.start()
        out["thread"] = th
        return out

    def wait(self, cond, timeout: float = 30, what: str = "") -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}\n--- supervisor log ---\n{self.log() if (self.cfg.logs / 'dispatcher.log').exists() else ''}")


class Healthy(Base):
    def test_live_session_gets_the_kickoff_and_scheduled_fires_between_turns(self) -> None:
        s = self.sup()
        run = self.run_bg(s)
        self.wait(lambda: s.live is not None, what="a live session")
        self.assertIn("channel connected at init", self.log())
        self.assertIn("dispatcher online", self.received()[0]["text"], "the kickoff came first")
        schedule.save(self.cfg.schedule_file, {"version": 1, "entries": [
            {"id": "ping", "kind": "once", "at": schedule.iso(schedule.now()), "prompt": "PING-FROM-THE-SCHEDULE"}]})
        threading.Thread(target=s.schedule_loop, daemon=True).start()
        self.wait(lambda: any("PING-FROM-THE-SCHEDULE" in r["text"] for r in self.received()), what="the fire")
        fire = next(r["text"] for r in self.received() if "PING-FROM-THE-SCHEDULE" in r["text"])
        self.assertTrue(fire.startswith("[jarvis-schedule] fire"))
        self.assertIn("1000001", fire, "the chat id from access.json travels with the fire")
        self.wait(lambda: schedule.find(schedule.load(self.cfg.schedule_file), "ping") is None, what="the one-shot to be acked")
        s.live.proc.kill()
        run["thread"].join(20)
        self.assertNotIn(run["rc"], (RC_DEAF, RC_AUTH))


class Guards(Base):
    def expect(self, rc: int, needle: str, timeout: float = 30) -> None:
        s = self.sup()
        run = self.run_bg(s)
        run["thread"].join(timeout)
        self.assertFalse(run["thread"].is_alive(), "run_once returned")
        self.assertEqual(run.get("rc"), rc, self.log())
        self.assertIn(needle, self.log())

    def test_deaf_session_is_killed_at_init(self) -> None:
        os.environ["FAKE_CHANNEL"] = "pending"
        self.expect(RC_DEAF, "a DEAF session")

    def test_hung_start_without_init(self) -> None:
        os.environ["FAKE_NO_INIT"] = "1"
        self.expect(RC_DEAF, "a hung start")

    def test_login_failure_kills_the_session(self) -> None:
        os.environ["FAKE_AUTH_FAIL"] = "1"
        self.expect(RC_AUTH, "session login failed")
        self.assertIn("(alert suppressed) Jarvis is DOWN", self.log())

    def test_poller_gone(self) -> None:
        os.environ.update({"FAKE_POLLER": "child", "FAKE_POLLER_DIES_S": "1"})
        self.expect(RC_DEAF, "poller gone")

    def test_stolen_bot(self) -> None:
        rival = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.helpers.append(rival)
        os.environ.update({"FAKE_RIVAL_PID": str(rival.pid), "FAKE_RIVAL_AFTER_S": "1.5"})
        self.expect(RC_DEAF, "another session's poller took the bot")


class Login(Base):
    def test_waits_while_logged_out_and_alerts_once(self) -> None:
        os.environ["FAKE_LOGGED_IN"] = "false"
        s = self.sup(check_auth=True)
        th = threading.Thread(target=s.wait_for_login, daemon=True)
        th.start()
        time.sleep(1.2)
        self.assertTrue(th.is_alive(), "no session is started while logged out")
        os.environ["FAKE_LOGGED_IN"] = "true"
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertEqual(self.log().count("(alert suppressed)"), 1, "one alert per outage")
        self.assertIn("logged in again", self.log())


class Manual(Base):
    def test_placeholders_are_filled(self) -> None:
        text = self.sup().render_manual().read_text(encoding="utf-8")
        self.assertNotIn("{{", text)
        self.assertIn(f'cd "{ROOT.as_posix()}"', text)


if __name__ == "__main__":
    unittest.main()
