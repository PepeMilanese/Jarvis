"""The launcher: start / status / answer / stop / result / cleanup. The dispatcher calls these through
its Bash tool; every command returns in seconds.

  start    pick a free slot, prepare the job's folder, write the brief (a generated header + the worker
           contract), launch the detached watchdog, print ONE JSON line
  status   one row per slot, plus per-slot detail: transcript growth, last step, recent activity, result
  answer   resume a job that asked a JARVIS-QUESTION, in the same Claude session, with the owner's words
  stop     stop the watchdog first (so it cannot re-classify), then the job's process tree
  result   print a finished job's JARVIS-SUMMARY / JARVIS-QUESTION block
  cleanup  forget a finished job: drop its record, free the slot, remove its worktree (repo jobs)

Three kinds of job folder:
  general (default)  a fresh scratch folder work/<runid>/
  --dir PATH         the folder the owner named (a project, a data folder, ...)
  --repo PATH        an isolated git worktree of that repo on a new branch jarvis/<date>-<slug>, so
                     parallel slots never trample one checkout
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import procs, slots, transcript
from .config import VALID_EFFORT, Config, JarvisError
from .util import now_iso, one_line, parse_iso, read_shared

STOPWORDS = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "please", "can", "you", "my", "it", "is"}


# --- helpers ---------------------------------------------------------------------------------------------
def slugify(text: str, words: int = 5, width: int = 40) -> str:
    toks = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS][:words]
    return ("-".join(toks)[:width].strip("-")) or "job"


def git(args: list[str], cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def same_or_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def read_task(text: str | None, file: str | None) -> str:
    if file:
        return Path(file).read_text(encoding="utf-8-sig").strip()
    if text == "-":
        return sys.stdin.buffer.read().decode("utf-8-sig").strip()
    return (text or "").strip()


def auth_ok(cfg: Config) -> bool | None:
    """`claude auth status` -> True / False, None when it cannot tell (never block on None)."""
    try:
        r = subprocess.run([*cfg.claude_argv(), "auth", "status"], capture_output=True, text=True, timeout=60)
        return bool(json.loads(r.stdout).get("loggedIn"))
    except Exception:  # noqa: BLE001
        return None


def brief_header(c: dict) -> str:
    task = c["task"].replace("\r\n", "\n").replace("\n", "\n  ")
    where = {"general": "a scratch folder made for this job: put outputs here unless TASK says where",
             "dir": "the folder the owner named - your cwd",
             "repo": "an isolated git worktree on its own branch - your cwd"}[c["kind"]]
    lines = [
        "# Jarvis job header (generated)",
        f"- RUNID: {c['runid']}",
        f"- DATE: {c['date']}",
        f"- SLOT: {c['slot']}",
        f"- MODE: {'repo' if c['kind'] == 'repo' else 'general'}",
        f"- TASK: {task}",
        f"- WORKDIR: {c['dir']}   <- {where}",
    ]
    if c["kind"] == "repo":
        lines += [f"- REPO: {c['repo']}", f"- BRANCH: {c['branch']}   <- commit here; never push to the default branch",
                  f"- BASE: {c['base']}"]
    lines += [
        f"- HOME: {Path.home()}",
        f"- RESULT_FILE: {c['resultFile']}   <- write your JARVIS-SUMMARY or JARVIS-QUESTION block here AND print it as your last output",
        f"- STEPS_FILE: {c['stepsFile']}   <- append one short line per milestone (also in $JARVIS_STEPS)",
        f"- PROTECTED: {', '.join(c['protected']) or 'none'}   <- never work inside these",
        f"- BUDGET_USD: {c['budget'] if c['budget'] else 'none'}   TIMEOUT_MINUTES: {c['timeout'] or 'none'}",
        "", "---", "", ""]
    return "\n".join(lines)


def spawn_watchdog(cfg: Config, n: int, runid: str, resume: bool) -> int:
    argv = [procs.python_exe(), "-m", "jarvis.watchdog", "--slot", str(n), "--runid", runid] + (["--resume"] if resume else [])
    env = dict(os.environ)
    env["JARVIS_STATE"] = str(cfg.state)
    if cfg.source:
        env["JARVIS_CONFIG"] = str(cfg.source)
    p = procs.spawn(argv, cwd=cfg.root, env=env, stdin_path=None,
                    stdout_path=cfg.logs / f"{runid}-watch.out.txt", stderr_path=cfg.logs / f"{runid}-watch.err.txt",
                    append=True, detach=True)
    return p.pid


def wait_for_pid(cfg: Config, n: int, seconds: float = 20) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = slots.load_job(cfg, n) or {}
        if job.get("pid") or job.get("status") not in ("launching",):
            return job
        time.sleep(0.25)
    return slots.load_job(cfg, n) or {}


def emit(job: dict) -> None:
    keep = ("slot", "runid", "mode", "status", "dir", "branch", "pid", "log", "resultFile", "stepsFile")
    print(json.dumps({k: job.get(k) for k in keep}, ensure_ascii=False))


def protected_dirs(cfg: Config) -> list[Path]:
    out = []
    for d in cfg.workers.get("protected_dirs") or []:
        p = Path(os.path.expandvars(os.path.expanduser(str(d))))
        if p.exists():
            out.append(p.resolve())
    return out


# --- start ---------------------------------------------------------------------------------------------------
def prepare_repo(cfg: Config, repo: Path, n: int, runid: str, slug: str, base: str | None) -> tuple[Path, str, str]:
    top = git(["rev-parse", "--show-toplevel"], repo)
    if top.returncode != 0:
        raise JarvisError(f"--repo {repo} is not a git repository")
    repo = Path(top.stdout.strip())
    if git(["remote"], repo).stdout.strip():
        f = git(["fetch", "--quiet", "--prune"], repo, timeout=300)
        if f.returncode != 0:
            print(f"warning: git fetch failed ({one_line(f.stderr, 120)}) - using what is local", file=sys.stderr)
    if not base:
        for cand in ("origin/HEAD", "origin/main", "origin/master", "HEAD"):
            if git(["rev-parse", "--verify", "--quiet", cand], repo).returncode == 0:
                base = cand
                break
    if git(["rev-parse", "--verify", "--quiet", base or "HEAD"], repo).returncode != 0:
        raise JarvisError(f"base '{base}' does not resolve in {repo}")
    stem = f"jarvis/{datetime.now():%Y-%m-%d}-{slug}"
    branch = stem
    for cand in (stem, f"{stem}-s{n}", f"{stem}-{runid}"):
        local = git(["show-ref", "--verify", "--quiet", f"refs/heads/{cand}"], repo).returncode == 0
        remote = git(["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{cand}"], repo).returncode == 0
        if not local and not remote:
            branch = cand
            break
    else:
        raise JarvisError(f"branch {stem} and its fallbacks are all taken")
    wt = cfg.work / runid
    # --no-track: the new branch must NOT have the default branch as its upstream, or a plain `git push`
    # (push.default=upstream) or `git pull` from the worktree would aim at main
    r = git(["worktree", "add", "--no-track", "-b", branch, str(wt), base or "HEAD"], repo, timeout=300)
    if r.returncode != 0:
        raise JarvisError(f"git worktree add failed: {one_line(r.stderr, 300)}")
    return wt, branch, base or "HEAD"


def cmd_start(cfg: Config, a) -> int:
    task = read_task(a.task, a.task_file)
    if not task:
        raise JarvisError("start needs a task: --task TEXT, --task - (stdin) or --task-file PATH")
    budget = float(a.budget_usd if a.budget_usd is not None else cfg.workers["budget_usd"])
    timeout = int(a.timeout_minutes if a.timeout_minutes is not None else cfg.workers["timeout_minutes"])
    if budget < 0:
        raise JarvisError("--budget-usd must be >= 0 (0 = no cap)")
    if timeout < 0:
        raise JarvisError("--timeout-minutes must be >= 0 (0 = no ceiling)")
    model = (cfg.workers.get("model") or "") if a.model is None else a.model
    effort = (cfg.workers.get("effort") or "") if a.effort is None else a.effort
    if effort and effort not in VALID_EFFORT:
        raise JarvisError(f"--effort must be one of {', '.join(VALID_EFFORT)} (or empty)")
    if a.dir and a.repo:
        raise JarvisError("pass --dir or --repo, not both")
    contract = Path(a.contract) if a.contract else cfg.contract
    if not contract.exists():
        raise JarvisError(f"the worker contract {contract} is missing")
    guard = protected_dirs(cfg)
    if a.dir:
        target = Path(a.dir).expanduser()
        if not target.is_dir():
            raise JarvisError(f"--dir {a.dir} is not an existing folder")
        target = target.resolve()
        if target == cfg.root.resolve():
            raise JarvisError("a job never runs in the Jarvis folder itself (the dispatcher's home) - name a subfolder or use a scratch folder")
        for p in guard:
            if same_or_under(target, p):
                raise JarvisError(f"{target} is inside the protected folder {p}")
    # (--repo needs no such check: the job works in a NEW worktree under work/, never inside the checkout)
    if not a.dry_run and not a.skip_auth and not cfg.workers.get("oauth_token_file"):
        if auth_ok(cfg) is False:
            raise JarvisError("the claude CLI is logged out - run `claude auth login` on this machine")

    n, runid = slots.claim(cfg, a.slot or 0)
    try:
        slug = slugify(a.name or task)
        kind, branch, base, repo = ("dir" if a.dir else "repo" if a.repo else "general"), None, None, None
        if kind == "dir":
            wd = Path(a.dir).expanduser().resolve()
        elif kind == "repo":
            repo = Path(a.repo).expanduser().resolve()
            if a.dry_run:
                wd, branch, base = cfg.work / runid, f"jarvis/{datetime.now():%Y-%m-%d}-{slug}", a.base or "origin/HEAD"
            else:
                wd, branch, base = prepare_repo(cfg, repo, n, runid, slug, a.base)
        else:
            wd = cfg.work / runid
            if not a.dry_run:
                wd.mkdir(parents=True, exist_ok=True)
        files = {k: str(cfg.logs / f"{runid}{suffix}") for k, suffix in
                 (("brief", "-brief.md"), ("resultFile", "-result.md"), ("stepsFile", "-steps.txt"),
                  ("log", ".jsonl"), ("err", ".err.txt"))}
        ctx = {"runid": runid, "date": f"{datetime.now():%Y-%m-%d}", "slot": n, "task": task, "dir": str(wd),
               "kind": kind, "repo": str(repo) if repo else None, "branch": branch, "base": base,
               "protected": [str(p) for p in guard], "budget": budget, "timeout": timeout, **files}
        header = brief_header(ctx)
        cfg.logs.mkdir(parents=True, exist_ok=True)
        Path(files["brief"]).write_text(header + contract.read_text(encoding="utf-8-sig"), encoding="utf-8")
        job = {"runid": runid, "slot": n, "mode": "repo" if kind == "repo" else "general", "kind": kind, "task": task,
               "dir": str(wd), "scratch": kind == "general", "repo": ctx["repo"], "branch": branch, "base": base,
               "model": model, "effort": effort, "budgetUsd": budget, "timeoutMinutes": timeout,
               "status": "dry-run" if a.dry_run else "launching", "startedAt": now_iso(), "launchedAt": None,
               "endedAt": None, "pid": None, "watchPid": None, "sessionId": None, "costUsd": None, "answers": 0,
               "prs": [], **files}
        if a.dry_run:
            # nothing is recorded: a finished job's record (and its unreported result) stays as it was
            print(header.rstrip("-\n ").rstrip(), file=sys.stderr)
            print(f"(dry run: nothing launched or recorded; brief at {files['brief']})", file=sys.stderr)
            slots.release_lock(cfg, n)
            emit(job)
            return 0
        slots.save_job(cfg, n, job)
        wpid = spawn_watchdog(cfg, n, runid, resume=False)
        slots.write_lock(cfg, n, wpid, runid)
        emit(wait_for_pid(cfg, n))
        return 0
    except BaseException:
        job = slots.load_job(cfg, n)
        if job and job.get("runid") == runid and job.get("status") == "launching" and not slots.watch_alive(job):
            slots.update_job(cfg, n, runid=runid, status="failed", endedAt=now_iso())
        if not job or job.get("runid") != runid or job.get("status") in ("launching", "failed"):
            slots.release_lock(cfg, n)
        raise


# --- status --------------------------------------------------------------------------------------------------
def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h{(s % 3600) // 60:02d}m ago"


def _elapsed(job: dict) -> str:
    a = parse_iso(job.get("launchedAt") or job.get("startedAt"))
    if not a:
        return "-"
    b = parse_iso(job.get("endedAt")) or datetime.now()
    m = int((b - a).total_seconds() // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h{m % 60:02d}m"


def slot_view(cfg: Config, n: int) -> dict:
    job = slots.load_job(cfg, n)
    if not job:
        return {"slot": n, "status": "free"}
    alive, walive = slots.job_alive(job), slots.watch_alive(job)
    st = job.get("status", "?")
    shown = st
    if st == "running" and not alive:
        shown = "ending" if walive else "stale"
    scan = transcript.scan(Path(job.get("log", "")))
    changes = {}
    if scan["session_id"] and scan["session_id"] != job.get("sessionId"):
        changes["sessionId"] = scan["session_id"]
    if scan["cost_usd"] is not None and scan["cost_usd"] != job.get("costUsd"):
        changes["costUsd"] = scan["cost_usd"]
    if scan["prs"] and scan["prs"] != job.get("prs"):
        changes["prs"] = scan["prs"]
    if changes:
        job = slots.update_job(cfg, n, runid=job.get("runid"), **changes)
    res = read_shared(Path(job.get("resultFile", "")), tail=1500).strip()
    return {"slot": n, "status": shown, "alive": alive, "runid": job.get("runid"), "mode": job.get("mode"),
            "kind": job.get("kind"), "dir": job.get("dir"), "branch": job.get("branch"),
            "task": job.get("task", ""), "elapsed": _elapsed(job), "costUsd": job.get("costUsd"),
            "prs": job.get("prs") or [], "transcript": job.get("log"), "transcriptBytes": scan["size"],
            "transcriptMtime": scan["mtime"], "lastStep": transcript.last_line(Path(job.get("stepsFile", ""))),
            "recent": scan["recent"], "result": res, "answers": job.get("answers", 0)}


def cmd_status(cfg: Config, a) -> int:
    want = [a.slot] if a.slot else list(range(1, cfg.slots + 1))
    views = [slot_view(cfg, n) for n in want]
    if a.json:
        print(json.dumps(views, indent=1, ensure_ascii=False))
        return 0
    print(f"{'slot':<5}{'status':<12}{'alive':<7}{'elapsed':<9}{'cost':<9}{'runid':<21}task")
    for v in views:
        if v["status"] == "free":
            print(f"{v['slot']:<5}free")
            continue
        cost = f"{v['costUsd']:.2f}" if isinstance(v.get("costUsd"), (int, float)) else "-"
        print(f"{v['slot']:<5}{v['status']:<12}{'yes' if v['alive'] else '-':<7}{v['elapsed']:<9}{cost:<9}"
              f"{v['runid']:<21}{one_line(v['task'], 60)}")
    for v in views:
        if v["status"] == "free":
            continue
        where = f"{v['kind']} job in {v['dir']}" + (f" on branch {v['branch']}" if v.get("branch") else "")
        print(f"\n--- slot {v['slot']} - {v['status']} - {where}")
        size = v["transcriptBytes"]
        print(f"  transcript: {v['transcript']}  {size / 1024:.0f} KB, last write {_ago(v['transcriptMtime'])}")
        if v["lastStep"]:
            print(f"  last step:  {one_line(v['lastStep'], 160)}")
        for r in v["recent"]:
            print(f"  recent:     {one_line(r, 160)}")
        for pr in v["prs"]:
            print(f"  PR (opened by this job): {pr}")
        if v["result"]:
            print("  result:\n    " + "\n    ".join(v["result"].splitlines()[-14:]))
    if not a.slot and not a.no_quota:
        from . import quota
        print("\n" + quota.oneline(cfg))
    return 0


# --- answer / stop / result / cleanup ---------------------------------------------------------------------------
def need_job(cfg: Config, n: int) -> dict:
    if not n:
        raise JarvisError("--slot N is required")
    job = slots.load_job(cfg, n)
    if not job:
        raise JarvisError(f"slot {n} has no job")
    return job


def cmd_answer(cfg: Config, a) -> int:
    text = read_task(a.text, a.text_file)
    if not text:
        raise JarvisError("answer needs --text TEXT, --text - (stdin) or --text-file PATH")
    from .util import FileLock
    # check-and-set under the claim lock: two answers at once must not resume one session twice
    with FileLock(cfg.locks / "claim.lock"):
        job = need_job(cfg, a.slot)
        if job.get("status") != "question":
            raise JarvisError(f"slot {a.slot} is '{job.get('status')}', not waiting for an answer")
        if not job.get("sessionId"):
            raise JarvisError(f"slot {a.slot} has no session id to resume (see its transcript)")
        if slots.job_alive(job) or slots.watch_alive(job):
            raise JarvisError(f"slot {a.slot} still has a live process - `stop` it first")
        k = int(job.get("answers") or 0) + 1
        ans = cfg.logs / f"{job['runid']}-answer-{k}.md"
        ans.write_text(
            "The owner answered your JARVIS-QUESTION:\n\n" + text + "\n\nContinue from where you stopped. Your header, "
            "contract and RESULT_FILE are unchanged: end with a JARVIS-SUMMARY (or another JARVIS-QUESTION) block, "
            "written to RESULT_FILE and printed last.\n", encoding="utf-8")
        slots.update_job(cfg, a.slot, runid=job["runid"], status="launching", answers=k, answerFile=str(ans),
                         pid=None, endedAt=None)
        slots.write_lock(cfg, a.slot, os.getpid(), job["runid"])
    wpid = spawn_watchdog(cfg, a.slot, job["runid"], resume=True)
    slots.write_lock(cfg, a.slot, wpid, job["runid"])
    emit(wait_for_pid(cfg, a.slot))
    return 0


def cmd_stop(cfg: Config, a) -> int:
    job = need_job(cfg, a.slot)
    # the watchdog first: a watchdog that outlives its job would classify the kill as a result
    if slots.watch_alive(job):
        procs.kill(int(job["watchPid"]))
    if slots.job_alive(job):
        procs.kill_tree(int(job["pid"]), log=lambda m: print(f"  {m}"))
    job = slots.update_job(cfg, a.slot, runid=job["runid"], status="stopped", endedAt=now_iso())
    slots.ledger(cfg, "stop", job)
    slots.release_lock(cfg, a.slot)
    print(f"slot {a.slot} stopped; folder kept at {job.get('dir')} (`cleanup` forgets it)")
    return 0


def cmd_result(cfg: Config, a) -> int:
    job = need_job(cfg, a.slot)
    text = read_shared(Path(job.get("resultFile", ""))).strip()
    if not text:
        text = transcript.scan(Path(job.get("log", "")))["last_result"].strip()
    print(text or f"no result yet (status: {job.get('status')})")
    return 0


def cmd_cleanup(cfg: Config, a) -> int:
    job = need_job(cfg, a.slot)
    st = job.get("status")
    if st in slots.ACTIVE or st == "question":
        if not a.force or slots.job_alive(job) or slots.watch_alive(job):
            raise JarvisError(f"slot {a.slot} is '{st}' - `stop` it first (or answer it)")
    if slots.job_alive(job):
        raise JarvisError(f"slot {a.slot}'s job pid is still alive - `stop` it first")
    wd = Path(job.get("dir") or "")
    note = f"folder kept at {wd}"
    if job.get("kind") == "repo" and job.get("repo") and wd.exists():
        r = git(["worktree", "remove", *(["--force"] if a.force else []), str(wd)], Path(job["repo"]))
        if r.returncode != 0:
            raise JarvisError(f"the worktree has changes ({one_line(r.stderr, 160)}) - commit them, or pass --force")
        note = f"worktree removed; branch {job.get('branch')} kept"
    elif job.get("scratch") and a.force and wd.exists() and same_or_under(wd, cfg.work):
        shutil.rmtree(wd, ignore_errors=True)
        note = f"scratch folder {wd} deleted"
    slots.update_job(cfg, a.slot, runid=job["runid"], cleanedAt=now_iso())
    slots.ledger(cfg, "cleanup", slots.load_job(cfg, a.slot) or job)
    slots.drop_job(cfg, a.slot)
    slots.release_lock(cfg, a.slot)
    print(f"slot {a.slot} is free; {note}")
    return 0


def cmd_projects(cfg: Config, a) -> int:
    projects = cfg.projects
    if not projects:
        print("no projects configured ([projects] in jarvis.toml maps a name to a folder)")
    for name, path in projects.items():
        p = Path(os.path.expanduser(str(path)))
        print(f"{name:<20} {p}{'' if p.exists() else '   (missing)'}")
    return 0
