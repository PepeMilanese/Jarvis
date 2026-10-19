#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, so the tests never talk to a model and never start a Telegram poller.

    fake_claude.py --version                     prints a version
    fake_claude.py auth status                   prints {"loggedIn": <FAKE_LOGGED_IN, default true>}
    fake_claude.py -p --input-format stream-json ...     DISPATCHER mode (the supervisor's session)
    fake_claude.py -p ... < brief                         WORKER mode (a job)

DISPATCHER mode writes a bot.pid into $FAKE_BOT_DIR (as the plugin's poller would), then answers every
stdin message with one turn (system/init, assistant, result) and logs it to $FAKE_BOT_DIR/received.jsonl.
  FAKE_CHANNEL=pending        the init event reports the channel not connected (a deaf session)
  FAKE_NO_INIT=1              never emit an init event (a hung start)
  FAKE_AUTH_FAIL=1            answer the kickoff with the CLI's synthetic "Failed to authenticate"
  FAKE_POLLER=child           the "poller" is a child process (its pid goes into bot.pid) ...
  FAKE_POLLER_DIES_S=N        ... which exits after N seconds (poller gone)
  FAKE_RIVAL_PID=P, FAKE_RIVAL_AFTER_S=N   after N s, bot.pid is overwritten with pid P (a stolen bot)

WORKER mode reads the brief from stdin; keywords in it pick the behaviour:
  FAKE-QUESTION   first run ends with a JARVIS-QUESTION block; the resumed run (--resume) with a summary
  FAKE-SLEEP      sleeps 300 s (for `stop`)
  FAKE-ORPHAN     leaves a grandchild process running (for the watchdog's reaper)
  FAKE-SILENT     ends with no block at all (ended-early)
  FAKE-PR         "runs" gh pr create; its tool result carries a PR URL
  otherwise       writes a JARVIS-SUMMARY block
"""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

args = sys.argv[1:]


def emit(o: dict) -> None:
    sys.stdout.write(json.dumps(o, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if args[:1] == ["--version"]:
    print("9.9.9 (fake claude)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.environ.get("FAKE_LOGGED_IN", "true") == "true"}))
    sys.exit(0)

if "--input-format" in args:   # ---------------------------------------------------------- DISPATCHER
    root = Path(os.environ["FAKE_BOT_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    poller = None
    if os.environ.get("FAKE_POLLER") == "child":
        life = os.environ.get("FAKE_POLLER_DIES_S", "3600")
        poller = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({life})"])
        (root / "bot.pid").write_text(str(poller.pid), encoding="utf-8")
    else:
        (root / "bot.pid").write_text(str(os.getpid()), encoding="utf-8")
    rival_pid, rival_after = os.environ.get("FAKE_RIVAL_PID"), float(os.environ.get("FAKE_RIVAL_AFTER_S", "0"))
    if rival_pid:
        import threading

        def steal() -> None:
            time.sleep(rival_after)
            (root / "bot.pid").write_text(rival_pid, encoding="utf-8")

        threading.Thread(target=steal, daemon=True).start()
    status = os.environ.get("FAKE_CHANNEL", "connected")
    tools = ["mcp__plugin_telegram_telegram__reply", "mcp__plugin_telegram_telegram__react"] if status == "connected" else []
    for raw in iter(sys.stdin.buffer.readline, b""):
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        text = msg.get("message", {}).get("content", "")
        if os.environ.get("FAKE_NO_INIT") != "1":
            emit({"type": "system", "subtype": "init", "session_id": sid,
                  "mcp_servers": [{"name": "plugin:telegram:telegram", "status": status}], "tools": tools})
        with (root / "received.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "session": sid, "text": text}) + "\n")
        if os.environ.get("FAKE_AUTH_FAIL") == "1":
            emit({"type": "assistant", "session_id": sid, "message": {"model": "<synthetic>", "role": "assistant",
                  "content": [{"type": "text", "text": "Failed to authenticate. API Error: 401"}]}})
        else:
            emit({"type": "assistant", "session_id": sid, "message": {"model": "fake", "role": "assistant",
                  "content": [{"type": "text", "text": "ok"}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": sid})
    sys.exit(0)

# ------------------------------------------------------------------------------------------------------- WORKER
brief = sys.stdin.buffer.read().decode("utf-8", "replace")
resumed = "--resume" in args
sid = args[args.index("--resume") + 1] if resumed else str(uuid.uuid4())
result_file = Path(os.environ["JARVIS_RESULT"])
steps = os.environ.get("JARVIS_STEPS")


def step(text: str) -> None:
    if steps:
        with open(steps, "a", encoding="utf-8") as f:
            f.write(text + "\n")


Path(os.getcwd(), "worker-env.json").write_text(json.dumps({
    "argv": args,
    "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
    "has_session_marker": any(k in os.environ for k in ("CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID")),
    "kept_user_setting": os.environ.get("CLAUDE_CONFIG_DIR"),
    "git_ceiling": os.environ.get("GIT_CEILING_DIRECTORIES")}), encoding="utf-8")
emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd()})
emit({"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": "Reading the brief."}]}})
step("step 1: read the brief")
if resumed and "DIE-SILENTLY" in brief:          # a resumed run that crashes before writing anything
    sys.exit(1)
if resumed and "LOGIN-GONE" in brief:            # a resumed run whose login is gone
    sys.stderr.write("Invalid API key - Please run /login\n")
    sys.exit(1)
block = ""
if "FAKE-SLEEP" in brief:
    time.sleep(300)
elif "FAKE-ORPHAN" in brief:
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": False}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], **kw)
    (Path(os.getcwd()) / "orphan.pid").write_text(str(child.pid), encoding="utf-8")
    time.sleep(16)   # long enough for the watchdog to see it once
if "FAKE-PR" in brief:
    emit({"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [
        {"type": "text", "text": "I looked at https://github.com/acme/demo/pull/7 first."},
        {"type": "tool_use", "id": "toolu_pr", "name": "Bash", "input": {"command": "gh pr create --fill"}}]}})
    emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_pr", "content": "https://github.com/acme/demo/pull/42\n"}]}})
if "FAKE-QUESTION" in brief and not resumed:
    block = ("=== JARVIS-QUESTION ===\ntask: fake\nquestion: A or B?\ndefault-if-no-answer: A\n=== END-JARVIS-QUESTION ===")
elif "FAKE-SILENT" not in brief:
    answered = "answered" if resumed else "first run"
    block = (f"=== JARVIS-SUMMARY ===\nstatus: done\nchanged: none\nproof: fake proof ({answered})\n"
             f"notes: did the fake thing\n=== END-JARVIS-SUMMARY ===")
if block:
    result_file.write_text(block + "\n", encoding="utf-8")
    step("step 2: wrote the result")
Path(os.getcwd(), "worker-ran.txt").write_text(f"{sid}\n{'resumed' if resumed else 'fresh'}\n", encoding="utf-8")
emit({"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": block or "stopping"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": block or "stopping", "session_id": sid,
      "total_cost_usd": 0.0123})
