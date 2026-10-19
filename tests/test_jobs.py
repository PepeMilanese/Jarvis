"""End-to-end job lifecycle through the real CLI, watchdog and slots - with tests/fake_claude.py standing in
for `claude`, so no model is ever called.

  python -m unittest tests.test_jobs -v        (about a minute: real processes, real waits)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis import procs  # noqa: E402

FAKE = ROOT / "tests" / "fake_claude.py"


class Jobs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-jobs-"))
        self.addCleanup(self.teardown_state)
        self.env = {**os.environ, "JARVIS_STATE": str(self.tmp / "state"),
                    "JARVIS_CONFIG": str(self.tmp / "none.toml"),
                    "JARVIS_CLAUDE": json.dumps([sys.executable, str(FAKE)])}
        self.env.pop("JARVIS_QUOTA_LIVE", None)

    def teardown_state(self) -> None:
        for n in (1, 2, 3):   # never leave a fake job running
            rec = self.record(n)
            if rec and rec.get("status") in ("launching", "running"):
                self.jarvis("stop", "--slot", str(n))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def jarvis(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(ROOT), env=self.env,
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        if check and r.returncode != 0:
            self.fail(f"jarvis {' '.join(args)} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def start(self, task: str, *extra: str) -> dict:
        r = self.jarvis("start", "--task", task, *extra, check=True)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def record(self, n: int) -> dict | None:
        p = self.tmp / "state" / "jobs" / f"s{n}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait_status(self, n: int, *want: str, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.record(n) or {}
            if rec.get("status") in want:
                return rec
            time.sleep(0.25)
        self.fail(f"slot {n} never reached {want}: {self.record(n)}")

    # --- the tests -------------------------------------------------------------------------------------------
    def test_done_with_summary_and_verified_pr(self) -> None:
        j = self.start("say hello FAKE-PR")
        rec = self.wait_status(j["slot"], "done")
        self.assertEqual(rec["prs"], ["https://github.com/acme/demo/pull/42"],
                         "only the PR the job's own `gh pr create` printed - never one it merely mentioned (#7)")
        self.assertAlmostEqual(rec["costUsd"], 0.0123)
        self.assertTrue(rec["sessionId"])
        out = self.jarvis("result", "--slot", str(j["slot"]), check=True).stdout
        self.assertIn("JARVIS-SUMMARY", out)
        st = self.jarvis("status", "--no-quota", check=True).stdout
        self.assertIn("last step:  step 2: wrote the result", st)
        self.assertIn("PR (opened by this job): https://github.com/acme/demo/pull/42", st)
        ledger = [json.loads(x) for x in (self.tmp / "state" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(ledger[-1]["status"], "done")
        self.assertFalse((self.tmp / "state" / "locks" / f"s{j['slot']}.lock").exists(), "the slot is released")

    def test_question_keeps_the_slot_and_answer_resumes_the_same_session(self) -> None:
        j = self.start("choose FAKE-QUESTION")
        n = j["slot"]
        rec = self.wait_status(n, "question")
        sid = rec["sessionId"]
        self.assertIn("waiting for an answer", self.jarvis("start", "--task", "x", "--slot", str(n)).stderr)
        self.jarvis("answer", "--slot", str(n), "--text", "B", check=True)
        rec = self.wait_status(n, "done")
        self.assertEqual(rec["answers"], 1)
        ran = (Path(rec["dir"]) / "worker-ran.txt").read_text(encoding="utf-8").split()
        self.assertEqual(ran, [sid, "resumed"], "resumed with --resume <the same session id>")
        self.assertIn("fake proof (answered)", self.jarvis("result", "--slot", str(n)).stdout)
        answer = Path(rec["answerFile"]).read_text(encoding="utf-8")
        self.assertIn("B", answer)

    def test_a_resumed_run_that_dies_is_not_the_old_question_again(self) -> None:
        for answer, want in (("DIE-SILENTLY", "ended-early"), ("LOGIN-GONE", "auth-failed")):
            j = self.start("choose FAKE-QUESTION")
            n = j["slot"]
            self.wait_status(n, "question")
            self.jarvis("answer", "--slot", str(n), "--text", answer, check=True)
            rec = self.wait_status(n, "ended-early", "auth-failed", "question", "done")
            self.assertEqual(rec["status"], want, f"after answering {answer}")
            self.jarvis("cleanup", "--slot", str(n), "--force", check=True)

    def test_dry_run_keeps_a_finished_record(self) -> None:
        j = self.start("say hello")
        rec = self.wait_status(j["slot"], "done")
        d = self.start("plan only", "--dry-run", "--slot", str(j["slot"]))
        self.assertEqual(d["status"], "dry-run")
        self.assertEqual(self.record(j["slot"])["runid"], rec["runid"], "the unreported result is still there")
        self.assertFalse(Path(d["dir"]).exists(), "a dry run creates no folder")

    def test_worker_environment_and_flags(self) -> None:
        self.env.update({"ANTHROPIC_API_KEY": "would-bill-per-token", "CLAUDE_EFFORT": "max", "CLAUDECODE": "1",
                         "CLAUDE_CONFIG_DIR": str(self.tmp / "cfgdir")})
        j = self.start("look around")
        rec = self.wait_status(j["slot"], "done")
        seen = json.loads((Path(rec["dir"]) / "worker-env.json").read_text(encoding="utf-8"))
        self.assertFalse(seen["has_api_key"], "an API key would bill per token: workers never get one by default")
        self.assertFalse(seen["has_session_marker"], "nothing of the parent Claude session leaks into the worker")
        self.assertEqual(seen["kept_user_setting"], str(self.tmp / "cfgdir"), "the owner's own CLAUDE_* settings pass")
        self.assertTrue(os.path.samefile(seen["git_ceiling"], self.tmp / "state" / "work"), "git cannot walk up into Jarvis")
        self.assertNotIn("--max-budget-usd", seen["argv"], "no cap unless configured")
        j2 = self.start("look around again", "--budget-usd", "5")
        rec2 = self.wait_status(j2["slot"], "done")
        argv = json.loads((Path(rec2["dir"]) / "worker-env.json").read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "5")

    def test_stop_kills_the_tree_and_frees_the_slot(self) -> None:
        j = self.start("nap FAKE-SLEEP")
        n = j["slot"]
        rec = self.wait_status(n, "running")
        pid, born = rec["pid"], rec.get("pidCreated")
        self.assertTrue(procs.alive(pid, born))
        self.jarvis("stop", "--slot", str(n), check=True)
        time.sleep(1)
        self.assertFalse(procs.alive(pid, born))
        self.assertEqual(self.record(n)["status"], "stopped")
        time.sleep(1)
        self.assertEqual(self.record(n)["status"], "stopped", "the killed watchdog must not re-classify the job")
        self.jarvis("cleanup", "--slot", str(n), "--force", check=True)
        self.assertIsNone(self.record(n))

    def test_orphan_left_by_the_job_is_reaped(self) -> None:
        j = self.start("spawn something FAKE-ORPHAN")
        rec = self.wait_status(j["slot"], "done", timeout=90)
        orphan = int((Path(rec["dir"]) / "orphan.pid").read_text())
        time.sleep(0.5)
        self.assertFalse(procs.pid_alive(orphan), "a process the job spawned and left behind is stopped")

    def test_silent_job_is_ended_early(self) -> None:
        j = self.start("say nothing FAKE-SILENT")
        self.wait_status(j["slot"], "ended-early")

    def test_slots_fill_and_a_fourth_is_refused(self) -> None:
        for _ in range(3):
            self.start("nap FAKE-SLEEP")
        r = self.jarvis("start", "--task", "one more")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no free slot", r.stderr)

    def test_dry_run_launches_nothing(self) -> None:
        j = self.start("plan only", "--dry-run")
        self.assertEqual(j["status"], "dry-run")
        self.assertFalse(Path(j["log"]).exists())
        brief = (self.tmp / "state" / "logs" / f"{j['runid']}-brief.md").read_text(encoding="utf-8")
        self.assertIn("- TASK: plan only", brief)
        self.assertIn("## 0. Five rules that override everything below", brief, "the worker contract follows the header")

    def test_refusals(self) -> None:
        self.assertIn("needs a task", self.jarvis("start", "--task", "  ").stderr)
        self.assertIn("not an existing folder", self.jarvis("start", "--task", "x", "--dir", str(self.tmp / "nope")).stderr)
        self.assertIn("Jarvis folder itself", self.jarvis("start", "--task", "x", "--dir", str(ROOT)).stderr)
        self.assertIn("--budget-usd must be >= 0", self.jarvis("start", "--task", "x", "--budget-usd", "-1").stderr)
        self.env["FAKE_LOGGED_IN"] = "false"
        self.assertIn("logged out", self.jarvis("start", "--task", "x").stderr)

    def test_protected_dir(self) -> None:
        guarded = self.tmp / "live-checkout"
        (guarded / "sub").mkdir(parents=True)
        cfg = self.tmp / "jarvis.toml"
        cfg.write_text(f"[workers]\nprotected_dirs = [{json.dumps(str(guarded))}]\n", encoding="utf-8")
        self.env["JARVIS_CONFIG"] = str(cfg)
        r = self.jarvis("start", "--task", "x", "--dir", str(guarded / "sub"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("protected folder", r.stderr)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_repo_job_gets_its_own_worktree_and_branch(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True)  # noqa: E731
        g("init", "-q", "-b", "main")
        g("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")
        j = self.start("fix the flaky date test", "--repo", str(repo))
        rec = self.wait_status(j["slot"], "done")
        self.assertTrue(rec["branch"].startswith("jarvis/"), rec["branch"])
        self.assertTrue(rec["branch"].endswith("-fix-flaky-date-test"), rec["branch"])
        wt = Path(rec["dir"])
        self.assertTrue((wt / ".git").is_file(), "a worktree, not a copy")
        self.assertIn(rec["branch"], g("branch", "--list", rec["branch"]).stdout)
        self.jarvis("cleanup", "--slot", str(j["slot"]), "--force", check=True)
        self.assertFalse(wt.exists())
        self.assertIn(rec["branch"], g("branch", "--list", rec["branch"]).stdout, "the branch is kept")


if __name__ == "__main__":
    unittest.main()
