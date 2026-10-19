"""Tests for the durable schedule (jarvis/schedule.py).

  python -m unittest tests.test_schedule -v

Offline and fast: a fake clock and a fake session drive schedule.Runner exactly as the supervisor's
thread does. tests/test_supervisor.py proves the same delivery through a real supervisor and a fake claude.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import schedule as S  # noqa: E402


class FakeSession:
    def __init__(self, sid: str = "sess-1") -> None:
        self.id = sid
        self.is_idle = True
        self.done = 1           # the kickoff turn has finished
        self.sent: list[str] = []
        self.broken = False

    def idle(self) -> bool:
        return self.is_idle

    def results(self) -> int:
        return self.done

    def send(self, text: str) -> None:
        if self.broken:
            raise OSError("gone")
        self.sent.append(text)

    def finish_turn(self) -> None:
        self.done += 1


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def add(self, **kw) -> None:
        self.t += timedelta(**kw)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="schedtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "schedule.json"
        self.jobs = self.tmp / "jobs"
        self.jobs.mkdir()
        self.clock = Clock(datetime(2026, 10, 19, 14, 0, 0))
        self.logs: list[str] = []
        self.alive: set[int] = set()
        self.runner = S.Runner(self.path, self.jobs, log=self.logs.append, pid_alive=lambda p: p in self.alive,
                               chat_ids=lambda: ["4242"], clock=self.clock)

    def put(self, *entries: dict) -> None:
        S.save(self.path, {"version": 1, "entries": list(entries)})

    def data(self) -> dict:
        return S.load(self.path)

    def entry(self, eid: str) -> dict | None:
        return S.find(self.data(), eid)

    def once(self, eid: str, minutes: int, prompt: str = "do the thing") -> dict:
        return {"id": eid, "kind": "once", "at": S.iso(self.clock.t + timedelta(minutes=minutes)), "prompt": prompt}

    def watch_paused(self) -> dict:
        return {**S.watch_entry(), "paused": True}

    def history(self) -> list[dict]:
        p = S.history_path(self.path)
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def cli(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = S.main(["--file", str(self.path), *args])
        finally:
            sys.stdin = old_stdin
        return rc, out.getvalue(), err.getvalue()


class Durations(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(S.parse_duration("45m"), 2700)
        self.assertEqual(S.parse_duration("1h30m"), 5400)
        self.assertEqual(S.parse_duration("2d"), 172800)
        self.assertEqual(S.parse_duration("90s"), 90)

    def test_rejects(self) -> None:
        for bad in ("5x", "m", "", "1h 30", "0m", "-5m"):
            with self.assertRaises(S.ScheduleError, msg=bad):
                S.parse_duration(bad)

    def test_when(self) -> None:
        ref = datetime(2026, 10, 19, 14, 0)
        self.assertEqual(S.parse_when("15:30", ref), datetime(2026, 10, 19, 15, 30))
        self.assertEqual(S.parse_when("09:00", ref), datetime(2026, 10, 20, 9, 0))   # past today -> tomorrow
        self.assertEqual(S.parse_when("2026-10-20 02:00", ref), datetime(2026, 10, 20, 2, 0))
        with self.assertRaises(S.ScheduleError):
            S.parse_when("tomorrow", ref)


class Cron(unittest.TestCase):
    def test_weekdays(self) -> None:   # Fri 23 Oct 08:00 -> Mon 26 Oct 07:30
        self.assertEqual(S.cron_next("30 7 * * 1-5", datetime(2026, 10, 23, 8, 0)), datetime(2026, 10, 26, 7, 30))

    def test_step(self) -> None:
        self.assertEqual(S.cron_next("*/15 * * * *", datetime(2026, 10, 19, 10, 7)), datetime(2026, 10, 19, 10, 15))
        self.assertEqual(S.cron_next("*/15 * * * *", datetime(2026, 10, 19, 10, 15)), datetime(2026, 10, 19, 10, 30))

    def test_leap_day(self) -> None:
        self.assertEqual(S.cron_next("0 0 29 2 *", datetime(2026, 3, 1)), datetime(2028, 2, 29))

    def test_dom_or_dow(self) -> None:   # both restricted = either matches (Vixie): Mon 26 Oct beats Sun 1 Nov
        self.assertEqual(S.cron_next("0 9 1 * mon", datetime(2026, 10, 19, 10, 0)), datetime(2026, 10, 26, 9, 0))

    def test_sunday_is_0_and_7(self) -> None:
        self.assertEqual(S.cron_next("0 12 * * 7", datetime(2026, 10, 19)), datetime(2026, 10, 25, 12, 0))
        self.assertEqual(S.cron_next("0 12 * * 0", datetime(2026, 10, 19)), datetime(2026, 10, 25, 12, 0))

    def test_rejects(self) -> None:
        for bad in ("61 * * * *", "* * * *", "* 24 * * *", "*/0 * * * *", "x * * * *"):
            with self.assertRaises(S.ScheduleError, msg=bad):
                S.cron_next(bad, datetime(2026, 10, 19))


class Delivery(Base):
    def test_nothing_without_a_session(self) -> None:
        self.put(self.watch_paused(), self.once("a", -1))
        self.assertIsNone(self.runner.tick(None))
        self.assertIsNotNone(self.entry("a"))

    def test_not_yet_due(self) -> None:
        self.put(self.watch_paused(), self.once("a", 5))
        s = FakeSession()
        self.assertIsNone(self.runner.tick(s))
        self.clock.add(minutes=5)
        self.assertIn('entry "a"', self.runner.tick(s))

    def test_waits_for_idle_then_fires_once_and_acks(self) -> None:
        self.put(self.watch_paused(), self.once("a", -1, "PROMPT-A"))
        s = FakeSession()
        s.is_idle = False
        self.assertIsNone(self.runner.tick(s), "never written mid-turn")
        s.is_idle = True
        text = self.runner.tick(s)
        self.assertTrue(text.startswith(S.PREFIX))
        self.assertIn("PROMPT-A", text)
        self.assertIn("4242", text)                      # the chat id travels with it
        self.assertIsNone(self.runner.tick(s), "pending: not sent twice")
        self.assertEqual(self.entry("a")["pending"]["session"], "sess-1")
        s.finish_turn()
        self.runner.tick(s)
        self.assertIsNone(self.entry("a"), "a one-shot is removed once a turn on it finished")
        self.assertEqual([h["event"] for h in self.history() if h["id"] == "a"], ["fired", "done"])

    def test_everything_due_goes_in_one_message(self) -> None:
        self.put(self.watch_paused(), self.once("a", -2, "PROMPT-A"), self.once("b", -1, "PROMPT-B"))
        s = FakeSession()
        self.runner.tick(s)
        self.assertEqual(len(s.sent), 1)
        self.assertIn("PROMPT-A", s.sent[0])
        self.assertIn("PROMPT-B", s.sent[0])

    def test_pending_blocks_the_next_fire_until_its_turn_ends(self) -> None:
        self.put(self.watch_paused(), self.once("a", -1, "PROMPT-A"), self.once("b", 2, "PROMPT-B"))
        s = FakeSession()
        self.runner.tick(s)
        self.clock.add(minutes=3)
        self.assertIsNone(self.runner.tick(s), "b is due, but a's turn has not ended")
        s.finish_turn()
        self.assertIn("PROMPT-B", self.runner.tick(s))

    def test_resent_to_the_next_session_then_given_up(self) -> None:
        self.put(self.watch_paused(), self.once("a", -1, "PROMPT-A"))
        self.runner.tick(FakeSession("s1"))
        t2 = self.runner.tick(FakeSession("s2"))
        self.assertIn("RE-SENT, send 2 of 3", t2)
        self.assertIn("first sent at 14:00", t2)
        self.clock.add(minutes=1)
        t3 = self.runner.tick(FakeSession("s3"))
        self.assertIn("RE-SENT, send 3 of 3", t3)
        self.assertIn("first sent at 14:00", t3)
        self.assertIsNone(self.runner.tick(FakeSession("s4")))
        self.assertTrue(self.entry("a")["failed"], "kept, marked failed, for the dispatcher to see in `list`")
        self.assertIsNone(self.runner.tick(FakeSession("s5")))

    def test_a_failed_send_keeps_it_for_the_next_session(self) -> None:
        self.put(self.watch_paused(), self.once("a", -1))
        s = FakeSession("s1")
        s.broken = True
        self.assertIsNone(self.runner.tick(s))
        self.assertIn("RE-SENT", self.runner.tick(FakeSession("s2")))

    def test_late_one_shot_says_how_late(self) -> None:
        self.put(self.watch_paused(), self.once("a", -45))
        self.assertIn("45m late", self.runner.tick(FakeSession()))

    def test_every_folds_missed_occurrences_and_keeps_its_rhythm(self) -> None:
        start = datetime(2026, 10, 19, 10, 0)
        self.put(self.watch_paused(), {"id": "e", "kind": "every", "every_s": 1800, "next": S.iso(start), "prompt": "P"})
        self.clock.t = datetime(2026, 10, 19, 12, 10)
        s = FakeSession()
        self.assertEqual(self.runner.tick(s).count('entry "e"'), 1)
        self.assertEqual(S.parse_iso(self.entry("e")["next"]), datetime(2026, 10, 19, 12, 30))
        s.finish_turn()
        self.runner.tick(s)
        self.assertNotIn("pending", self.entry("e"))
        self.assertIsNotNone(self.entry("e"), "a recurring entry stays")

    def test_cron_entry_advances(self) -> None:
        self.put(self.watch_paused(), {"id": "c", "kind": "cron", "cron": "0 7 * * *", "next": S.iso(datetime(2026, 10, 19, 7, 0)), "prompt": "P"})
        self.runner.tick(FakeSession())
        self.assertEqual(S.parse_iso(self.entry("c")["next"]), datetime(2026, 10, 20, 7, 0))

    def test_paused_entry_does_not_fire(self) -> None:
        self.put(self.watch_paused(), {**self.once("a", -1), "paused": True})
        self.assertIsNone(self.runner.tick(FakeSession()))

    def test_stuck_pending_is_let_go(self) -> None:
        self.put(self.watch_paused(), self.once("a", -1), self.once("b", 1, "PROMPT-B"))
        s = FakeSession()
        self.runner.tick(s)
        self.clock.add(minutes=31)
        self.assertIn("PROMPT-B", self.runner.tick(s))
        self.assertIn("unconfirmed", [h["event"] for h in self.history()])

    def test_corrupt_file_fires_nothing_and_is_left_alone(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.runner.tick(FakeSession()))
        self.assertIsNone(self.runner.tick(FakeSession()))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")
        self.assertEqual(len(self.logs), 1, "logged once, not every tick")

    def test_stale_lock_is_broken(self) -> None:
        lock = self.path.with_name(self.path.name + ".lock")
        lock.write_text("999", encoding="utf-8")
        old = time.time() - 120
        os.utime(lock, (old, old))
        self.put(self.watch_paused(), self.once("a", -1))
        self.assertIsNotNone(self.runner.tick(FakeSession()))


class Watch(Base):
    def job(self, status: str, pid: int = 101, runid: str = "20261019-140000-s1") -> None:
        (self.jobs / "s1.json").write_text(json.dumps({"runid": runid, "slot": 1, "mode": "general", "task": "t",
                                                       "status": status, "pid": pid, "watchPid": pid}), encoding="utf-8")

    def test_seeded_when_missing(self) -> None:
        self.runner.tick(FakeSession())
        self.assertEqual(self.entry("watch")["kind"], "watch")

    def test_polls_while_active_then_once_on_done_then_quiet(self) -> None:
        self.alive.add(101)
        self.job("running")
        s = FakeSession()
        self.assertIsNone(self.runner.tick(s), "first sight is remembered, not reported (the kickoff ran status)")
        t = self.runner.tick(s)
        self.assertIn("watch poll - a slot is active", t)
        self.assertIn("s1 running general 20261019-140000-s1", t)
        s.finish_turn()
        self.clock.add(minutes=2)
        self.assertIsNone(self.runner.tick(s), "not before 4 minutes")
        self.clock.add(minutes=2)
        self.assertIsNotNone(self.runner.tick(s))
        s.finish_turn()
        self.job("done")
        self.alive.discard(101)
        self.clock.add(seconds=30)
        self.assertIsNone(self.runner.tick(s), "a change waits out the 60 s gap")
        self.clock.add(seconds=30)
        t = self.runner.tick(s)
        self.assertIn("a slot changed state", t)
        self.assertIn("s1 done", t)
        s.finish_turn()
        for _ in range(5):
            self.clock.add(minutes=4)
            self.assertIsNone(self.runner.tick(s), "nothing active, nothing changed")

    def test_cleanup_dropping_a_record_is_not_news(self) -> None:
        self.job("done")
        s = FakeSession()
        self.runner.tick(s)                       # first sight
        (self.jobs / "s1.json").unlink()          # `cleanup --slot 1`
        self.clock.add(minutes=2)
        self.assertIsNone(self.runner.tick(s))
        self.assertEqual(self.entry("watch")["seen"], "", "the vanished slot is forgotten quietly")
        self.job("running", pid=202, runid="20261019-150000-s1")   # the next job on that slot IS news
        self.alive.add(202)
        self.clock.add(minutes=2)
        self.assertIn("20261019-150000-s1", self.runner.tick(s))

    def test_question_is_reported_once_and_not_polled(self) -> None:
        self.alive.add(101)
        self.job("running")
        s = FakeSession()
        self.runner.tick(s)
        self.runner.tick(s)
        s.finish_turn()
        self.job("question")
        self.alive.discard(101)
        self.clock.add(minutes=1)
        self.assertIn("s1 question", self.runner.tick(s))
        s.finish_turn()
        self.clock.add(minutes=10)
        self.assertIsNone(self.runner.tick(s))

    def test_a_watch_poll_is_never_resent(self) -> None:
        self.alive.add(101)
        self.job("running")
        self.runner.tick(FakeSession("s1"))
        self.runner.tick(FakeSession("s1"))
        self.assertIn("pending", self.entry("watch"))
        self.assertIsNone(self.runner.tick(FakeSession("s2")), "dropped, and the next poll waits its interval")
        self.assertIn("dropped", [h["event"] for h in self.history()])
        self.clock.add(minutes=4)
        self.assertNotIn("RE-SENT", self.runner.tick(FakeSession("s2")))

    def test_dead_pid_with_running_status_is_not_active(self) -> None:
        self.job("running")       # pid 101 not alive: the watchdog died - one poll for the change, no more
        s = FakeSession()
        self.runner.tick(s)
        for _ in range(3):
            self.clock.add(minutes=5)
            self.assertIsNone(self.runner.tick(s))


class Cli(Base):
    def test_add_list_remove(self) -> None:
        rc, out, _ = self.cli("add", "--id", "nightly", "--cron", "0 2 * * *", "--prompt", "start the night queue")
        self.assertEqual(rc, 0)
        self.assertIn("added nightly: cron '0 2 * * *'", out)
        rc, out, _ = self.cli("list")
        self.assertIn("nightly", out)
        self.assertIn("start the night queue", out)
        rc, out, _ = self.cli("remove", "nightly")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.entry("nightly"))

    def test_in_and_every(self) -> None:
        self.assertEqual(self.cli("add", "--id", "x", "--in", "90m", "--prompt", "p")[0], 0)
        self.assertEqual(self.entry("x")["kind"], "once")
        self.assertEqual(self.cli("add", "--id", "y", "--every", "30m", "--prompt", "p")[0], 0)
        self.assertEqual(self.entry("y")["every_s"], 1800)

    def test_prompt_from_stdin_keeps_quotes_and_dollars(self) -> None:
        text = "check `status`; say \"it's $5\" and C:\\Users\\someone\\My Folder"
        self.assertEqual(self.cli("add", "--id", "q", "--in", "5m", "--prompt", "-", stdin=text)[0], 0)
        self.assertEqual(self.entry("q")["prompt"], text)

    def test_refusals(self) -> None:
        self.cli("add", "--id", "dup", "--in", "5m", "--prompt", "p")
        rc, _, err = self.cli("add", "--id", "dup", "--in", "5m", "--prompt", "p")
        self.assertEqual(rc, 2)
        self.assertIn("--replace", err)
        self.assertEqual(self.cli("add", "--id", "dup", "--in", "9m", "--prompt", "p2", "--replace")[0], 0)
        self.assertEqual(self.entry("dup")["prompt"], "p2")
        self.assertEqual(self.cli("add", "--id", "watch", "--in", "5m", "--prompt", "p")[0], 2)
        self.assertEqual(self.cli("add", "--in", "5m", "--every", "5m", "--prompt", "p")[0], 2)
        self.assertEqual(self.cli("add", "--at", "2020-01-01 10:00", "--prompt", "p")[0], 2)
        self.assertEqual(self.cli("add", "--in", "5m", "--prompt", "")[0], 2)
        self.assertEqual(self.cli("add", "--every", "30s", "--prompt", "p")[0], 2)
        self.assertEqual(self.cli("remove", "nope")[0], 2)

    def test_watch_can_be_paused_not_removed(self) -> None:
        self.runner.tick(FakeSession())   # seeds it
        rc, _, err = self.cli("remove", "watch")
        self.assertEqual(rc, 2)
        self.assertIn("pause watch", err)
        self.assertEqual(self.cli("pause", "watch")[0], 0)
        self.assertTrue(self.entry("watch")["paused"])
        self.assertEqual(self.cli("resume", "watch")[0], 0)
        self.assertNotIn("paused", self.entry("watch"))

    def test_history(self) -> None:
        self.cli("add", "--id", "h", "--in", "5m", "--prompt", "p")
        rc, out, _ = self.cli("history")
        self.assertIn("added", out)


if __name__ == "__main__":
    unittest.main()
