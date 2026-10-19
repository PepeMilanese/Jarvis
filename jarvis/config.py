"""Configuration: jarvis.toml (optional) on top of built-in defaults.

    JARVIS_CONFIG   path of the config file   (default: <repo>/jarvis.toml)
    JARVIS_STATE    folder for runtime state  (default: the repo root) - logs/, jobs/, locks/, work/,
                    schedule.json, ledger.jsonl. The tests point it at a temp folder.
    JARVIS_CLAUDE   the claude command, a path or a JSON list (default: `claude` from PATH). The tests
                    point it at tests/fake_claude.py so nothing ever talks to a model.

Relative paths in the config resolve against the repo root (code) or the state folder (runtime files).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    sys.exit("Jarvis needs Python 3.11 or newer (for tomllib).")

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "dispatcher": {
        "manual": "manual/DISPATCHER.md",
        "model": "",                      # empty = the CLI's default
        "effort": "",
        "permission_mode": "bypassPermissions",
        "channel": "plugin:telegram@claude-plugins-official",
        "session_name": "jarvis",
        "add_dirs": [],                   # extra folders the dispatcher may READ to answer questions
    },
    "workers": {
        "slots": 3,
        "model": "",
        "effort": "",
        "permission_mode": "bypassPermissions",
        "budget_usd": 0,                  # 0 = no cap (no --max-budget-usd); ceilings only ever killed working jobs
        "timeout_minutes": 0,             # 0 = no ceiling: the dispatcher stops a WEDGED job, not a timer
        "contract": "manual/WORKER.md",
        "add_home": True,                 # give general jobs the home folder via --add-dir
        "protected_dirs": [],             # folders a job may never run in (e.g. a checkout other sessions use)
        "oauth_token_file": "",           # optional file holding a CLAUDE_CODE_OAUTH_TOKEN for workers
        "allow_api_key": False,           # pass ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN to workers (pay-per-use)
    },
    "projects": {},                       # name = "path": lets the dispatcher resolve "the website" to a folder
    "telegram": {"state_dir": ""},        # default: $TELEGRAM_STATE_DIR or ~/.claude/channels/telegram
    "stt": {
        "model": "large-v3-turbo",
        "language": "auto",               # or an ISO code such as "en"; a forced language TRANSLATES other speech
        "candidates": [],                 # restrict auto-detection, e.g. ["mk", "en"]; empty = all
        "max_minutes": 10,
        "models_dir": "",                 # default: stt/models
    },
}

VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")


class JarvisError(Exception):
    """A refusal or failure with a message meant for the user (no traceback)."""


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict, source: Path | None, state: Path) -> None:
        self.data, self.source, self.root, self.state = data, source, ROOT, state

    # sections
    @property
    def dispatcher(self) -> dict:
        return self.data["dispatcher"]

    @property
    def workers(self) -> dict:
        return self.data["workers"]

    @property
    def stt(self) -> dict:
        return self.data["stt"]

    @property
    def projects(self) -> dict:
        return dict(self.data.get("projects") or {})

    # code paths
    def code_path(self, p: str) -> Path:
        q = Path(os.path.expandvars(os.path.expanduser(p)))
        return q if q.is_absolute() else ROOT / q

    @property
    def manual(self) -> Path:
        return self.code_path(self.dispatcher["manual"])

    @property
    def contract(self) -> Path:
        return self.code_path(self.workers["contract"])

    @property
    def stt_dir(self) -> Path:
        return ROOT / "stt"

    @property
    def stt_models(self) -> Path:
        d = self.stt.get("models_dir") or ""
        return self.code_path(d) if d else self.stt_dir / "models"

    # runtime paths (under the state folder)
    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def jobs(self) -> Path:
        return self.state / "jobs"

    @property
    def locks(self) -> Path:
        return self.state / "locks"

    @property
    def work(self) -> Path:
        return self.state / "work"

    @property
    def schedule_file(self) -> Path:
        return self.state / "schedule.json"

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.jsonl"

    @property
    def telegram_dir(self) -> Path:
        d = self.data["telegram"].get("state_dir") or os.environ.get("TELEGRAM_STATE_DIR") or ""
        return Path(os.path.expanduser(d)) if d else Path.home() / ".claude" / "channels" / "telegram"

    @property
    def slots(self) -> int:
        return max(1, int(self.workers.get("slots") or 3))

    def ensure_dirs(self) -> None:
        for d in (self.logs, self.jobs, self.locks, self.work):
            d.mkdir(parents=True, exist_ok=True)

    def claude_argv(self) -> list[str]:
        """The claude command as an argv prefix. JARVIS_CLAUDE wins (tests); else `claude` from PATH."""
        raw = os.environ.get("JARVIS_CLAUDE", "").strip()
        if raw:
            return [str(x) for x in json.loads(raw)] if raw.startswith("[") else [raw]
        exe = shutil.which("claude")
        if not exe:
            raise JarvisError("`claude` is not on PATH - install Claude Code first (https://claude.com/claude-code)")
        return [exe]


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("JARVIS_CONFIG") or ROOT / "jarvis.toml")
    data = copy.deepcopy(DEFAULTS)
    source = None
    if p.exists():
        try:
            data = _merge(DEFAULTS, tomllib.loads(p.read_text(encoding="utf-8-sig")))
        except tomllib.TOMLDecodeError as e:
            raise JarvisError(f"{p.name} is not valid TOML: {e}") from None
        source = p
    state = Path(os.environ.get("JARVIS_STATE") or ROOT).resolve()
    effort_w, effort_d = data["workers"].get("effort") or "", data["dispatcher"].get("effort") or ""
    for e in (effort_w, effort_d):
        if e and e not in VALID_EFFORT:
            raise JarvisError(f"effort '{e}' is not one of {', '.join(VALID_EFFORT)} (or empty for the CLI default)")
    return Config(data, source, state)
