"""Process helpers that are safe on a busy, long-running machine. Standard library only.

Two lessons are built in:

* **Never trust a parent pid alone.** On Windows a process keeps the pid of a parent that has long
  exited, and pids are reused - so `taskkill /T` (and any naive tree walk) can wander from a job into
  unrelated processes. In production one such walk started at a recycled pid and reached core system
  processes. Here a child counts only if it is YOUNGER than its parent, and on Windows only known
  tree-process names are ever stopped (the root excepted: the caller chose it).
* **A pid alone does not identify a process.** Records store the process's creation time next to its
  pid, and `alive(pid, created)` checks both.

The process table comes from the Toolhelp snapshot API on Windows, /proc on Linux and `ps` on macOS
(where creation times come from elapsed time, so a wall-clock step can shift them).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

IS_WIN = os.name == "nt"

# Windows: the only names a tree walk may stop below its root
TREE_NAMES = re.compile(
    r"^(cmd|claude|bun|node|py|pyw|python|python3|pythonw|powershell|pwsh|conhost|git|bash|sh|uv|uvx|gh|npm|npx|"
    r"pnpm|ffmpeg|timeout|ping|curl|rg|wscript)(\.exe)?$", re.I)


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    created: float | None   # unix seconds, None when unreadable (a protected system process)


# --- Windows -----------------------------------------------------------------------------------------
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    def _filetime(ft: "wintypes.FILETIME") -> float:
        return ((ft.dwHighDateTime << 32) + ft.dwLowDateTime - 116444736000000000) / 1e7

    def _created_win(pid: int) -> float | None:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            return round(_filetime(c), 3)
        finally:
            _k32.CloseHandle(h)

    def _alive_win(pid: int) -> bool:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)

    def _table_win() -> dict[int, Proc]:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            return {}
        out: dict[int, Proc] = {}
        try:
            e = _PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                pid = int(e.th32ProcessID)
                out[pid] = Proc(pid, int(e.th32ParentProcessID), e.szExeFile, None)
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        for p in out.values():
            p.created = _created_win(p.pid) if p.pid > 4 else None
        return out

    def _kill_win(pid: int) -> bool:
        h = _k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(_k32.TerminateProcess(h, 1))
        finally:
            _k32.CloseHandle(h)


# --- POSIX ---------------------------------------------------------------------------------------------
def _etime_s(s: str) -> int:
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return days * 86400 + h * 3600 + m * 60 + sec


_HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") and "SC_CLK_TCK" in getattr(os, "sysconf_names", {}) else 100


def _stat_linux(pid: int) -> Proc | None:
    """One /proc/<pid>/stat: ppid, name, and the start time in clock ticks since boot - immune to wall-clock
    steps (NTP, a resumed VM), unlike now - etime. `created` is then seconds since boot: comparable with
    every other `created` on this machine, which is all it is ever used for."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        head, tail = raw.rsplit(b")", 1)
        name = head.split(b"(", 1)[1].decode("utf-8", "replace")
        fields = tail.split()
        return Proc(pid, int(fields[1]), name, round(int(fields[19]) / _HZ, 2))   # fields: state ppid ... starttime(22)
    except (IndexError, ValueError):
        return None


def _table_linux() -> dict[int, Proc]:
    out: dict[int, Proc] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            p = _stat_linux(int(d))
            if p:
                out[p.pid] = p
    return out


def _table_posix() -> dict[int, Proc]:
    if os.path.isdir("/proc/self"):
        return _table_linux()
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,comm="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    t0 = time.time()
    table: dict[int, Proc] = {}
    for line in out.splitlines():
        f = line.split(None, 3)
        if len(f) < 4:
            continue
        try:
            pid, ppid, age = int(f[0]), int(f[1]), _etime_s(f[2])
        except ValueError:
            continue
        table[pid] = Proc(pid, ppid, os.path.basename(f[3].strip()), round(t0 - age, 0))
    return table


# --- the API -------------------------------------------------------------------------------------------
def process_table() -> dict[int, Proc]:
    return _table_win() if IS_WIN else _table_posix()


def created(pid: int) -> float | None:
    if not pid or pid <= 0:
        return None
    if IS_WIN:
        return _created_win(pid)
    if os.path.isdir("/proc/self"):
        p = _stat_linux(pid)
        return p.created if p else None
    p = process_table().get(pid)
    return p.created if p else None


def pid_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if IS_WIN:
        return _alive_win(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _zombie(pid)


def _zombie(pid: int) -> bool:
    """A killed process that nobody has reaped yet still answers kill(pid, 0) - it is dead all the same."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] == b"Z"   # the state follows the "(comm)" field
    except FileNotFoundError:
        return not os.path.isdir("/proc") and _zombie_ps(pid)
    except (OSError, IndexError):
        return False


def _zombie_ps(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z")


def alive(pid: int | None, created_at: float | None, slack_s: float = 2.0) -> bool:
    """True if `pid` is alive AND (when known) is the same process that was recorded - not a recycled pid."""
    if not pid_alive(pid):
        return False
    if created_at is None:
        return True
    now = created(int(pid))   # type: ignore[arg-type]
    return now is None or abs(now - float(created_at)) <= slack_s


def descendants(root: int, table: dict[int, Proc] | None = None) -> dict[int, Proc]:
    """Every live descendant of `root`, guarded: a child must be younger than its parent (a stale parent
    pid after pid reuse is not parentage) and, on Windows, carry a tree-process name."""
    table = table if table is not None else process_table()
    kids: dict[int, list[Proc]] = {}
    for p in table.values():
        kids.setdefault(p.ppid, []).append(p)
    out: dict[int, Proc] = {}
    queue = [root]
    while queue:
        parent = queue.pop()
        born = table[parent].created if parent in table else None
        for c in kids.get(parent, []):
            if c.pid in out or c.pid == root or c.pid == parent:
                continue
            if born is None or c.created is None or c.created + 1 < born:
                continue        # older than its "parent": a stale ppid, not ours
            if IS_WIN and not TREE_NAMES.match(c.name):
                continue
            out[c.pid] = c
            queue.append(c.pid)
    return out


def is_descendant(pid: int, root: int, table: dict[int, Proc] | None = None) -> bool | None:
    """Does `pid` sit under `root`? Walks UP the parent chain, stopping at a parent younger than its child
    (a stale ppid). None = could not tell (no process table)."""
    table = table if table is not None else process_table()
    if not table:
        return None
    cur, n = pid, 0
    while cur and n < 32:
        if cur == root:
            return True
        p = table.get(cur)
        if not p:
            return False
        parent = table.get(p.ppid)
        if parent and parent.created is not None and p.created is not None and parent.created > p.created + 1:
            return False
        cur, n = p.ppid, n + 1
    return False


def kill(pid: int) -> bool:
    if IS_WIN:
        return _kill_win(pid)
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def kill_tree(root: int, grace_s: float = 3.0, log=None) -> list[int]:
    """Stop `root` and its live descendants, leaves first, so nothing is re-parented mid-walk. Returns the
    pids it signalled. On POSIX the stragglers get SIGKILL after `grace_s`."""
    if not pid_alive(root):
        return []
    table = process_table()
    tree = descendants(root, table)
    order = sorted(tree.values(), key=lambda p: -(p.created or 0)) + ([table[root]] if root in table else [Proc(root, 0, "?", None)])
    done = []
    for p in order:
        if kill(p.pid):
            done.append(p.pid)
            if log:
                log(f"stopped {p.pid} {p.name}")
    if not IS_WIN and done:
        deadline = time.time() + grace_s
        while time.time() < deadline and any(pid_alive(p) for p in done):
            time.sleep(0.1)
        for p in done:
            if pid_alive(p):
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
    return done


# --- spawning --------------------------------------------------------------------------------------------
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def spawn(argv: list[str], *, cwd: Path, env: dict[str, str], stdin_path: Path | None, stdout_path: Path,
          stderr_path: Path, append: bool = False, detach: bool = False) -> subprocess.Popen:
    """Start a child whose std handles are FILES - never a pipe of ours.

    In production an inherited handle once made the dispatcher's own tool call wait two hours for a job to
    end: the launcher's child had inherited the pipe the dispatcher was reading. So: stdin from a file (or
    nothing), stdout/stderr to files, close_fds, and with `detach` a new session / process group that may
    also break away from a job object (a Windows tool runner can kill its whole job on exit)."""
    mode = "ab" if append else "wb"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    fin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    fout, ferr = open(stdout_path, mode), open(stderr_path, mode)
    try:
        kw: dict = dict(cwd=str(cwd), env=env, stdin=fin, stdout=fout, stderr=ferr, close_fds=True)
        if IS_WIN:
            flags = CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP if detach else 0)
            if detach:
                try:
                    return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
                except OSError:
                    pass   # the job does not allow breakaway (or there is none to break from)
            return subprocess.Popen(argv, creationflags=flags, **kw)
        return subprocess.Popen(argv, start_new_session=detach, **kw)
    finally:
        for f in (fin, fout, ferr):
            if f is not subprocess.DEVNULL:
                f.close()


def python_exe() -> str:
    """The interpreter to re-launch Jarvis with: this one, but the windowless pythonw.exe is avoided
    (it has no std handles, and the watchdog writes logs)."""
    exe = sys.executable
    if IS_WIN and Path(exe).name.lower() == "pythonw.exe":
        cand = Path(exe).with_name("python.exe")
        if cand.exists():
            return str(cand)
    return exe
