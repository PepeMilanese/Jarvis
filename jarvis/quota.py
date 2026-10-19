r"""How much of the Claude subscription's 5-hour and weekly quota is used.

    python -m jarvis quota              the full readout, fetched now
    python -m jarvis quota --oneline    one line (what `status` appends; a result up to --max-age old is reused)
    python -m jarvis quota --json       the normalised figures

The source is the endpoint behind claude.ai's Settings > Usage and Claude Code's own /usage: GET
https://api.anthropic.com/api/oauth/usage with the CLI's claude.ai OAuth access token and the header
`anthropic-beta: oauth-2025-04-20` (the open-source OpenClaw project reads it the same way). The token is
the CLI's own login in ~/.claude/.credentials.json (Windows and Linux; on macOS it lives in the Keychain,
so the readout falls back to the session logs there).

The token is never printed, logged, cached or REFRESHED. Never refreshed, because Claude Code owns that
refresh-token family: a refresh by anything else rotates it under the CLI and logs the machine out (a
refused refresh is exactly how one production outage began). An expired token is simply not used - the
CLI renews it on its next call.

Fail soft. When the endpoint cannot answer, the newest `rate_limit_event` in a session log (the CLI writes
one per turn carrying the same two windows) is shown instead, labelled with the log's time; with neither,
one line says why. The exit code is 0 either way.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

LOGS = Path("logs")              # replaced by the configured logs folder (see oneline / main)
CACHE = LOGS / "quota-cache.json"
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"
HEADERS = {"Accept": "application/json", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
           "User-Agent": "jarvis-quota"}
TIMEOUT_S = 8


class Unavailable(Exception):
    pass


# --- the endpoint -----------------------------------------------------------------------------------
def read_login(path: Path = CREDENTIALS) -> tuple[str, str | None]:
    """(access token, plan label). Raises Unavailable with a reason that never contains the token."""
    try:
        oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError) as e:
        raise Unavailable(f"no CLI login to read ({type(e).__name__} on {path.name})") from None
    token = str(oauth.get("accessToken") or "")
    if not token:
        raise Unavailable("the CLI is not logged in to claude.ai (no access token)")
    if "user:profile" not in (oauth.get("scopes") or []):
        raise Unavailable("the CLI login lacks the user:profile scope the usage endpoint needs")
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp / 1000 < time.time() + 30:
        raise Unavailable(f"the CLI's login token expired at {datetime.fromtimestamp(exp / 1000):%H:%M} "
                          "(the CLI renews it on its next call; this tool never refreshes it)")
    plan = None
    sub = str(oauth.get("subscriptionType") or "").strip()
    if sub:
        tier = re.search(r"_(\d+x)$", str(oauth.get("rateLimitTier") or ""))
        plan = sub[:1].upper() + sub[1:] + (f" {tier.group(1)}" if tier else "")
    return token, plan


def fetch(token: str) -> dict:
    req = urllib.request.Request(URL, headers={**HEADERS, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = str((json.loads(e.read()).get("error") or {}).get("message") or "")[:120]
        except Exception:  # noqa: BLE001
            pass
        raise Unavailable(f"the usage endpoint answered HTTP {e.code}{': ' + msg if msg else ''}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unavailable(f"the usage endpoint did not answer ({getattr(e, 'reason', e)})") from None
    except ValueError:
        raise Unavailable("the usage endpoint's answer was not JSON") from None


def _minute(dt: datetime) -> str:
    # resets_at jitters around the minute (12:19:59.98 on one call, 12:20:00.16 on the next): round it
    return datetime.fromtimestamp(round(dt.timestamp() / 60) * 60).astimezone().isoformat(timespec="seconds")


def _when(s) -> str | None:
    if not s:
        return None
    try:
        return _minute(datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except ValueError:
        return None


def normalise(raw: dict, plan: str | None) -> dict:
    windows = []

    def window(key: str, label: str, w) -> None:
        if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
            return
        windows.append({"key": key, "label": label, "used": float(w["utilization"]), "resets_at": _when(w.get("resets_at")),
                        "locked": w.get("locked_reason")})

    window("five_hour", "5-hour window", raw.get("five_hour"))
    window("seven_day", "week, all models", raw.get("seven_day"))
    window("seven_day_opus", "week, Opus", raw.get("seven_day_opus"))
    window("seven_day_sonnet", "week, Sonnet", raw.get("seven_day_sonnet"))
    for lim in raw.get("limits") or []:   # model-scoped weekly limits (e.g. one model's own weekly cap)
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped" or not isinstance(lim.get("percent"), (int, float)):
            continue
        model = ((lim.get("scope") or {}).get("model") or {})
        name = model.get("display_name") or model.get("id")
        if name and not any(w["label"].lower() == f"week, {name}".lower() for w in windows):
            windows.append({"key": f"scoped:{name}", "label": f"week, {name}", "used": float(lim["percent"]),
                            "resets_at": _when(lim.get("resets_at")),
                            "severity": lim.get("severity") if lim.get("severity") not in (None, "normal") else None})
    sev = {lim.get("kind"): lim.get("severity") for lim in raw.get("limits") or [] if isinstance(lim, dict)}
    for w, kind in ((w, {"five_hour": "session", "seven_day": "weekly_all"}.get(w["key"])) for w in windows):
        if kind and sev.get(kind) not in (None, "normal"):
            w["severity"] = sev[kind]
    extra = raw.get("extra_usage") if isinstance(raw.get("extra_usage"), dict) else None
    rows = ((raw.get("seven_day_breakdown") or {}).get("rows")) or []
    return {"source": "endpoint", "as_of": datetime.now().astimezone().isoformat(timespec="seconds"), "plan": plan,
            "windows": windows,
            "extra_usage": None if extra is None else {
                "enabled": bool(extra.get("is_enabled")), "used_minor": extra.get("used_credits"),
                "limit_minor": extra.get("monthly_limit"), "currency": extra.get("currency") or "USD"},
            "breakdown": [{"name": r.get("display_name") or r.get("key"), "pct": r.get("percent")} for r in rows
                          if isinstance(r, dict) and r.get("percent")]}


# --- the fallback: the newest rate_limit_event a session wrote ------------------------------------------
def from_session_logs(logs: Path = LOGS, files: int = 12, tail: int = 2_000_000) -> dict:
    cands = sorted((p for p in logs.glob("*.jsonl") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)[:files]
    for p in cands:
        try:
            with p.open("rb") as f:
                f.seek(max(0, p.stat().st_size - tail))
                chunk = f.read()
        except OSError:
            continue
        for line in reversed(chunk.splitlines()):
            if b'"rate_limit_event"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            uw = ((o.get("rate_limit_info") or {}).get("unifiedWindows")) or {}
            windows = []
            for key, label in (("five_hour", "5-hour window"), ("seven_day", "week, all models")):
                w = uw.get(key) or {}
                if isinstance(w.get("utilization"), (int, float)):
                    ra = w.get("resetsAt")
                    windows.append({"key": key, "label": label, "used": round(float(w["utilization"]) * 100, 1),
                                    "resets_at": _minute(datetime.fromtimestamp(ra)) if ra else None})
            if windows:
                return {"source": "session-log", "as_of": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                        "log": p.name, "plan": None, "windows": windows, "extra_usage": None, "breakdown": []}
    raise Unavailable("no rate_limit_event in the newest session logs")


# --- one call ---------------------------------------------------------------------------------------
def get(max_age: float = 0, cache: Path = CACHE, credentials: Path = CREDENTIALS, logs: Path = LOGS) -> dict:
    if max_age > 0:
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - float(c["fetched_at"]) <= max_age:
                return c["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        token, plan = read_login(credentials)
        data = normalise(fetch(token), plan)
        del token
        try:
            cache.write_text(json.dumps({"fetched_at": time.time(), "data": data}), encoding="utf-8")   # figures only, never the token
        except OSError:
            pass
        return data
    except Unavailable as e:
        why = str(e)
    try:
        data = from_session_logs(logs)
        data["note"] = why
        return data
    except Unavailable as e:
        return {"source": None, "windows": [], "note": f"{why}; {e}"}


# --- rendering (ASCII only: a Windows console pipe may be cp1252) -------------------------------
def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).replace(tzinfo=None) if s else None


def _left(dt: datetime | None, ref: datetime) -> str:
    if not dt:
        return ""
    s = int((dt - ref).total_seconds())
    if s <= 0:
        return "now"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return f"in {d}d{h:02d}h" if d else (f"in {h}h{s // 60:02d}m" if h else f"in {s // 60}m")


def _pct(x: float) -> str:
    return f"{x:.0f}%" if abs(x - round(x)) < 0.05 else f"{x:.1f}%"


def _reset(dt: datetime | None, ref: datetime, short: bool) -> str:
    if not dt:
        return "no reset time (window not started)"
    same_day = dt.date() == ref.date()
    return dt.strftime("%H:%M" if short and same_day else "%a %d %b %H:%M")


def render_full(d: dict) -> str:
    ref = datetime.now()
    if not d.get("windows"):
        return f"Claude quota: unavailable - {d.get('note', 'no source answered')}"
    if d["source"] == "endpoint":
        head = (f"Claude quota - {d.get('plan') or 'subscription'} (the claude.ai login on this machine), read "
                f"{_dt(d['as_of']):%H:%M:%S} from api.anthropic.com/api/oauth/usage (what claude.ai Settings > Usage shows)")
    else:
        head = (f"Claude quota - from the newest session log ({d.get('log')}, written {_dt(d['as_of']):%a %d %b %H:%M}), "
                f"NOT live: {d.get('note')}")
    width = max(len(w["label"]) for w in d["windows"])
    lines = [head]
    for w in d["windows"]:
        rs = _dt(w.get("resets_at"))
        flag = "".join(f"  [{x}]" for x in (w.get("severity"), w.get("locked") and f"locked: {w['locked']}") if x)
        lines.append(f"  {w['label']:<{width}}  {_pct(w['used']):>5} used  {_pct(max(0.0, 100 - w['used'])):>5} left  "
                     f"resets {_reset(rs, ref, False)}{' (' + _left(rs, ref) + ')' if rs else ''}{flag}")
    ex = d.get("extra_usage")
    if ex is not None:
        if ex["enabled"] and isinstance(ex.get("used_minor"), (int, float)):
            lim = ex.get("limit_minor")
            lines.append(f"  extra usage: on, {ex['used_minor'] / 100:.2f} {ex['currency']} used this month"
                         + (f" of {lim / 100:.2f}" if isinstance(lim, (int, float)) else ""))
        else:
            lines.append(f"  extra usage: {'on' if ex['enabled'] else 'off'}")
    if d.get("breakdown"):
        lines.append("  this week so far by surface: " + ", ".join(f"{b['name']} {b['pct']}%" for b in d["breakdown"]))
    return "\n".join(lines)


def render_line(d: dict) -> str:
    ref = datetime.now()
    if not d.get("windows"):
        return f"quota: unavailable - {d.get('note', 'no source answered')}"
    short = {"five_hour": "5h", "seven_day": "week"}
    parts = []
    for w in d["windows"]:
        rs = _dt(w.get("resets_at"))
        if w["key"] in short:
            parts.append(f"{short[w['key']]} {_pct(w['used'])} used (resets {_reset(rs, ref, True)})")
        else:
            parts.append(f"{w['label'].replace('week, ', '')} week {_pct(w['used'])}")
    src = (f"{d.get('plan') or 'subscription'}, read {_dt(d['as_of']):%H:%M}" if d["source"] == "endpoint" else
           f"from a session log written {_dt(d['as_of']):%H:%M}, NOT live: {d.get('note')}")
    return "quota: " + " | ".join(parts) + f" | {src}"


def oneline(cfg) -> str:
    """The one line `status` appends (a result up to 5 minutes old is reused)."""
    try:
        return render_line(get(300, cache=cfg.logs / "quota-cache.json", logs=cfg.logs))
    except Exception as e:  # noqa: BLE001 - fail soft, always
        return f"quota: unavailable - quota.py failed ({type(e).__name__})"


def main(argv: list[str] | None = None, cfg=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jarvis quota", description="The Claude subscription's 5-hour and weekly quota.")
    ap.add_argument("--oneline", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-age", type=float, default=None, help="reuse a result this many seconds old (default: 0, or 300 with --oneline)")
    a = ap.parse_args(argv)
    max_age = a.max_age if a.max_age is not None else (300 if a.oneline else 0)
    logs = cfg.logs if cfg is not None else LOGS
    try:
        logs.mkdir(parents=True, exist_ok=True)
        d = get(max_age, cache=logs / "quota-cache.json", logs=logs)
    except Exception as e:  # noqa: BLE001 - fail soft, always
        d = {"source": None, "windows": [], "note": f"quota.py failed ({type(e).__name__})"}
    print(json.dumps(d, indent=1) if a.json else render_line(d) if a.oneline else render_full(d))
    return 0
