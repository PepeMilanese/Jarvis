r"""The dispatcher's durable schedule: timed prompts that survive restarts.

Why. A Claude Code session's own cron tool lives inside that ONE session, so the daily fresh-context
restart, every deaf-session retry and every supervisor restart wiped whatever it had scheduled. So the
schedule is a file, schedule.json in the state folder, and the supervisor (supervisor.py) fires every
due entry into the live session as a user message that starts with `[jarvis-schedule]`. A restart
loses nothing: the file is read again, and whatever came due while no session was up fires as soon as
one is.

    python -m jarvis schedule add --at "2026-10-20 02:00" --prompt "..." [--id NAME]   one-shot (--at 02:00 = the next 02:00)
    python -m jarvis schedule add --in 90m --prompt "..."                              one-shot, relative (s m h d: 45m, 1h30m)
    python -m jarvis schedule add --every 30m [--start 09:00] --prompt "..."           recurring interval
    python -m jarvis schedule add --cron "30 7 * * 1-5" --prompt "..."                  recurring, 5-field cron, local time
    python -m jarvis schedule list [--json]
    python -m jarvis schedule remove NAME [NAME ...]
    python -m jarvis schedule pause NAME | resume NAME
    python -m jarvis schedule history [-n 20]

  --prompt - reads the prompt from stdin (a quoted heredoc, so no shell quoting can mangle it);
  --prompt-file PATH reads it from a file; --replace overwrites an entry with the same id;
  --file PATH works on another schedule file (the tests use it). Times are the machine's local time.

The built-in entry `watch` is the watch loop (manual/DISPATCHER.md): the supervisor seeds it, it polls
every 4 minutes while a slot is launching or running, and once more whenever a slot changes state - so
the final `done` / `question` reaches the dispatcher too. It is never removed, only paused.

Delivery rules (class Runner, driven by the supervisor's schedule thread):
- It fires only into a session whose Telegram channel the supervisor verified, and only while that
  session is IDLE (between turns). A message written to stdin mid-turn is folded into the running turn:
  measured on Claude Code 2.1.293, a second prompt sent during a 20 s tool call was merged in and the
  first prompt's answer never came. So everything due at one tick goes out as ONE message.
- A fire stays pending until the session finishes a turn on it (its next `result` event). If that
  session ends first - the 06:45 restart, a deaf kill, a crash, a killed supervisor - the next session
  gets it again, marked RE-SENT, at most MAX_SENDS times in all. Watch polls are never re-sent.
- A recurring entry that missed several occurrences while nothing was up fires once, then keeps its
  rhythm. A one-shot fires late rather than never, and the message says how late.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

PREFIX = "[jarvis-schedule]"
WATCH_ID = "watch"
WATCH_EVERY_S = 240          # the watch poll while a slot is active (the manual's "every ~4 minutes")
WATCH_CHANGE_GAP_S = 60      # a slot that changed state is polled at the next tick, but never twice within this
MAX_SENDS = 3                # a fire no session finished a turn on is sent at most this many times in all
PENDING_STUCK_S = 1800       # a fire whose turn never showed up, in an idle session, is let go after this
LAUNCHING_GRACE_S = 600      # a `launching` record with no live pid counts as active for this long
MAX_PROMPT = 4000
ID_RX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
DUR_RX = re.compile(r"(\d+)\s*([smhd])")
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}

WATCH_PROMPT = (
    "Watch-loop poll. Run `python -m jarvis status` and report each NEW milestone per slot to the owner on "
    "Telegram exactly as 'The watch loop' in your manual says; if nothing changed since your last "
    "report, send nothing. Do not create a CronCreate job: this poll comes by itself every 4 minutes "
    "while a slot is launching or running, and once more whenever a slot changes state."
)


class ScheduleError(Exception):
    pass


# --- time -----------------------------------------------------------------------------------------
def now() -> datetime:
    return datetime.now().replace(microsecond=0)   # naive local time, like everything stored here


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")   # naive local -> "2026-10-20T02:00:00+02:00"


def parse_iso(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def show(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M")


def span(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 60:
        return f"{s}s"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def rel(dt: datetime, ref: datetime) -> str:
    secs = (dt - ref).total_seconds()
    return f"in {span(secs)}" if secs >= 0 else f"{span(secs)} ago"


def parse_duration(text: str) -> int:
    t = text.strip().lower().replace(" ", "")
    parts = DUR_RX.findall(t)
    if not parts or "".join(n + u for n, u in parts) != t:
        raise ScheduleError(f"'{text}' is not a duration - use s, m, h or d, e.g. 45m, 2h, 1h30m, 1d")
    total = sum(int(n) * UNIT_S[u] for n, u in parts)
    if total < 1:
        raise ScheduleError(f"'{text}' is zero")
    return total


def parse_when(text: str, ref: datetime) -> datetime:
    """'HH:MM' = the next such time (today, else tomorrow); 'YYYY-MM-DD HH:MM'; or ISO with an offset."""
    t = text.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ScheduleError(f"'{text}' is not a time of day")
        d = ref.replace(hour=h, minute=mi, second=0)
        return d if d > ref else d + timedelta(days=1)
    try:
        return parse_iso(t.replace(" ", "T", 1) if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d.*", t) else t).replace(microsecond=0)
    except ValueError:
        raise ScheduleError(f"'{text}' is not a time - use HH:MM, 'YYYY-MM-DD HH:MM' or ISO 8601") from None


# --- cron (5 fields, local time, Vixie day rule) ----------------------------------------------------
DOW_NAMES = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
MON_NAMES = {n: i + 1 for i, n in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def _cron_field(spec: str, lo: int, hi: int, names: dict[str, int]) -> set[int]:
    def num(x: str) -> int:
        x = x.lower()
        if x in names:
            return names[x]
        if not x.isdigit():
            raise ValueError(x)
        return int(x)

    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = num(x), num(y)
        else:
            a = num(part)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError(part)
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str):
    f = expr.split()
    if len(f) != 5:
        raise ScheduleError(f"cron '{expr}' needs 5 fields: minute hour day-of-month month day-of-week")
    try:
        mins = _cron_field(f[0], 0, 59, {})
        hours = _cron_field(f[1], 0, 23, {})
        doms = _cron_field(f[2], 1, 31, {})
        mons = _cron_field(f[3], 1, 12, MON_NAMES)
        dows = {d % 7 for d in _cron_field(f[4], 0, 7, DOW_NAMES)}   # 7 is Sunday too
    except ValueError as e:
        raise ScheduleError(f"cron '{expr}': bad field value {e}") from None
    return mins, hours, doms, mons, dows, f[2] == "*", f[4] == "*"


def cron_next(expr: str, after: datetime) -> datetime:
    """The first minute strictly after `after` that the expression matches."""
    mins, hours, doms, mons, dows, dom_star, dow_star = parse_cron(expr)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366 * 5)
    while t <= limit:
        if t.month not in mons:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        dom_ok, dow_ok = t.day in doms, (t.weekday() + 1) % 7 in dows
        if not ((dom_ok and dow_ok) if (dom_star or dow_star) else (dom_ok or dow_ok)):
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if t.hour not in hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in mins:
            t += timedelta(minutes=1)
            continue
        return t
    raise ScheduleError(f"cron '{expr}' never fires")


# --- the file -------------------------------------------------------------------------------------
class Locked:
    """An exclusive lock file beside the schedule. Held for milliseconds by the CLI and the supervisor;
    one left behind by a killed process is broken after 30 s."""

    def __init__(self, path: Path) -> None:
        self.lock = path.with_name(path.name + ".lock")

    def __enter__(self) -> "Locked":
        deadline = time.time() + 10
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.close(fd)
                return self
            except FileExistsError:
                from .util import break_stale   # rename-then-delete: two waiters never both break it
                if break_stale(self.lock, 30):
                    continue
                if time.time() > deadline:
                    raise ScheduleError(f"{self.lock.name} has been held for 10 s - is a schedule call stuck?") from None
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        try:
            self.lock.unlink()
        except OSError:
            pass


def load(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "entries": []}
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return {"version": 1, "entries": []}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ScheduleError(f"{path.name} is not valid JSON ({e}) - nothing fires until it is fixed") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ScheduleError(f"{path.name} has no 'entries' list - nothing fires until it is fixed")
    return data


def save(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for _ in range(40):   # a virus scanner can hold the target for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def history_path(path: Path) -> Path:
    return path.with_name("schedule-history.jsonl")


def record(path: Path, event: str, entry_id: str, **extra) -> None:
    row = {"ts": iso(now()), "event": event, "id": entry_id, **{k: v for k, v in extra.items() if v is not None}}
    try:
        with history_path(path).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass   # the history is an audit trail, never a reason to fail a fire


def find(data: dict, entry_id: str) -> dict | None:
    return next((e for e in data["entries"] if e.get("id") == entry_id), None)


def watch_entry() -> dict:
    return {"id": WATCH_ID, "kind": "watch", "every_s": WATCH_EVERY_S, "prompt": WATCH_PROMPT,
            "created": iso(now()), "builtin": True}


def due_at(e: dict) -> datetime | None:
    if e.get("kind") == "once":
        return parse_iso(e["at"])
    if e.get("kind") in ("every", "cron"):
        return parse_iso(e["next"])
    return None


def advance(e: dict, fired: datetime) -> None:
    """Move a recurring entry to its first occurrence after `fired`: missed occurrences fold into one."""
    if e["kind"] == "every":
        n, step = parse_iso(e["next"]), int(e["every_s"])
        if n <= fired:
            n += timedelta(seconds=step * (int((fired - n).total_seconds()) // step + 1))
        e["next"] = iso(n)
    elif e["kind"] == "cron":
        e["next"] = iso(cron_next(e["cron"], fired))


def describe(e: dict) -> str:
    k = e.get("kind")
    if k == "once":
        return f"once at {show(parse_iso(e['at']))}"
    if k == "every":
        return f"every {span(e['every_s'])}"
    if k == "cron":
        return f"cron '{e['cron']}'"
    if k == "watch":
        return f"watch: every {span(e['every_s'])} while a slot is active, and on a slot change"
    return str(k)


# --- the slots, as the watch entry sees them ---------------------------------------------------------
def slots(jobs_dir: Path, pid_alive: Callable[..., bool]) -> list[dict]:
    out = []
    for p in sorted(jobs_dir.glob("s[0-9].json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8-sig"))
            age = time.time() - p.stat().st_mtime
        except (OSError, ValueError):
            continue
        st = str(j.get("status") or "?")
        def up(pid_key: str, created_key: str) -> bool:
            if not j.get(pid_key):
                return False
            try:
                return pid_alive(int(j[pid_key]), j.get(created_key))
            except TypeError:   # a one-argument pid_alive
                return pid_alive(int(j[pid_key]))

        alive, walive = up("pid", "pidCreated"), up("watchPid", "watchCreated")
        active = (st == "running" and (alive or walive)) or (st == "launching" and (alive or walive or age < LAUNCHING_GRACE_S))
        task = " ".join(str(j.get("task") or "").split())
        out.append({"slot": int(j.get("slot") or p.stem[1:]), "runid": j.get("runid"), "status": st,
                    "active": active, "mode": j.get("mode") or "general", "task": task[:50] + ("..." if len(task) > 50 else "")})
    return out


def fingerprint(snap: Iterable[dict]) -> str:
    return "|".join(f"s{s['slot']}:{s['runid']}:{s['status']}:{int(s['active'])}" for s in snap)


def snapshot_line(snap: list[dict]) -> str:
    if not snap:
        return "no slot records"
    return " | ".join(f"s{s['slot']} {s['status']}{'' if s['active'] or s['status'] != 'running' else ' (pid gone)'} "
                      f"{s['mode']} {s['runid']} \"{s['task']}\"" for s in snap)


# --- the supervisor side ------------------------------------------------------------------------------
class Runner:
    """Fires due entries into the live dispatcher session. supervisor.py calls tick(session)
    every few seconds from its schedule thread; the tests drive it the same way with a fake session.

    `session` is None while no verified session is up, else an object with:
      .id        the claude session id (a new session = a new id)
      .idle()    True between turns (and never before the kickoff turn has finished)
      .results() how many turns the session has finished (`result` events)
      .send(t)   write one stream-json user message; raises when the session is gone
    """

    def __init__(self, path: Path, jobs_dir: Path, log: Callable[[str], None], pid_alive: Callable[..., bool],
                 chat_ids: Callable[[], list[str]], clock: Callable[[], datetime] = now) -> None:
        """`pid_alive(pid)` or `pid_alive(pid, created)` - the supervisor passes procs.alive, which also checks
        the recorded creation time, so a reused pid never keeps a finished slot "active"."""
        self.path, self.jobs_dir, self.log, self.pid_alive, self.chat_ids, self.clock = path, jobs_dir, log, pid_alive, chat_ids, clock
        self._error = ""

    def tick(self, session) -> str | None:
        """One pass. Returns the text it sent, or None."""
        if session is None:
            return None
        t = self.clock()
        items: list[tuple[dict, dict]] = []
        events: list[tuple] = []
        try:
            with Locked(self.path):
                data = load(self.path)
                dirty = False
                if find(data, WATCH_ID) is None:
                    data["entries"].insert(0, watch_entry())
                    events.append(("added", WATCH_ID, {"note": "built-in watch loop seeded by the supervisor"}))
                    dirty = True
                dirty |= self._settle(data, session, t, events)
                waiting = any((e.get("pending") or {}).get("session") == session.id for e in data["entries"])
                if not waiting and session.idle():
                    items = self._due(data, t)
                    dirty |= any(i.get("seen_only") for _, i in items)
                    items = [(e, i) for e, i in items if not i.get("seen_only")]
                if items:
                    fire = uuid.uuid4().hex[:8]
                    results_at = session.results()
                    for e, info in items:
                        self._mark(e, info, t, fire, session.id, results_at)
                    text = self._compose(items, t, fire)
                    dirty = True
                if dirty:
                    # pending is on disk BEFORE the send: a kill in between costs a re-send, never a lost entry
                    save(self.path, data)
        except ScheduleError as e:
            if str(e) != self._error:
                self.log(f"schedule: {e}")
                self._error = str(e)
            return None
        self._error = ""
        for ev, eid, extra in events:
            record(self.path, ev, eid, **extra)
        if not items:
            return None
        ids = [e["id"] for e, _ in items]
        try:
            session.send(text)
        except Exception as e:  # noqa: BLE001 - the session went away; the next one gets it (RE-SENT)
            self.log(f"schedule: could not write fire {fire} ({', '.join(ids)}) to the session ({type(e).__name__}) - "
                     f"the next session gets it again")
            return None
        for e, info in items:
            record(self.path, "fired", e["id"], fire=fire, session=session.id, due=info.get("due"),
                   late_s=info.get("late_s"), resent=info.get("resent"), why=info.get("why"))
        self.log(f"schedule: fired {', '.join(ids)} (fire {fire})")
        return text

    # a fire is pending until the session that got it finishes a turn; a session that ends first passes it on
    def _settle(self, data: dict, session, t: datetime, events: list) -> bool:
        dirty = False
        for e in list(data["entries"]):
            p = e.get("pending")
            if not p:
                continue
            if p.get("session") == session.id:
                if session.results() > int(p.get("results_at", 0)):
                    how = "done"
                elif session.idle() and (t - parse_iso(p["at"])).total_seconds() > PENDING_STUCK_S:
                    how = "unconfirmed"   # no turn ever showed up for it; do not block the schedule forever
                    self.log(f"schedule: no finished turn seen for fire {p.get('fire')} ({e['id']}) in "
                             f"{span(PENDING_STUCK_S)} - letting it go")
                else:
                    continue
                e.pop("pending", None)
                e.pop("sends", None)
                if e["kind"] == "once":
                    data["entries"].remove(e)
                events.append((how, e["id"], {"fire": p.get("fire"), "session": session.id}))
                dirty = True
                continue
            # sent to a session that is gone and never finished a turn on it
            e.pop("pending", None)
            dirty = True
            if e["kind"] == "watch":
                events.append(("dropped", e["id"], {"fire": p.get("fire"), "note": "watch polls are never re-sent"}))
                continue
            if int(e.get("sends", 1)) >= MAX_SENDS:
                e.pop("sends", None)
                if e["kind"] == "once":
                    e["failed"] = iso(t)
                self.log(f"schedule: {e['id']} was sent {MAX_SENDS} times and no session finished it - giving up "
                         f"({'kept as failed; remove it' if e['kind'] == 'once' else 'its next occurrence still fires'})")
                events.append(("failed", e["id"], {"fire": p.get("fire"), "sends": MAX_SENDS}))
                continue
            e["resend"] = {"first_at": p.get("first_at") or p["at"], "fire": p.get("fire")}
            events.append(("resend", e["id"], {"fire": p.get("fire"), "note": "its session ended before finishing a turn on it"}))
        return dirty

    def _due(self, data: dict, t: datetime) -> list[tuple[dict, dict]]:
        out = []
        for e in data["entries"]:
            if e.get("paused") or e.get("failed") or e.get("pending"):
                continue
            k = e.get("kind")
            if k == "watch":
                info = self._watch_due(e, t)
                if info:
                    out.append((e, info))
                continue
            if e.get("resend"):
                out.append((e, {"resent": True, "first_at": e["resend"]["first_at"], "why": "re-sent"}))
                continue
            try:
                d = due_at(e)
            except (KeyError, ValueError):
                continue
            if d is not None and d <= t:
                late = int((t - d).total_seconds())
                out.append((e, {"due": iso(d), "late_s": late if late >= 120 else None, "why": "due"}))
        return out

    def _watch_due(self, e: dict, t: datetime) -> dict | None:
        snap = slots(self.jobs_dir, self.pid_alive)
        fp = fingerprint(snap)
        active = any(s["active"] for s in snap)
        if e.get("seen") is None:          # first sight: remember, do not call it a change (the kickoff ran status)
            e["seen"] = fp
            return {"seen_only": True}
        since = (t - parse_iso(e["last"])).total_seconds() if e.get("last") else float("inf")
        # a slot that is new or changed is news; one that merely vanished (`cleanup` dropped its record) is not
        fresh = set(filter(None, fp.split("|"))) - set(filter(None, str(e["seen"]).split("|")))
        if fresh and since >= WATCH_CHANGE_GAP_S:
            return {"snap": snap, "fp": fp, "why": "a slot changed state"}
        if fp != e["seen"] and not fresh:
            e["seen"] = fp
            return {"seen_only": True}
        if active and since >= int(e.get("every_s", WATCH_EVERY_S)):
            return {"snap": snap, "fp": fp, "why": "a slot is active"}
        return None

    @staticmethod
    def _mark(e: dict, info: dict, t: datetime, fire: str, sid: str, results_at: int) -> None:
        e["pending"] = {"fire": fire, "at": iso(t), "session": sid, "results_at": results_at,
                        "first_at": info.get("first_at") or iso(t)}
        e["last"] = iso(t)
        if info.get("resent"):
            e["sends"] = int(e.get("sends", 1)) + 1
            e.pop("resend", None)
        else:
            e["sends"] = 1
            if e["kind"] in ("every", "cron"):
                advance(e, t)
        if e["kind"] == "watch":
            e["seen"] = info["fp"]

    def _compose(self, items: list[tuple[dict, dict]], t: datetime, fire: str) -> str:
        chats = [c for c in self.chat_ids() if c]
        chat = (f"Telegram chat_id for any report: {', '.join(chats)}." if chats else
                "Telegram chat_id unknown (access.json unreadable): use the one on the owner's latest message.")
        head = (f"{PREFIX} fire {fire} at {t:%H:%M} - from the supervisor's durable schedule (schedule.json), "
                f"not a message from the owner. {chat} Handle every entry below in this one turn, as its text "
                f"says. Never answer one with a CronCreate job: use `python -m jarvis schedule` (your manual).")
        parts = [head]
        for e, info in items:
            k = e["kind"]
            if k == "watch":
                label = f"watch poll - {info['why']}"
            elif info.get("resent"):
                label = (f"{describe(e)} - RE-SENT, send {int(e.get('sends', 2))} of {MAX_SENDS}: first sent at "
                         f"{parse_iso(info['first_at']):%H:%M} to a session that ended before finishing it, so check "
                         f"`python -m jarvis status` and what you already sent before acting twice")
            else:
                late = f", {span(info['late_s'])} late (nothing was up to fire it)" if info.get("late_s") else ""
                label = (f"one-shot, due {show(parse_iso(info['due']))}{late}" if k == "once" else
                         f"{describe(e)}, due {parse_iso(info['due']):%H:%M}{late}")
            body = e.get("prompt", "")
            if k == "watch":
                body += f"\nSlots now (the supervisor's read of jobs/): {snapshot_line(info['snap'])}"
            parts.append(f"--- entry \"{e['id']}\" ({label}) ---\n{body}")
        return "\n\n".join(parts)


# --- the CLI -------------------------------------------------------------------------------------------
def _prompt(args) -> str:
    if args.prompt_file:
        text = Path(args.prompt_file).read_text(encoding="utf-8-sig")
    elif args.prompt == "-":
        text = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        text = args.prompt or ""
    text = text.strip()
    if not text:
        raise ScheduleError("the prompt is empty - pass --prompt TEXT, --prompt - (stdin) or --prompt-file PATH")
    if len(text) > MAX_PROMPT:
        raise ScheduleError(f"the prompt is {len(text)} characters; keep it under {MAX_PROMPT} (point it at a file instead)")
    return text


def cmd_add(args, path: Path) -> int:
    t = now()
    whens = [w for w in (args.at, args.in_, args.every, args.cron) if w]
    if len(whens) != 1:
        raise ScheduleError("say exactly one of --at, --in, --every, --cron")
    prompt = _prompt(args)
    e: dict = {"prompt": prompt, "created": iso(t)}
    if args.at or args.in_:
        at = parse_when(args.at, t) if args.at else t + timedelta(seconds=parse_duration(args.in_))
        if at <= t:
            raise ScheduleError(f"{show(at)} is in the past")
        e.update(kind="once", at=iso(at))
        when = f"once at {show(at)} ({rel(at, t)})"
    elif args.every:
        step = parse_duration(args.every)
        if step < 60:
            raise ScheduleError("--every must be at least 1m (the supervisor checks every 15 s)")
        first = parse_when(args.start, t) if args.start else t + timedelta(seconds=step)
        e.update(kind="every", every_s=step, next=iso(first))
        when = f"every {span(step)}, first {show(first)} ({rel(first, t)})"
    else:
        expr = " ".join(args.cron.split())
        first = cron_next(expr, t)
        e.update(kind="cron", cron=expr, next=iso(first))
        when = f"cron '{expr}', first {show(first)} ({rel(first, t)})"
    eid = (args.id or "").strip().lower() or f"{e['kind']}-{t:%m%d-%H%M%S}"
    if not ID_RX.match(eid):
        raise ScheduleError(f"id '{eid}' - use 1-40 of a-z 0-9 . _ - (starting with a letter or digit)")
    if eid == WATCH_ID:
        raise ScheduleError("'watch' is the built-in watch loop - pick another id")
    e = {"id": eid, **e}
    if args.note:
        e["note"] = args.note
    with Locked(path):
        data = load(path)
        old = find(data, eid)
        if old and not args.replace:
            raise ScheduleError(f"an entry '{eid}' exists ({describe(old)}) - pass --replace or pick another id")
        if old:
            data["entries"].remove(old)
        data["entries"].append(e)
        save(path, data)
    record(path, "replaced" if old else "added", eid, schedule=describe(e))
    print(f"{'replaced' if old else 'added'} {eid}: {when}")
    return 0


def _row(e: dict, t: datetime) -> str:
    state = []
    if e.get("paused"):
        state.append("PAUSED")
    if e.get("failed"):
        state.append(f"FAILED {parse_iso(e['failed']):%d %b %H:%M} (sent {MAX_SENDS}x, never finished)")
    if e.get("pending"):
        state.append(f"pending since {parse_iso(e['pending']['at']):%H:%M} (fire {e['pending'].get('fire')})")
    if e.get("resend"):
        state.append("waiting to be RE-SENT")
    d = None
    try:
        d = due_at(e)
    except (KeyError, ValueError):
        pass
    nxt = f"next {show(d)} ({rel(d, t)})" if d else ""
    last = f"last {show(parse_iso(e['last']))}" if e.get("last") else "never fired"
    prompt = " ".join(str(e.get("prompt", "")).split())
    return (f"{e['id']:<18} {describe(e)}\n{'':<18} {'; '.join(x for x in (nxt, last, *state) if x)}\n"
            f"{'':<18} prompt: {prompt[:110]}{'...' if len(prompt) > 110 else ''}")


def cmd_list(args, path: Path) -> int:
    with Locked(path):
        data = load(path)
    if args.json:
        print(json.dumps(data, indent=1, ensure_ascii=False))
        return 0
    t = now()
    entries = data["entries"]
    if not entries:
        print(f"no entries in {path} (the supervisor seeds 'watch' when it starts)")
        return 0
    print(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in {path} at {show(t)}:")
    for e in entries:
        print(_row(e, t))
    return 0


def cmd_remove(args, path: Path) -> int:
    with Locked(path):
        data = load(path)
        gone = []
        for eid in args.ids:
            eid = eid.lower()
            if eid == WATCH_ID:
                raise ScheduleError("'watch' is the built-in watch loop - 'pause watch' instead")
            e = find(data, eid)
            if not e:
                raise ScheduleError(f"no entry '{eid}' (see: python -m jarvis schedule list)")
            data["entries"].remove(e)
            gone.append(e)
        save(path, data)
    for e in gone:
        record(path, "removed", e["id"], schedule=describe(e))
        print(f"removed {e['id']}: {describe(e)}")
    return 0


def cmd_pause(args, path: Path, pause: bool) -> int:
    with Locked(path):
        data = load(path)
        e = find(data, args.id.lower())
        if not e:
            raise ScheduleError(f"no entry '{args.id}' (see: python -m jarvis schedule list)")
        if pause:
            e["paused"] = True
        else:
            e.pop("paused", None)
            if e.get("kind") in ("every", "cron") and parse_iso(e["next"]) <= now():
                advance(e, now())   # resuming does not unleash the occurrences missed while paused
        save(path, data)
    record(path, "paused" if pause else "resumed", e["id"])
    print(f"{'paused' if pause else 'resumed'} {e['id']}: {describe(e)}")
    return 0


def cmd_history(args, path: Path) -> int:
    hp = history_path(path)
    if not hp.exists():
        print("no history yet")
        return 0
    lines = hp.read_text(encoding="utf-8", errors="replace").splitlines()[-args.n:]
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        extra = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts", "event", "id"))
        print(f"{r.get('ts', '?')[:19].replace('T', ' ')}  {r.get('event', '?'):<11} {r.get('id', '?'):<18} {extra}")
    return 0


def main(argv: list[str] | None = None, default_file: Path | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jarvis schedule", description="The dispatcher's durable schedule.")
    ap.add_argument("--file", default=str(default_file or Path("schedule.json")), help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--id", help="a name for the entry (1-40 of a-z 0-9 . _ -); default: made up from the time")
    a.add_argument("--at", help="once, at HH:MM (the next one) or 'YYYY-MM-DD HH:MM', local time")
    a.add_argument("--in", dest="in_", help="once, after a duration: 45m, 2h, 1h30m, 1d")
    a.add_argument("--every", help="recurring, every duration (at least 1m)")
    a.add_argument("--start", help="with --every: the first time, HH:MM or 'YYYY-MM-DD HH:MM'")
    a.add_argument("--cron", help="recurring, a 5-field cron expression in local time, e.g. '30 7 * * 1-5'")
    a.add_argument("--prompt", help="what the dispatcher should do then; '-' reads it from stdin")
    a.add_argument("--prompt-file", help="read the prompt from this file")
    a.add_argument("--note", help="a free-text note kept with the entry")
    a.add_argument("--replace", action="store_true", help="overwrite an existing entry with the same id")
    li = sub.add_parser("list")
    li.add_argument("--json", action="store_true")
    r = sub.add_parser("remove")
    r.add_argument("ids", nargs="+")
    for name in ("pause", "resume"):
        sub.add_parser(name).add_argument("id")
    h = sub.add_parser("history")
    h.add_argument("-n", type=int, default=20)
    args = ap.parse_args(argv)
    path = Path(args.file)
    try:
        if args.cmd == "add":
            return cmd_add(args, path)
        if args.cmd == "list":
            return cmd_list(args, path)
        if args.cmd == "remove":
            return cmd_remove(args, path)
        if args.cmd in ("pause", "resume"):
            return cmd_pause(args, path, args.cmd == "pause")
        return cmd_history(args, path)
    except ScheduleError as e:
        print(f"schedule: error: {e}", file=sys.stderr)
        return 2
