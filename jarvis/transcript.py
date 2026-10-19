"""Read a worker's transcript: the stream-json that `claude -p --output-format stream-json --verbose` writes.

One pass, line by line, tolerant of a file that is still being written:

  system/init  -> session id (needed to resume the same session with `answer`)
  result       -> cost (summed over resumed runs) and the final text
  assistant    -> the last few text snippets (what `status` shows as recent activity)
  PRs          -> VERIFIED ONLY: the URL that the job's own `gh pr create` printed in its tool result.

Why so strict about PR numbers: in production, numbers scraped out of the agent's prose were wrong three
times in one morning - it had merely *read* those PRs. A number that the job itself created is a fact;
one it mentioned is a guess, and guesses do not get relayed to a phone as milestones.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text", "") if isinstance(c, dict) else c) for c in content)
    return ""


def scan(path: Path, recent: int = 3, start: int = 0) -> dict:
    """`start`: read from this byte offset - a resumed run appends to the same transcript, and its outcome
    must be judged on what IT wrote, not on the question the previous run ended with."""
    r: dict = {"session_id": None, "cost_usd": None, "last_result": "", "recent": [], "prs": [],
               "size": 0, "mtime": None, "results": 0}
    try:
        st = path.stat()
    except OSError:
        return r
    r["size"], r["mtime"] = st.st_size, st.st_mtime
    texts: list[str] = []
    pending: set[str] = set()     # tool_use ids of `gh pr create` calls, waiting for their results
    cost = 0.0
    seen_cost = False
    with open(path, "rb") as f:
        if start:
            f.seek(start - 1)
            if f.read(1) != b"\n":
                f.readline()   # the offset fell inside a line: skip the rest of it
        for raw in f:
            if not raw.startswith(b"{"):
                continue
            if b'"type":"user"' in raw and not pending:
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue      # a half-written last line
            t = o.get("type")
            if t == "system" and o.get("subtype") == "init" and not r["session_id"]:
                r["session_id"] = o.get("session_id")
            elif t == "assistant":
                for c in (o.get("message") or {}).get("content") or []:
                    if c.get("type") == "text" and c.get("text", "").strip():
                        texts.append(" ".join(c["text"].split()))
                    elif c.get("type") == "tool_use":
                        cmd = str((c.get("input") or {}).get("command") or "")
                        if re.search(r"\bgh\s+pr\s+create\b", cmd):
                            pending.add(c.get("id"))
            elif t == "user":
                for c in (o.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                        pending.discard(c.get("tool_use_id"))
                        if not c.get("is_error"):
                            for url in PR_URL.findall(_text(c.get("content"))):
                                if url not in r["prs"]:
                                    r["prs"].append(url)
            elif t == "result":
                r["results"] += 1
                r["last_result"] = str(o.get("result") or "")
                if isinstance(o.get("total_cost_usd"), (int, float)):
                    cost += float(o["total_cost_usd"])
                    seen_cost = True
    r["recent"] = texts[-recent:]
    r["cost_usd"] = round(cost, 4) if seen_cost else None
    return r


def last_line(path: Path) -> str:
    """The last non-empty line of a small text file (the worker's steps file)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = [ln.strip() for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""
