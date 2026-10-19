"""The per-slot watchdog: one detached process per running job.

    python -m jarvis.watchdog --slot N --runid R [--resume]

`start` and `answer` launch it fully detached and return in seconds; the watchdog then OWNS the worker:

1. starts `claude -p` with stdin = the brief (or the answer), stdout = the transcript, stderr = a file -
   never a pipe, so nothing upstream ever waits on it;
2. waits on the process object, 15 s at a time, and snapshots the worker's process tree each time, so it
   knows exactly what the job spawned;
3. after the worker exits (or hits a ceiling, if one is configured - none by default): stops whatever the
   job spawned and left running - and nothing it did not see under the job;
4. classifies the outcome from the result file and what THIS run wrote: `done` (a JARVIS-SUMMARY block),
   `question` (a JARVIS-QUESTION block - the slot stays reserved for `answer`), `auth-failed`, `timeout`,
   or `ended-early` (no block at all);
5. writes the job record, one ledger row, and frees the slot.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import procs, slots, transcript
from .config import Config, JarvisError, load
from .util import FileLock, clean_env, now_iso, read_shared

AUTH_FAIL = re.compile(r"Failed to authenticate|OAuth session expired|Invalid API key|Please run /login", re.I)
POLL_S = 15


def say(cfg: Config, job: dict, msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with open(cfg.logs / f"{job['runid']}-watch.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def worker_argv(cfg: Config, job: dict, resume: bool) -> list[str]:
    w = cfg.workers
    a = [*cfg.claude_argv(), "-p"]
    if resume:
        a += ["--resume", str(job["sessionId"])]
    if job.get("model"):
        a += ["--model", job["model"]]
    if job.get("effort"):
        a += ["--effort", job["effort"]]
    a += ["--permission-mode", w.get("permission_mode") or "bypassPermissions",
          "--output-format", "stream-json", "--verbose",
          # the worker needs write access to its RESULT_FILE and steps file - the logs folder, nothing else of
          # Jarvis (Claude Code reads CLAUDE.md from every --add-dir, so never the repo root)
          "--add-dir", str(cfg.logs)]
    budget = float(job.get("budgetUsd") or 0)
    if budget > 0:                       # 0 = no cap: the flag is simply not passed
        a += ["--max-budget-usd", f"{budget:g}"]
    if job.get("mode") != "repo" and w.get("add_home", True):
        a += ["--add-dir", str(Path.home())]
    return a


def worker_env(cfg: Config, job: dict) -> dict[str, str]:
    extra = {"JARVIS_RUNID": job["runid"], "JARVIS_SLOT": str(job["slot"]), "JARVIS_RESULT": job["resultFile"],
             "JARVIS_STEPS": job["stepsFile"]}
    if job.get("kind") == "general":
        # a scratch folder lives under the Jarvis checkout: stop git from walking up into it, so a worker's
        # `git reset --hard` or `git stash` in its scratch folder can never act on the live Jarvis code
        extra["GIT_CEILING_DIRECTORIES"] = str(cfg.work)
    tok_file = cfg.workers.get("oauth_token_file") or ""
    if tok_file:
        try:
            token = Path(os.path.expanduser(tok_file)).read_text(encoding="utf-8").strip()
            if token:
                extra["CLAUDE_CODE_OAUTH_TOKEN"] = token   # never logged
        except OSError:
            pass
    # An API key in the environment would win over the subscription login in -p mode and bill per token,
    # silently. Workers only get one when the config says so explicitly.
    drop = () if cfg.workers.get("allow_api_key") else ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    return clean_env(extra, drop=drop)


def _mtime(p: str) -> float | None:
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def classify(job: dict, scan: dict, timed_out: bool, err_offset: int = 0, result_before: float | None = None) -> str:
    """The outcome of THIS run. `scan` and the error text cover only what this run wrote: a resumed run
    appends to the previous run's files, whose last word was the very question that was answered. The
    result file counts only if this run wrote it (it did not exist before the launch, or changed since)."""
    if timed_out:
        return "timeout"
    text = ""
    now = _mtime(job["resultFile"])
    if now is not None and (result_before is None or now != result_before):
        text = read_shared(Path(job["resultFile"]))
    probe = text + "\n" + (scan.get("last_result") or "")
    iq, isum = probe.rfind("JARVIS-QUESTION"), probe.rfind("JARVIS-SUMMARY")
    if iq >= 0 and iq > isum:
        return "question"
    if isum >= 0:
        return "done"
    err = read_shared(Path(job["err"]))[err_offset:][-6000:]
    if AUTH_FAIL.search(probe + err):
        return "auth-failed"
    return "ended-early"


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def run(cfg: Config, n: int, resume: bool, runid: str | None = None) -> int:
    me = os.getpid()
    # Claim the record under its lock: it must still be THIS job, still waiting to launch. A watchdog that
    # starts late (after `start` gave up waiting, the job was stopped, or the slot was reused) does nothing.
    with FileLock(cfg.locks / f"s{n}.record.lock"):
        job = slots.load_job(cfg, n)
        if not job or (runid and job.get("runid") != runid) or job.get("status") != "launching":
            print(f"watchdog: slot {n} no longer holds a job waiting to launch "
                  f"({(job or {}).get('runid')}, {(job or {}).get('status')}) - nothing to do", file=sys.stderr)
            return 0
        job.update(watchPid=me, watchCreated=procs.created(me))
        slots.save_job(cfg, n, job)
    rid = job["runid"]
    slots.write_lock(cfg, n, me, rid)   # the lock follows the watchdog: it outlives the launcher
    stdin = Path(job["answerFile"] if resume else job["brief"])
    limit_min = int(job.get("timeoutMinutes") or 0)
    timeout_s = limit_min * 60 if limit_min > 0 else None   # None = no ceiling (the default)
    say(cfg, job, f"watchdog up (pid {me}) for slot {n}; {'resume' if resume else 'launch'}; "
                  f"{'ceiling ' + str(limit_min) + ' min' if timeout_s else 'no ceiling'}")

    log_offset, err_offset, result_before = _size(job["log"]), _size(job["err"]), _mtime(job["resultFile"])
    since = time.time()
    try:
        proc = procs.spawn(worker_argv(cfg, job, resume), cwd=Path(job["dir"]), env=worker_env(cfg, job),
                           stdin_path=stdin, stdout_path=Path(job["log"]), stderr_path=Path(job["err"]),
                           append=True)
    except (OSError, JarvisError) as e:
        say(cfg, job, f"could not launch the worker: {e}")
        job = slots.update_job(cfg, n, runid=rid, status="failed", endedAt=now_iso(), error=str(e))
        slots.ledger(cfg, "slot", job)
        slots.release_lock(cfg, n)
        return 1
    slots.update_job(cfg, n, runid=rid, pid=proc.pid, pidCreated=procs.created(proc.pid), status="running",
                     launchedAt=now_iso())
    say(cfg, job, f"worker pid {proc.pid} running in {job['dir']}")

    seen: dict[int, float | None] = {}

    def track() -> None:
        for p in procs.descendants(proc.pid).values():
            seen.setdefault(p.pid, p.created)

    timed_out = False
    while proc.poll() is None:
        if timeout_s and time.time() - since >= timeout_s:
            timed_out = True
            break
        try:
            proc.wait(timeout=POLL_S)
        except Exception:  # noqa: BLE001 - subprocess.TimeoutExpired: still running
            pass
        track()
    if timed_out:
        say(cfg, job, f"TIMEOUT after {limit_min} min - stopping the worker's tree")
        track()
        procs.kill_tree(proc.pid)
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            pass
    mins = round((time.time() - since) / 60)
    # the reaper: only what we SAW under this job, still the same process, and still alive
    for pid, born in seen.items():
        if procs.alive(pid, born):
            procs.kill(pid)
            say(cfg, job, f"reaped leftover pid {pid}")

    whole = transcript.scan(Path(job["log"]))                        # session id, cost, PRs: the whole job
    this_run = transcript.scan(Path(job["log"]), start=log_offset)   # the outcome: this run only
    status = classify(job, this_run, timed_out, err_offset, result_before)
    job = slots.update_job(cfg, n, runid=rid, status=status, endedAt=now_iso(), exitCode=proc.returncode,
                           sessionId=whole["session_id"] or job.get("sessionId"),
                           costUsd=whole["cost_usd"], prs=whole["prs"])
    if job.get("runid") != rid:
        say(cfg, job, f"slot {n} now holds {job.get('runid')} - this job's outcome ({status}) was not recorded there")
        return 0
    slots.ledger(cfg, "slot", job)
    if status == "question":
        say(cfg, job, f"slot {n} stays RESERVED: the job asked a question (python -m jarvis answer --slot {n} --text ...)")
    else:
        slots.release_lock(cfg, n)
    say(cfg, job, f"slot {n}: {status} after {mins} min; session {whole['session_id'] or '?'}; "
                  f"cost {whole['cost_usd'] if whole['cost_usd'] is not None else '?'} USD")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jarvis.watchdog")
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--runid")
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args(argv)
    cfg = load()
    try:
        return run(cfg, a.slot, a.resume, a.runid)
    except JarvisError as e:
        print(f"watchdog: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
