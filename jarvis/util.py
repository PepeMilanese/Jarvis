"""Small shared helpers: atomic JSON files, an exclusive lock file, time stamps, a clean environment."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: dict) -> None:
    """Write via a temp file + replace, so a reader never sees half a file. Retries briefly: on Windows a
    virus scanner or a concurrent reader can hold the target for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for _ in range(60):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_shared(path: Path, tail: int = 0) -> str:
    """Read a text file another process may be writing (never locks it). tail>0 = only the last N bytes."""
    try:
        with open(path, "rb") as f:
            if tail:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - tail))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def break_stale(path: Path, stale_s: float) -> bool:
    """Remove a lock file older than `stale_s` - atomically: it is RENAMED to a name only this process uses
    and then deleted, so two waiters can never both break the same lock and both believe they hold it."""
    try:
        if time.time() - path.stat().st_mtime <= stale_s:
            return False
        grave = path.with_name(f"{path.name}.stale-{os.getpid()}-{time.monotonic_ns()}")
        os.rename(path, grave)
        grave.unlink(missing_ok=True)
        return True
    except OSError:
        return False   # gone already, or another waiter won the rename: just try to take it again


class FileLock:
    """An exclusive lock file, held for milliseconds around a read-modify-write. One left behind by a
    killed process is broken after `stale_s`."""

    def __init__(self, path: Path, wait_s: float = 10, stale_s: float = 30) -> None:
        self.path, self.wait_s, self.stale_s = path, wait_s, stale_s

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.wait_s
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.close(fd)
                return self
            except FileExistsError:
                if break_stale(self.path, self.stale_s):
                    continue
                if time.time() > deadline:
                    from .config import JarvisError
                    raise JarvisError(f"{self.path.name} has been held for {self.wait_s:g} s - try again "
                                      f"(a lock older than {self.stale_s:g} s is broken automatically)") from None
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# What a running Claude Code session injects into the commands it runs. A child `claude` that inherited
# these would believe it is part of that session (its id, its effort level, its messaging socket). Only
# these are removed - your own settings (CLAUDE_CONFIG_DIR, CLAUDE_CODE_GIT_BASH_PATH, CLAUDE_CODE_USE_*)
# pass through untouched.
SESSION_MARKERS = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_CHILD_SESSION",
                   "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT", "CLAUDE_PID"}
SESSION_PREFIXES = ("CLAUDE_CODE_SESSION_", "CLAUDE_CODE_MESSAGING_", "CLAUDE_AGENT_SDK_")


def clean_env(extra: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment for a child claude: a plain one, not a child of the dispatcher's session. A
    session-injected ANTHROPIC_BASE_URL goes too (only when we are inside a session), plus any `drop`."""
    inside = "CLAUDECODE" in os.environ
    env = {k: v for k, v in os.environ.items()
           if k not in SESSION_MARKERS and not k.startswith(SESSION_PREFIXES)
           and not (inside and k == "ANTHROPIC_BASE_URL") and k not in drop}
    if extra:
        env.update(extra)
    return env


def one_line(text: str, width: int = 0) -> str:
    s = " ".join(str(text or "").split())
    return s if not width or len(s) <= width else s[: width - 3] + "..."
