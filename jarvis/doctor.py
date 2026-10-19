"""`python -m jarvis doctor` - check every prerequisite, read-only. Never prints a token, an id or an email.

Exit 0 when nothing FAILs (WARN lines are advice: optional parts, or things worth tightening).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import procs
from .config import Config
from .stt import venv_python
from .supervisor import Supervisor, plugin_parts
from .util import read_json


class Report:
    def __init__(self) -> None:
        self.fails = 0
        self.warns = 0

    def line(self, level: str, what: str, detail: str = "") -> None:
        if level == "FAIL":
            self.fails += 1
        elif level == "WARN":
            self.warns += 1
        print(f"  [{level:<4}] {what}" + (f" - {detail}" if detail else ""))


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def cmd_doctor(cfg: Config, a) -> int:
    r = Report()
    print("Jarvis doctor\n\ncore")
    v = sys.version_info
    r.line("ok" if v >= (3, 11) else "FAIL", f"Python {v.major}.{v.minor}.{v.micro}", "" if v >= (3, 11) else "need 3.11+")
    r.line("ok", "config", str(cfg.source) if cfg.source else "no jarvis.toml - built-in defaults (copy jarvis.example.toml)")
    for label, p in (("dispatcher manual", cfg.manual), ("worker contract", cfg.contract)):
        r.line("ok" if p.exists() else "FAIL", label, str(p))
    try:
        cfg.ensure_dirs()
        with tempfile.NamedTemporaryFile(dir=cfg.logs, delete=True):
            pass
        r.line("ok", "state folder writable", str(cfg.state))
    except OSError as e:
        r.line("FAIL", "state folder writable", f"{cfg.state}: {e}")

    print("\nClaude Code")
    try:
        claude = cfg.claude_argv()
    except Exception as e:  # noqa: BLE001
        claude = []
        r.line("FAIL", "claude CLI", str(e))
    if claude:
        ver = _run([*claude, "--version"], 60)
        r.line("ok" if ver and ver.returncode == 0 else "FAIL", "claude CLI",
               (ver.stdout.strip() if ver and ver.returncode == 0 else "`claude --version` failed"))
        auth = _run([*claude, "auth", "status"], 60)
        try:
            logged = bool(json.loads(auth.stdout).get("loggedIn")) if auth else None
        except ValueError:
            logged = None
        r.line("ok" if logged else "FAIL" if logged is False else "WARN", "claude login",
               "logged in" if logged else "logged OUT - run `claude auth login`" if logged is False else "could not tell")
    keys = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if os.environ.get(k)]
    if keys:
        r.line("WARN", "API key in the environment",
               f"{', '.join(keys)} set - it would bill per token instead of your subscription; workers drop it "
               f"unless workers.allow_api_key = true{' (it is)' if cfg.workers.get('allow_api_key') else ''}")

    print("\nTelegram channel")
    bun = shutil.which("bun") or (str(Path.home() / ".bun" / "bin" / "bun") if (Path.home() / ".bun" / "bin").exists() else "")
    r.line("ok" if bun else "FAIL", "bun", "found" if bun else "the plugin's server runs on Bun - https://bun.sh")
    plugin, market = plugin_parts(cfg.dispatcher["channel"])
    cache = Path.home() / ".claude" / "plugins" / "cache" / market / plugin
    vers = sorted(p.name for p in cache.iterdir()) if cache.exists() else []
    r.line("ok" if vers else "FAIL", f"plugin {plugin}@{market}",
           f"installed ({vers[-1]})" if vers else f"not installed - in `claude`: /plugin install {plugin}@{market}")
    tdir = cfg.telegram_dir
    token = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    try:
        token = token or any(ln.startswith("TELEGRAM_BOT_TOKEN=") and ln.split("=", 1)[1].strip()
                             for ln in (tdir / ".env").read_text(encoding="utf-8").splitlines())
    except OSError:
        pass
    r.line("ok" if token else "FAIL", "bot token", "configured (not shown)" if token else f"none - in `claude`: /{plugin}:configure <token>")
    acc = read_json(tdir / "access.json") or {}
    allow = acc.get("allowFrom") or []
    policy = acc.get("dmPolicy", "pairing")
    r.line("ok" if allow else "FAIL", "allowlist", f"{len(allow)} sender(s) allowed" if allow else "nobody paired yet - DM the bot, then /telegram:access pair <code>")
    r.line("ok" if policy == "allowlist" else "WARN", "DM policy", policy if policy == "allowlist" else
           f"'{policy}' - once paired, lock it down: /telegram:access policy allowlist")
    sup = Supervisor(cfg, sweep=False, alerts=False)
    pp = sup.poller_pid()
    running = sup.running_pid()
    if running:
        r.line("ok", "dispatcher", f"supervisor running (pid {running})")
    elif pp and procs.pid_alive(pp):
        r.line("WARN", "dispatcher", "not running here, but a Telegram poller is alive - another session holds the bot "
                                     "(a dispatcher start would take it over)")
    else:
        r.line("WARN", "dispatcher", "not running - `python -m jarvis dispatcher` (or the scheduled task)")

    print("\noptional")
    for tool, why in (("git", "repo jobs (--repo) need it"), ("gh", "workers open PRs with it")):
        r.line("ok" if shutil.which(tool) else "WARN", tool, "found" if shutil.which(tool) else why)
    py = venv_python(cfg)
    model = cfg.stt_models / cfg.stt["model"] / "model.bin"
    r.line("ok" if py.exists() else "WARN", "voice memos: stt venv", str(py) if py.exists() else "run `python -m jarvis stt-setup`")
    r.line("ok" if model.exists() else "WARN", f"voice memos: model {cfg.stt['model']}",
           f"{model.stat().st_size / 1e9:.1f} GB" if model.exists() else "run `python -m jarvis stt-setup`")
    if os.name == "nt":
        for task in ("Jarvis-Dispatcher", "Jarvis-Dispatcher-Restart", "Jarvis-Dispatcher-Heal"):
            q = _run(["schtasks", "/query", "/tn", task], 30)
            r.line("ok" if q and q.returncode == 0 else "WARN", f"scheduled task {task}",
                   "registered" if q and q.returncode == 0 else "not registered - windows/install-tasks.ps1")
    print(f"\n{r.fails} FAIL, {r.warns} WARN")
    return 1 if r.fails else 0
