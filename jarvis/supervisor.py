r"""The supervisor: runs the dispatcher as a headless, self-healing Claude Code session.

    python -m jarvis dispatcher             run in the foreground (what the scheduled task / service runs)
    python -m jarvis dispatcher --once      one supervised session, no restart loop
    python -m jarvis dispatcher --dry-run   print what would run and check the prerequisites; launches nothing
    python -m jarvis dispatcher --status    is it running, which poller, the last log lines
    python -m jarvis dispatcher --stop      stop the supervisor, its session and the Telegram poller

The session is `claude -p --channels plugin:telegram@claude-plugins-official --input-format stream-json
--output-format stream-json` with stdin held open, so it lives as long as the supervisor wants it to:
the Telegram plugin injects each message as a turn, and the supervisor writes the durable schedule's
fires into the same stdin between turns. Every event is logged to logs/dispatcher-<stamp>.jsonl.

Every guard below is a real incident from production:

1. DEAF SESSION. Claude starts the plugin's poller when the process launches, but the session snapshots
   its MCP servers when it processes the FIRST message. A kickoff sent too early meets a plugin still
   `pending` - and such a session never receives a single channel message for its whole life (4 of 4
   morning restarts; one ignored its owner for 24 hours). So: wait for the poller's bot.pid, settle a few
   seconds, send the kickoff, read the `init` event, and kill + retry (with backoff) any session whose
   channel is not `connected` with the `reply` tool.
2. POLLER GONE. The poller process missing for 90 s while the session lives = deaf: restart.
3. STOLEN BOT. Telegram allows one getUpdates consumer per token, and any other Claude Code session opened
   with the plugin enabled starts its own poller and takes the bot - while "is the poller alive?" stays
   green (73 minutes of silence with a clean log). So the poller must be a DESCENDANT of this session,
   re-proven every 5 minutes (pids get reused), and stray pollers are swept before every launch.
4. LOGGED OUT. A refused token refresh wiped the CLI's login; every message got a 401 behind a channel
   that still said `connected`. So no session starts while `claude auth status` says logged out, a session
   that answers with the CLI's "Failed to authenticate" is killed, and the owner gets ONE alert straight
   through the Bot API (the session cannot talk without a login) - and one "back online" afterwards.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import procs
from .config import Config, JarvisError
from .util import break_stale, clean_env, read_json

RC_DEAF = 1000     # the session was killed as deaf / hung / robbed of its bot
RC_AUTH = 1001     # the session was killed because its login failed
RC_STOP = 130      # Ctrl+C

AUTH_FAIL_PREFIX = "Failed to authenticate"

DEFAULT_TIMINGS = {
    "poller_wait_s": 60,        # wait this long for the poller to come up before the kickoff
    "poller_settle_s": 3,       # then give the MCP handshake a moment
    "init_wait_s": 240,         # the init event must follow the kickoff within this, or the start hung
    "poller_gone_s": 90,        # poller missing this long while the session lives = deaf
    "rival_grace_s": 15,        # bot.pid naming someone else's poller this long = the bot was taken
    "recheck_s": 300,           # re-prove poller ownership this often (pid reuse)
    "watch_tick_s": 5,          # the live-session check interval
    "auth_poll_s": 60,          # while logged out, re-check this often
    "deaf_backoff_s": [15, 30, 60, 120, 300],
    "schedule_tick_s": 15,      # the durable schedule's check interval
    "idle_settle_s": 5,         # a turn ended this long ago, none started since = between turns
    "turn_stuck_s": 1800,       # a turn silent this long is taken as over
}

KICKOFF = (
    "You are now online as the Jarvis dispatcher (your operating manual is in your system prompt). This may "
    "be a restart: run `python -m jarvis status` once and note each slot's state. Do not create any "
    "CronCreate job: watch polls and every timed prompt come from the supervisor's durable schedule as "
    "messages that start with [jarvis-schedule], and they survived this restart. Send nothing to Telegram "
    "now. Telegram messages will arrive as <channel> events and each one is your cue. Reply to THIS message "
    "with exactly: dispatcher online"
)


def plugin_parts(channel: str) -> tuple[str, str]:
    """'plugin:telegram@claude-plugins-official' -> ('telegram', 'claude-plugins-official')."""
    m = re.fullmatch(r"plugin:([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+)", channel.strip())
    if not m:
        raise JarvisError(f"channel '{channel}' is not plugin:<name>@<marketplace>")
    return m.group(1), m.group(2)


class Turns:
    """Turn boundaries in the session's stream-json: every turn opens with a system/init event and closes
    with a result event. Fed by the pump thread; read by the schedule thread to write only between turns."""

    def __init__(self) -> None:
        self.inits = 0
        self.results = 0
        self.last_event = time.time()
        self.last_result = 0.0

    def see(self, line: bytes) -> None:
        self.last_event = time.time()
        if b'"subtype":"init"' not in line and b'"type":"result"' not in line:
            return
        try:
            o = json.loads(line)   # key order varies (a result line starts with "duration_api_ms"), so parse
        except ValueError:
            return
        if o.get("type") == "system" and o.get("subtype") == "init":
            self.inits += 1
        elif o.get("type") == "result":
            self.results += 1
            self.last_result = time.time()


class Session:
    """What the schedule may touch of the live session: its id, whether it is between turns, how many turns
    it has finished, and a locked write to its stdin."""

    def __init__(self, proc: subprocess.Popen, turns: Turns, session_id: str | None, timings: dict) -> None:
        self.proc, self.turns, self.t = proc, turns, timings
        self.id = session_id or f"pid-{proc.pid}"
        self._lock = threading.Lock()

    def results(self) -> int:
        return self.turns.results

    def idle(self) -> bool:
        t = self.turns
        if self.proc.poll() is not None or t.results < 1:   # gone, or the kickoff turn is still running
            return False
        if t.inits > t.results:
            return time.time() - t.last_event > self.t["turn_stuck_s"]
        return time.time() - t.last_result >= self.t["idle_settle_s"]

    def send(self, text: str) -> None:
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        with self._lock:
            if self.proc.poll() is not None or self.proc.stdin is None:
                raise OSError("the session has exited")
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()


class Supervisor:
    def __init__(self, cfg: Config, *, claude_argv: list[str] | None = None, sweep: bool = True,
                 alerts: bool = True, timings: dict | None = None, check_auth: bool = True, prewarm: bool = True,
                 echo: bool = True) -> None:
        self.cfg = cfg
        self.t = {**DEFAULT_TIMINGS, **(timings or {})}
        self.sweep_enabled, self.alerts_enabled, self.check_auth = sweep, alerts, check_auth
        self.prewarm_enabled, self.echo = prewarm, echo
        self._claude = claude_argv
        self.plugin, self.marketplace = plugin_parts(cfg.dispatcher["channel"])
        self.channel_server = f"plugin:{self.plugin}:{self.plugin}"
        self.tool_prefix = f"mcp__plugin_{self.plugin}_{self.plugin}__"
        self.reply_tool = self.tool_prefix + "reply"
        self.bot_pid_file = cfg.telegram_dir / "bot.pid"
        self.plugin_cache = Path.home() / ".claude" / "plugins" / "cache" / self.marketplace / self.plugin
        self.log_file = cfg.logs / "dispatcher.log"
        self.pid_file = cfg.logs / "dispatcher.pid"
        self.live: Session | None = None
        self.alerted = False

    # --- plumbing ----------------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        if self.echo:
            print(line, flush=True)
        self.cfg.logs.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def claude(self) -> list[str]:
        return list(self._claude) if self._claude else self.cfg.claude_argv()

    def env(self) -> dict[str, str]:
        env = clean_env()
        # channels need the claude.ai login: an inference-only setup token or an API key would shadow it
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        bun = Path.home() / ".bun" / "bin"
        if bun.exists() and str(bun).lower() not in env.get("PATH", "").lower():
            env["PATH"] = f"{bun}{os.pathsep}{env.get('PATH', '')}"
        # the plugin's start script runs `bun install` first: give the MCP connection two minutes
        env.setdefault("MCP_TIMEOUT", "120000")
        return env

    def render_manual(self) -> Path:
        """The manual with its placeholders filled: where Jarvis lives, which python, the projects."""
        src = self.cfg.manual
        if not src.exists():
            raise JarvisError(f"the dispatcher manual {src} is missing")
        # forward slashes: the dispatcher runs these through Bash (Git Bash on Windows), where they just work
        projects = "\n".join(f"- `{k}` = `{Path(os.path.expanduser(str(v))).as_posix()}`"
                             for k, v in self.cfg.projects.items()) or "- (none configured)"
        text = (src.read_text(encoding="utf-8-sig")
                .replace("{{ROOT}}", self.cfg.root.as_posix())
                .replace("{{PYTHON}}", Path(procs.python_exe()).as_posix())
                .replace("{{STATE}}", self.cfg.state.as_posix())
                .replace("{{SLOTS}}", str(self.cfg.slots))
                .replace("{{INBOX}}", (self.cfg.telegram_dir / "inbox").as_posix())
                .replace("{{PROJECTS}}", projects))
        out = self.cfg.logs / "DISPATCHER.rendered.md"
        self.cfg.logs.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return out

    def args(self, manual: Path) -> list[str]:
        d = self.cfg.dispatcher
        a = ["-p", "--channels", d["channel"], "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--permission-mode", d.get("permission_mode") or "bypassPermissions",
             "--append-system-prompt-file", str(manual), "-n", d.get("session_name") or "jarvis"]
        if d.get("model"):
            a += ["--model", d["model"]]
        if d.get("effort"):
            a += ["--effort", d["effort"]]
        for extra in d.get("add_dirs") or []:
            a += ["--add-dir", str(Path(os.path.expanduser(str(extra))))]
        return a

    def poller_pid(self) -> int:
        try:
            return int(self.bot_pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def find_pollers(self) -> list[int]:
        """Every process of the plugin's poller, whoever started it. The plugin's start script is TWO
        processes - `bun run --cwd <plugin> ... start` and the bare `bun server.ts` child it spawns - and only
        the parent carries the plugin path on its command line, yet the CHILD holds the token. Matching the
        path alone killed the wrapper and left the real poller running, so children are found by parentage."""
        marker = f"{self.marketplace}.{self.plugin}"
        if procs.IS_WIN:
            ps = ("$m = @(Get-CimInstance Win32_Process -Filter \"Name='bun.exe'\" | Where-Object { $_.CommandLine -match "
                  f"'{marker}' }}); $ids = @($m | ForEach-Object {{ $_.ProcessId }}); $k = @(); "
                  "if ($ids.Count) { $k = @(Get-CimInstance Win32_Process -Filter \"Name='bun.exe'\" | "
                  "Where-Object { $ids -contains $_.ParentProcessId }) }; ($k + $m) | ForEach-Object { $_.ProcessId }")
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                                     capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                return []
            return [int(x) for x in out.split() if x.strip().isdigit()]
        try:
            out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        rows = []
        for line in out.splitlines():
            f = line.split(None, 2)
            if len(f) == 3 and f[0].isdigit() and f[1].isdigit():
                rows.append((int(f[0]), int(f[1]), f[2]))
        rx = re.compile(re.escape(self.marketplace) + r"[/\\.]" + re.escape(self.plugin))
        parents = [p for p, _, cmd in rows if "bun" in cmd and rx.search(cmd)]
        kids = [p for p, pp, cmd in rows if pp in parents and "bun" in cmd]
        return kids + parents

    def sweep_pollers(self, why: str) -> None:
        if not self.sweep_enabled:
            return
        for pid in self.find_pollers():
            if procs.kill(pid):
                self.log(f"  {why}: stopped stray poller {pid}")

    def kill_session(self, proc: subprocess.Popen) -> None:
        procs.kill_tree(proc.pid, log=lambda m: self.log(f"  {m}"))
        self.sweep_pollers("after the session")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass

    def prewarm(self, env: dict[str, str]) -> None:
        """Run the plugin's `bun install` ahead of the session so its MCP server starts in seconds."""
        if not self.prewarm_enabled:
            return
        pkgs = sorted(self.plugin_cache.glob("*/package.json"), key=lambda p: p.stat().st_mtime) if self.plugin_cache.exists() else []
        bun = shutil.which("bun", path=env.get("PATH"))
        if not pkgs or not bun:
            self.log(f"prewarm: skipped ({'plugin not in the cache' if not pkgs else 'bun not on PATH'})")
            return
        try:
            r = subprocess.run([bun, "install", "--no-summary"], cwd=str(pkgs[-1].parent), env=env,
                               capture_output=True, text=True, timeout=180)
            self.log(f"prewarm: bun install in {pkgs[-1].parent.name} -> rc {r.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"prewarm: skipped ({type(e).__name__})")

    # --- login + alerts ------------------------------------------------------------------------------------
    def auth_status(self) -> bool | None:
        try:
            r = subprocess.run([*self.claude(), "auth", "status"], env=self.env(), capture_output=True, text=True, timeout=60)
            return bool(json.loads(r.stdout).get("loggedIn"))
        except Exception:  # noqa: BLE001 - a broken check must never stop the dispatcher
            return None

    def telegram_alert(self, text: str) -> None:
        """Send `text` to every allow-listed chat straight through the Bot API - for when the session itself
        cannot talk. sendMessage does not compete with the poller's getUpdates. Never logs the token."""
        if not self.alerts_enabled:
            self.log(f"  (alert suppressed) {text}")
            return
        token = ""
        try:
            for ln in (self.cfg.telegram_dir / ".env").read_text(encoding="utf-8").splitlines():
                if ln.startswith("TELEGRAM_BOT_TOKEN="):
                    token = ln.split("=", 1)[1].strip()
            chats = (read_json(self.cfg.telegram_dir / "access.json") or {}).get("allowFrom", [])
        except OSError as e:
            self.log(f"  telegram alert skipped: no bot config ({type(e).__name__})")
            return
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or token
        for chat in chats if token else []:
            try:
                data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()
                self.log("  telegram alert sent")
            except Exception as e:  # noqa: BLE001
                self.log(f"  telegram alert failed ({type(e).__name__})")

    def alert_once(self, text: str) -> None:
        if not self.alerted:
            self.telegram_alert(text)
            self.alerted = True

    def wait_for_login(self) -> None:
        if not self.check_auth or self.auth_status() is not False:
            return
        self.log(f"claude CLI is LOGGED OUT - not starting a session; run `claude auth login` on this machine "
                 f"(re-checking every {self.t['auth_poll_s']} s)")
        self.alert_once("Jarvis is DOWN: the Claude CLI on the host machine is logged out, so I cannot answer. "
                        "Run `claude auth login` there - I start again by myself within a minute.")
        while self.auth_status() is False:
            time.sleep(self.t["auth_poll_s"])
        self.log("claude CLI logged in again - starting the session")

    @staticmethod
    def auth_failure(o: dict) -> str | None:
        """The CLI's synthetic failure text if this event is one - never a Telegram message quoting the words."""
        if o.get("type") == "assistant" and (o.get("message") or {}).get("model") == "<synthetic>":
            for c in o["message"].get("content", []):
                if c.get("type") == "text" and c.get("text", "").startswith(AUTH_FAIL_PREFIX):
                    return c["text"]
        if o.get("type") == "result" and o.get("is_error") and str(o.get("result", "")).startswith(AUTH_FAIL_PREFIX):
            return str(o["result"])
        return None

    def channel_state(self, init: dict) -> tuple[str, list[str]]:
        status = next((m.get("status", "?") for m in init.get("mcp_servers", []) if m.get("name") == self.channel_server), "ABSENT")
        tools = [t for t in init.get("tools", []) if t.startswith(self.tool_prefix)]
        return status, tools

    # --- the schedule thread -------------------------------------------------------------------------------
    def schedule_loop(self) -> None:
        try:
            from . import schedule as durable
            runner = durable.Runner(self.cfg.schedule_file, self.cfg.jobs, log=self.log, pid_alive=procs.alive,
                                    chat_ids=self.allowed_chats)
        except Exception as e:  # noqa: BLE001 - a broken schedule costs the schedule, never the dispatcher
            self.log(f"schedule: DISABLED ({type(e).__name__}: {e})")
            return
        self.log(f"schedule: running ({self.cfg.schedule_file}, every {self.t['schedule_tick_s']} s)")
        last = ""
        while True:
            try:
                runner.tick(self.live)
                last = ""
            except Exception as e:  # noqa: BLE001
                if str(e) != last:
                    self.log(f"schedule: tick failed ({type(e).__name__}: {e})")
                    last = str(e)
            time.sleep(self.t["schedule_tick_s"])

    def allowed_chats(self) -> list[str]:
        return [str(c) for c in (read_json(self.cfg.telegram_dir / "access.json") or {}).get("allowFrom", [])]

    # --- one session -----------------------------------------------------------------------------------------
    def run_once(self) -> int:
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        out_path = self.cfg.logs / f"dispatcher-{stamp}.jsonl"
        err_path = self.cfg.logs / f"dispatcher-{stamp}.err.txt"
        manual = self.render_manual()
        env = self.env()
        self.log(f"starting claude (log {out_path.name})")
        self.prewarm(env)
        # any poller alive right now is someone else's - ours does not exist yet - and it would hold the token
        self.sweep_pollers("pre-start")
        state: dict = {"init": None, "auth_error": None}
        spawned = time.time()
        flags = procs.CREATE_NO_WINDOW if procs.IS_WIN else 0
        with err_path.open("wb") as err:
            proc = subprocess.Popen([*self.claude(), *self.args(manual)], cwd=str(self.cfg.root), env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, creationflags=flags)
        assert proc.stdin is not None and proc.stdout is not None
        turns = Turns()

        def pump() -> None:
            with out_path.open("wb") as out:
                for line in iter(proc.stdout.readline, b""):
                    out.write(line)
                    out.flush()
                    turns.see(line)
                    if state["init"] is None and b'"init"' in line:
                        try:
                            o = json.loads(line)
                            if o.get("type") == "system" and o.get("subtype") == "init":
                                state["init"] = o
                        except ValueError:
                            pass
                    if state["auth_error"] is None and AUTH_FAIL_PREFIX.encode() in line:
                        try:
                            state["auth_error"] = self.auth_failure(json.loads(line))
                        except ValueError:
                            pass

        pumper = threading.Thread(target=pump, daemon=True)
        pumper.start()

        def finish(verdict: int | None = None) -> int:
            self.live = None   # the schedule holds anything due until the next verified session
            pumper.join(timeout=10)
            # a session WE killed returns our verdict (deaf / auth), not the exit code the kill gave it -
            # otherwise the deaf backoff never applies and a deaf loop restarts as fast as a crash would
            if verdict is not None:
                return verdict
            return proc.returncode if proc.returncode is not None else -1

        # 1. wait for the poller: claude starts its MCP servers at launch; the plugin rewrites bot.pid as it connects
        seen_at = None
        while time.time() - spawned < self.t["poller_wait_s"] and proc.poll() is None:
            try:
                if self.bot_pid_file.stat().st_mtime >= spawned - 1:
                    seen_at = time.time() - spawned
                    break
            except OSError:
                pass
            time.sleep(0.25)
        if proc.poll() is not None:
            self.log(f"claude exited with code {proc.returncode} before the kickoff (see {err_path.name})")
            return finish()
        if seen_at is None:
            self.log(f"poller not up within {self.t['poller_wait_s']} s - sending the kickoff anyway; the init check decides")
        else:
            self.log(f"poller up after {seen_at:.1f} s (pid {self.poller_pid()}); settling {self.t['poller_settle_s']} s")
            time.sleep(self.t["poller_settle_s"])

        # 2. kickoff, then the init verdict
        try:
            proc.stdin.write((json.dumps({"type": "user", "message": {"role": "user", "content": KICKOFF}}) + "\n").encode())
            proc.stdin.flush()
        except OSError as e:
            self.log(f"could not send the kickoff ({e})")
            self.kill_session(proc)
            return finish(RC_DEAF)
        self.log(f"claude pid {proc.pid}; kickoff sent; stdin held open so the session stays alive")
        kicked = time.time()
        while time.time() - kicked < self.t["init_wait_s"] and proc.poll() is None and state["init"] is None:
            time.sleep(0.2)
        if proc.poll() is not None:
            self.log(f"claude exited with code {proc.returncode} right after the kickoff (see {err_path.name})")
            return finish()
        if state["init"] is None:
            self.log(f"no init event within {self.t['init_wait_s']} s of the kickoff - a hung start; killing it and retrying")
            self.kill_session(proc)
            return finish(RC_DEAF)
        status, tools = self.channel_state(state["init"])
        if status != "connected" or self.reply_tool not in tools:
            self.log(f"channel {status} at init with {len(tools)} channel tools -> a DEAF session (it would never "
                     f"receive a message); killing it and retrying")
            self.kill_session(proc)
            return finish(RC_DEAF)
        self.log(f"channel connected at init ({len(tools)} channel tools; session {state['init'].get('session_id')}) - dispatcher live")
        live_at, announced = time.time(), False

        # 3. live: the poller must stay alive AND ours
        mine = self.poller_pid()
        if mine and procs.is_descendant(mine, proc.pid) is False:
            self.log(f"bot.pid names poller {mine}, which is not ours - another session holds the bot; restarting")
            self.kill_session(proc)
            return finish(RC_DEAF)
        self.live = Session(proc, turns, state["init"].get("session_id"), self.t)
        gone_since = rival_since = None
        recheck_at = time.time() + self.t["recheck_s"]
        unknown_until = 0.0
        try:
            while proc.poll() is None:
                time.sleep(self.t["watch_tick_s"])
                if state["auth_error"]:
                    self.log(f"session login failed ({state['auth_error']}) -> it can hear Telegram but not answer; killing it")
                    self.alert_once(f"Jarvis is DOWN: {state['auth_error']}. If the host is logged out, run "
                                    "`claude auth login` there - I start again by myself.")
                    self.kill_session(proc)
                    return finish(RC_AUTH)
                if self.alerted and not announced and time.time() - live_at > 30:
                    self.alerted = False
                    self.telegram_alert("Jarvis is back online.")
                    announced = True
                p = self.poller_pid()
                if p and p == mine and procs.pid_alive(p):
                    gone_since = rival_since = None
                    if time.time() >= recheck_at:
                        recheck_at = time.time() + self.t["recheck_s"]
                        if procs.is_descendant(p, proc.pid) is False:
                            self.log(f"bot.pid {p} is alive but no longer ours (pid reuse) -> deaf; restarting")
                            self.kill_session(proc)
                            return finish(RC_DEAF)
                    continue
                if p and p != mine and procs.pid_alive(p):
                    # bot.pid moved: the plugin restarted OUR poller, or another session started one
                    if time.time() < unknown_until:
                        continue
                    own = procs.is_descendant(p, proc.pid)
                    if own is None:
                        unknown_until = time.time() + 30
                        continue
                    unknown_until = 0.0
                    if own:
                        self.log(f"our poller was replaced in-session (bot.pid {mine or '-'} -> {p}); adopting it")
                        mine, gone_since, rival_since = p, None, None
                        continue
                    rival_since = rival_since or time.time()
                    if time.time() - rival_since >= self.t["rival_grace_s"]:
                        self.log(f"another session's poller took the bot (bot.pid {p}, ours was {mine or '-'}) -> deaf; restarting")
                        self.kill_session(proc)
                        return finish(RC_DEAF)
                    continue
                rival_since = None
                gone_since = gone_since or time.time()
                if time.time() - gone_since >= self.t["poller_gone_s"]:
                    self.log(f"poller gone for {self.t['poller_gone_s']} s while the session lives -> deaf; restarting")
                    self.kill_session(proc)
                    return finish(RC_DEAF)
        except KeyboardInterrupt:
            self.log("Ctrl+C - closing stdin and stopping claude")
            try:
                proc.stdin.close()
                proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                self.kill_session(proc)
            finish()
            return RC_STOP
        rc = finish()
        self.log(f"claude exited with code {rc}")
        return rc

    # --- the loop -----------------------------------------------------------------------------------------------
    def main(self, once: bool = False) -> int:
        self.cfg.ensure_dirs()
        self.claim_instance()
        threading.Thread(target=self.schedule_loop, name="schedule", daemon=True).start()
        backoff, deaf = 10, 0
        try:
            while True:
                self.wait_for_login()
                started = time.time()
                rc = self.run_once()
                if once or rc == RC_STOP:
                    return 0 if rc != RC_STOP else RC_STOP
                if rc == RC_AUTH:
                    time.sleep(self.t["auth_poll_s"])
                    continue
                if rc == RC_DEAF:
                    wait = self.t["deaf_backoff_s"][min(deaf, len(self.t["deaf_backoff_s"]) - 1)]
                    deaf += 1
                    self.log(f"deaf/hung start #{deaf} - retrying in {wait} s")
                    time.sleep(wait)
                    continue
                deaf = 0
                if time.time() - started < 60:   # died at once: a config problem - do not spin
                    self.log(f"died within a minute - backing off {backoff} s (see the newest .err.txt)")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 600)
                else:
                    backoff = 10
                    time.sleep(5)
        except KeyboardInterrupt:
            return RC_STOP
        finally:
            try:
                self.pid_file.unlink()
            except FileNotFoundError:
                pass
            self.log("supervisor stopped")

    def claim_instance(self) -> None:
        """One supervisor per install, atomically: the pid file is created with O_EXCL, so two near-
        simultaneous starts cannot both pass (and then kill each other's poller). A pid file left by a
        supervisor that died is broken - by rename, so only one starter can break it."""
        me = os.getpid()
        rec = json.dumps({"pid": me, "created": procs.created(me)}).encode()
        for _ in range(5):
            try:
                fd = os.open(self.pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, rec)
                os.close(fd)
                return
            except FileExistsError:
                other = self.running_pid()
                if other and other != me:
                    raise JarvisError(f"a supervisor is already running (pid {other}) - "
                                      "`python -m jarvis dispatcher --stop` first") from None
                if not (read_json(self.pid_file) or {}) and time.time() - self.pid_file.stat().st_mtime < 2:
                    time.sleep(0.5)   # another starter is between create and write: let it finish
                    continue
                break_stale(self.pid_file, 0)
        raise JarvisError(f"could not claim {self.pid_file.name} - another supervisor is starting")

    def running_pid(self) -> int:
        rec = read_json(self.pid_file) or {}
        pid = int(rec.get("pid") or 0)
        return pid if pid and procs.alive(pid, rec.get("created")) else 0


# --- CLI ---------------------------------------------------------------------------------------------------------
def cmd_dispatcher(cfg: Config, a) -> int:
    sup = Supervisor(cfg)
    if a.status:
        pid = sup.running_pid()
        print(f"supervisor: {'running (pid ' + str(pid) + ')' if pid else 'not running'}")
        pp = sup.poller_pid()
        print(f"poller:     {'pid ' + str(pp) + (' (alive)' if procs.pid_alive(pp) else ' (gone)') if pp else 'none'}")
        if sup.log_file.exists():
            print("--- last supervisor lines ---")
            print("\n".join(sup.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]))
        return 0
    if a.stop:
        pid = sup.running_pid()
        if pid:
            procs.kill_tree(pid, log=lambda m: print(f"  {m}"))
            print(f"stopped supervisor pid {pid} and its tree")
        else:
            print("supervisor not running")
        sup.sweep_pollers("stop")
        try:
            sup.pid_file.unlink()
        except FileNotFoundError:
            pass
        print("note: the heal task (Windows) or the service (Linux) starts it again - to keep it off: "
              "Disable-ScheduledTask Jarvis-Dispatcher, Jarvis-Dispatcher-Heal | systemctl --user stop jarvis-dispatcher")
        return 0
    if a.dry_run:
        manual = sup.render_manual()
        argv = [*sup.claude(), *sup.args(manual)]
        print("would run:  " + subprocess.list2cmdline(argv))
        print(f"cwd:        {cfg.root}")
        print(f"manual:     {manual} (rendered from {cfg.manual})")
        print(f"bot.pid:    {sup.bot_pid_file}")
        print(f"plugin:     {'found in ' + str(sup.plugin_cache) if sup.plugin_cache.exists() else 'NOT in the plugin cache - /plugin install ' + cfg.dispatcher['channel'].split(':', 1)[1]}")
        print(f"supervisor: {'already running (pid ' + str(sup.running_pid()) + ')' if sup.running_pid() else 'not running'}")
        pp = sup.poller_pid()
        if pp and procs.pid_alive(pp) and not sup.running_pid():
            print(f"poller:     a Telegram poller is alive (pid {pp}) - a real start would stop it and take the bot over")
        print("(dry run: nothing launched, no poller touched)")
        return 0
    return sup.main(once=a.once)
