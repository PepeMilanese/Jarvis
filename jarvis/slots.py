"""Slots: the job records (jobs/s<N>.json), the slot locks (locks/s<N>.lock) and the ledger.

A slot is busy while its lock names a live process, while its record says `launching`/`running` with a
live watchdog or worker, or while its record says `question`: a job that asked something keeps its slot
(and its session) reserved until `answer` or `stop`.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import procs
from .config import Config, JarvisError
from .util import FileLock, append_jsonl, now_iso, read_json, write_json

ACTIVE = ("launching", "running")
TERMINAL = ("done", "timeout", "auth-failed", "ended-early", "stopped", "failed", "dry-run")
LAUNCH_GRACE_S = 120   # a `launching` record whose watchdog never reported is stale after this


def job_path(cfg: Config, n: int) -> Path:
    return cfg.jobs / f"s{n}.json"


def lock_path(cfg: Config, n: int) -> Path:
    return cfg.locks / f"s{n}.lock"


def load_job(cfg: Config, n: int) -> dict | None:
    return read_json(job_path(cfg, n))


def save_job(cfg: Config, n: int, job: dict) -> None:
    write_json(job_path(cfg, n), job)


def update_job(cfg: Config, n: int, runid: str | None = None, **changes) -> dict:
    """Read-modify-write under the record's own lock: the watchdog and `status` both write records. With
    `runid`, the change applies only if the slot still holds THAT job - a late writer (a slow `status`, a
    watchdog finishing after `cleanup` + a new `start`) must never stamp its job's facts onto the next one."""
    with FileLock(cfg.locks / f"s{n}.record.lock"):
        job = load_job(cfg, n) or {}
        if runid is not None and job.get("runid") != runid:
            return job
        job.update(changes)
        save_job(cfg, n, job)
        return job


def drop_job(cfg: Config, n: int) -> None:
    try:
        job_path(cfg, n).unlink()
    except FileNotFoundError:
        pass


def job_alive(job: dict) -> bool:
    return procs.alive(job.get("pid"), job.get("pidCreated"))


def watch_alive(job: dict) -> bool:
    return procs.alive(job.get("watchPid"), job.get("watchCreated"))


# --- locks -------------------------------------------------------------------------------------------------
def read_lock(cfg: Config, n: int) -> dict | None:
    return read_json(lock_path(cfg, n))


def write_lock(cfg: Config, n: int, pid: int, runid: str) -> None:
    write_json(lock_path(cfg, n), {"pid": pid, "created": procs.created(pid), "runid": runid, "ts": now_iso()})


def release_lock(cfg: Config, n: int) -> None:
    try:
        lock_path(cfg, n).unlink()
    except FileNotFoundError:
        pass


def busy_reason(cfg: Config, n: int) -> str | None:
    job = load_job(cfg, n)
    if job:
        st = job.get("status")
        if st == "question":
            return f"waiting for an answer ({job.get('runid')}) - use `answer` or `stop`"
        if st in ACTIVE:
            if job_alive(job) or watch_alive(job):
                return f"running ({job.get('runid')}) - `stop` it first"
            age = time.time() - (job_path(cfg, n).stat().st_mtime if job_path(cfg, n).exists() else 0)
            if st == "launching" and age < LAUNCH_GRACE_S:
                return f"launching ({job.get('runid')})"
    lk = read_lock(cfg, n)
    if lk and procs.alive(lk.get("pid"), lk.get("created")):
        return f"locked by pid {lk.get('pid')} ({lk.get('runid')})"
    return None


def new_runid(cfg: Config, n: int) -> str:
    """<yyyymmdd-hhmmss>-s<N>, never one already used: a job that ends within the second it started would
    otherwise hand its run id (and its transcript) to the next job on the same slot."""
    while True:
        runid = f"{time.strftime('%Y%m%d-%H%M%S')}-s{n}"
        if not (cfg.logs / f"{runid}-brief.md").exists() and not (cfg.work / runid).exists():
            return runid
        time.sleep(0.5)


def claim(cfg: Config, want: int = 0, runid_for=None) -> tuple[int, str]:
    """Pick a free slot (or validate `want`) and lock it with this process. Atomic: two `start` calls at
    the same moment never get the same slot. Returns (slot, runid)."""
    cfg.ensure_dirs()
    slots = [want] if want else list(range(1, cfg.slots + 1))
    if want and not 1 <= want <= cfg.slots:
        raise JarvisError(f"--slot must be 1..{cfg.slots}")
    reasons = []
    with FileLock(cfg.locks / "claim.lock"):
        for n in slots:
            why = busy_reason(cfg, n)
            if why:
                reasons.append(f"slot {n}: {why}")
                continue
            runid = runid_for(n) if runid_for else new_runid(cfg, n)
            write_lock(cfg, n, os.getpid(), runid)
            return n, runid
    raise JarvisError("no free slot - " + "; ".join(reasons) + " (a request beyond the slots waits until one frees)")


# --- ledger ------------------------------------------------------------------------------------------------
LEDGER_KEYS = ("runid", "slot", "mode", "status", "task", "dir", "branch", "sessionId", "costUsd", "answers",
               "startedAt", "endedAt", "prs")


def ledger(cfg: Config, kind: str, job: dict) -> None:
    row = {"ts": now_iso(), "kind": kind, **{k: job.get(k) for k in LEDGER_KEYS}}
    started, ended = job.get("launchedAt") or job.get("startedAt"), job.get("endedAt")
    if started and ended:
        from .util import parse_iso
        a, b = parse_iso(started), parse_iso(ended)
        if a and b:
            row["minutes"] = round((b - a).total_seconds() / 60)
    append_jsonl(cfg.ledger, row)


def read_ledger(cfg: Config) -> list[dict]:
    out = []
    try:
        for line in cfg.ledger.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out
