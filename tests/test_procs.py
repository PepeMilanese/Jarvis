"""Process safety (jarvis/procs.py): the tree walk never follows a stale parent pid, and a recorded pid only
counts as alive while it is still the same process.

  python -m unittest tests.test_procs -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import procs  # noqa: E402
from jarvis.procs import Proc  # noqa: E402

NAME = "python.exe" if procs.IS_WIN else "python"


class Synthetic(unittest.TestCase):
    """A hand-made process table: what the walk may and may not claim."""

    def table(self) -> dict[int, Proc]:
        return {
            100: Proc(100, 1, NAME, 1000.0),         # the job
            101: Proc(101, 100, NAME, 1001.0),       # its child
            102: Proc(102, 101, NAME, 1002.0),       # its grandchild
            200: Proc(200, 100, NAME, 900.0),        # claims parent 100 but is OLDER: a stale ppid after pid reuse
            201: Proc(201, 200, NAME, 901.0),        # ... and that one's child
            300: Proc(300, 1, NAME, 1000.5),         # unrelated
        }

    def test_descendants_skip_a_stale_parent_pid(self) -> None:
        self.assertEqual(sorted(procs.descendants(100, self.table())), [101, 102])

    @unittest.skipUnless(procs.IS_WIN, "the name filter is Windows-only")
    def test_only_tree_names_are_claimed_on_windows(self) -> None:
        t = self.table()
        t[103] = Proc(103, 100, "explorer.exe", 1003.0)
        self.assertNotIn(103, procs.descendants(100, t))

    def test_is_descendant_walks_up_and_stops_at_a_stale_link(self) -> None:
        t = self.table()
        self.assertTrue(procs.is_descendant(102, 100, t))
        self.assertFalse(procs.is_descendant(300, 100, t))
        self.assertFalse(procs.is_descendant(201, 100, t), "the chain crosses the stale link 200 -> 100 (100 is younger)")

    def test_etime(self) -> None:
        self.assertEqual(procs._etime_s("05:07"), 307)
        self.assertEqual(procs._etime_s("01:02:03"), 3723)
        self.assertEqual(procs._etime_s("2-01:02:03"), 2 * 86400 + 3723)


class Real(unittest.TestCase):
    def test_kill_tree_stops_child_and_grandchild(self) -> None:
        code = ("import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); time.sleep(120)")
        p = subprocess.Popen([sys.executable, "-c", code])
        try:
            deadline = time.time() + 15
            kids = {}
            while time.time() < deadline and not any(k.name.lower().startswith("python") for k in kids.values()):
                time.sleep(0.2)
                kids = procs.descendants(p.pid)
            self.assertTrue(kids, "the grandchild showed up")
            stopped = procs.kill_tree(p.pid)
            self.assertIn(p.pid, stopped)
            time.sleep(1)
            for pid in [p.pid, *kids]:
                self.assertFalse(procs.pid_alive(pid), pid)
        finally:
            p.kill()

    def test_alive_checks_the_creation_time(self) -> None:
        me = os.getpid()
        born = procs.created(me)
        self.assertIsNotNone(born)
        self.assertTrue(procs.alive(me, born))
        self.assertFalse(procs.alive(me, born - 3600), "same pid, different process (reused pid)")
        self.assertFalse(procs.pid_alive(0))

    def test_spawn_writes_to_files_and_detaches(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="jarvis-spawn-"))
        p = procs.spawn([sys.executable, "-c", "print('hello from the child')"], cwd=d, env=dict(os.environ),
                        stdin_path=None, stdout_path=d / "out.txt", stderr_path=d / "err.txt", detach=True)
        p.wait(timeout=30)
        self.assertIn("hello from the child", (d / "out.txt").read_text())


if __name__ == "__main__":
    unittest.main()
