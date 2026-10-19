"""python -m jarvis <command> - run `python -m jarvis --help` for the list."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import JarvisError, load

USAGE = """\
python -m jarvis <command> [options]

  jobs        start      start a worker job in a free slot          (--task TEXT | --task-file F | --task -)
              status     every slot: state, transcript growth, last step, recent activity, result
              answer     resume a job that asked a question        (--slot N --text TEXT)
              stop       stop a job                                 (--slot N)
              result     print a job's summary or question block    (--slot N)
              cleanup    forget a finished job, free its slot       (--slot N [--force])
              projects   the named folders from jarvis.toml
  dispatcher  dispatcher run the supervised Telegram dispatcher      (--once | --dry-run | --status | --stop)
              schedule   the durable schedule                       (add | list | remove | pause | resume | history)
              quota      the Claude subscription's 5-hour and weekly windows
  voice       stt        transcribe an audio file offline           (<file> [--language en])
              stt-setup  build stt/.venv and fetch the pinned Whisper model
  setup       doctor     check every prerequisite (read-only)
"""


def build() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m jarvis", usage=USAGE, add_help=True)
    ap.add_argument("--version", action="version", version=f"jarvis {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("start")
    s.add_argument("--task")
    s.add_argument("--task-file")
    s.add_argument("--dir", help="run in this existing folder")
    s.add_argument("--repo", help="run in a fresh git worktree of this repo, on a new branch")
    s.add_argument("--base", help="--repo: the commit/branch to start from (default origin/HEAD, else HEAD)")
    s.add_argument("--slot", type=int, default=0)
    s.add_argument("--name", help="a short label for the branch / folder (default: from the task)")
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--budget-usd", type=float)
    s.add_argument("--timeout-minutes", type=int)
    s.add_argument("--contract", help="a different worker contract for this job (default manual/WORKER.md)")
    s.add_argument("--skip-auth", action="store_true", help="do not check `claude auth status` first")
    s.add_argument("--dry-run", action="store_true", help="prepare everything and print the header; launch nothing")

    st = sub.add_parser("status")
    st.add_argument("--slot", type=int, default=0)
    st.add_argument("--json", action="store_true")
    st.add_argument("--no-quota", action="store_true")

    an = sub.add_parser("answer")
    an.add_argument("--slot", type=int, required=True)
    an.add_argument("--text")
    an.add_argument("--text-file")

    for name in ("stop", "result"):
        sub.add_parser(name).add_argument("--slot", type=int, required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--slot", type=int, required=True)
    c.add_argument("--force", action="store_true")
    sub.add_parser("projects")

    d = sub.add_parser("dispatcher")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--stop", action="store_true")

    sub.add_parser("schedule", add_help=False)
    sub.add_parser("quota", add_help=False)

    t = sub.add_parser("stt")
    t.add_argument("path")
    t.add_argument("--language")
    t.add_argument("--model")
    t.add_argument("--candidates")
    t.add_argument("--max-minutes", type=float)
    ts = sub.add_parser("stt-setup")
    ts.add_argument("--model", action="append", help="model(s) to fetch (default: the configured one)")
    ts.add_argument("--skip-model", action="store_true")

    sub.add_parser("doctor")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # transcripts and tasks carry any character
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cfg = load()
        # pass-through commands keep their own argument parsers
        if argv and argv[0] == "schedule":
            from . import schedule
            cfg.ensure_dirs()
            return schedule.main(argv[1:], default_file=cfg.schedule_file)
        if argv and argv[0] == "quota":
            from . import quota
            return quota.main(argv[1:], cfg=cfg)
        a = build().parse_args(argv)
        if not a.cmd:
            print(USAGE)
            return 0
        from . import doctor, launcher, stt, supervisor
        handlers = {
            "start": launcher.cmd_start, "status": launcher.cmd_status, "answer": launcher.cmd_answer,
            "stop": launcher.cmd_stop, "result": launcher.cmd_result, "cleanup": launcher.cmd_cleanup,
            "projects": launcher.cmd_projects, "dispatcher": supervisor.cmd_dispatcher, "stt": stt.cmd_stt,
            "stt-setup": stt.cmd_stt_setup, "doctor": doctor.cmd_doctor,
        }
        return handlers[a.cmd](cfg, a)
    except JarvisError as e:
        print(f"jarvis: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
