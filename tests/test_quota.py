"""Tests for the quota readout (jarvis/quota.py).

  python -m unittest tests.test_quota -v

Offline: the endpoint is replaced by a recorded response (trimmed); the credentials file and the session
logs are temporary. JARVIS_QUOTA_LIVE=1 adds one live call through the real CLI login.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis import quota as Q  # noqa: E402

TOKEN = "FAKE-ACCESS-TOKEN-FOR-THE-TESTS-ONLY"
RESPONSE = {   # a recorded answer of the usage endpoint, trimmed
    "five_hour": {"utilization": 3.0, "resets_at": "2026-10-19T12:19:59.961598+00:00", "locked_reason": None},
    "seven_day": {"utilization": 35.0, "resets_at": "2026-10-21T13:00:00.161621+00:00", "locked_reason": None},
    "seven_day_opus": None, "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None, "currency": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 3, "severity": "normal", "resets_at": "2026-10-19T12:20:00.161598+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 35, "severity": "normal", "resets_at": "2026-10-21T13:00:00.161621+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 1, "severity": "normal", "resets_at": "2026-10-21T13:00:00.161804+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": False}],
    "seven_day_breakdown": {"rows": [{"key": "claude_code", "display_name": "Claude Code", "percent": 100},
                                     {"key": "chat", "display_name": "Chats", "percent": 0}]},
}
EVENT = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": 0.03, "resetsAt": 1792412400}, "seven_day": {"utilization": 0.35, "resetsAt": 1792587600}}}}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quotatest-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.creds = self.tmp / ".credentials.json"
        self.cache = self.tmp / "quota-cache.json"
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.calls: list[str] = []
        self._fetch = Q.fetch

    def tearDown(self) -> None:
        Q.fetch = self._fetch

    def login(self, expires_in_s: float = 3600, scopes=("user:inference", "user:profile")) -> None:
        self.creds.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": TOKEN, "refreshToken": "FAKE-REFRESH", "expiresAt": int((time.time() + expires_in_s) * 1000),
            "scopes": list(scopes), "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}), encoding="utf-8")

    def endpoint(self, answer) -> None:
        def fake(token: str) -> dict:
            self.calls.append(token)
            if isinstance(answer, Exception):
                raise answer
            return answer
        Q.fetch = fake

    def get(self, max_age: float = 0) -> dict:
        return Q.get(max_age, cache=self.cache, credentials=self.creds, logs=self.logs)

    def event_log(self) -> None:
        (self.logs / "dispatcher-20261019-120000.jsonl").write_text(
            '{"type":"system","subtype":"init"}\n' + json.dumps(EVENT) + "\n", encoding="utf-8")


class Endpoint(Base):
    def test_windows_plan_and_reset_rounding(self) -> None:
        self.login()
        self.endpoint(RESPONSE)
        d = self.get()
        self.assertEqual(self.calls, [TOKEN])
        self.assertEqual(d["source"], "endpoint")
        self.assertEqual(d["plan"], "Max 20x")
        w = {x["label"]: x for x in d["windows"]}
        self.assertEqual(w["5-hour window"]["used"], 3.0)
        self.assertEqual(w["week, all models"]["used"], 35.0)
        self.assertEqual(w["week, Fable"]["used"], 1.0)
        want = datetime(2026, 10, 19, 12, 20, tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")
        self.assertEqual(w["5-hour window"]["resets_at"], want, "12:19:59.96 UTC rounds to 12:20 UTC, shown in local time")
        line = Q.render_line(d)
        self.assertIn("5h 3% used (resets", line)
        self.assertIn("week 35% used", line)
        self.assertIn("Fable week 1%", line)
        full = Q.render_full(d)
        self.assertIn("65% left", full)
        self.assertIn("extra usage: off", full)
        self.assertIn("Claude Code 100%", full)
        self.assertNotIn("Chats", full, "zero rows are left out")

    def test_token_never_in_output_or_cache(self) -> None:
        self.login()
        self.endpoint(RESPONSE)
        d = self.get()
        for text in (Q.render_full(d), Q.render_line(d), json.dumps(d), self.cache.read_text(encoding="utf-8")):
            self.assertNotIn("FAKE-ACCESS", text)

    def test_http_error_falls_back_to_the_session_log(self) -> None:
        self.login()
        self.event_log()
        self.endpoint(Q.Unavailable("the usage endpoint answered HTTP 429: rate limited"))
        d = self.get()
        self.assertEqual(d["source"], "session-log")
        self.assertEqual([w["used"] for w in d["windows"]], [3.0, 35.0])
        line = Q.render_line(d)
        self.assertIn("NOT live", line)
        self.assertIn("HTTP 429", line)
        self.assertNotIn("FAKE-ACCESS", line)

    def test_expired_token_is_not_used_or_refreshed(self) -> None:
        self.login(expires_in_s=-60)
        self.event_log()
        self.endpoint(RESPONSE)
        d = self.get()
        self.assertEqual(self.calls, [], "an expired token is never sent anywhere")
        self.assertEqual(d["source"], "session-log")
        self.assertIn("expired", d["note"])

    def test_missing_scope(self) -> None:
        self.login(scopes=("user:inference",))
        self.endpoint(RESPONSE)
        d = self.get()
        self.assertEqual(self.calls, [])
        self.assertIn("user:profile", Q.render_line(d))

    def test_nothing_at_all_is_one_line_and_soft(self) -> None:
        self.endpoint(RESPONSE)
        d = self.get()
        self.assertEqual(Q.render_line(d).splitlines()[0][:19], "quota: unavailable ")
        self.assertIn("no CLI login", Q.render_full(d))

    def test_cache_reuse(self) -> None:
        self.login()
        self.endpoint(RESPONSE)
        self.get(max_age=300)
        self.get(max_age=300)
        self.assertEqual(len(self.calls), 1)
        self.get(max_age=0)
        self.assertEqual(len(self.calls), 2)

    @unittest.skipUnless(os.environ.get("JARVIS_QUOTA_LIVE") == "1", "set JARVIS_QUOTA_LIVE=1 for one real call through the CLI login")
    def test_live(self) -> None:
        d = Q.get(0, cache=self.cache)
        self.assertEqual(d["source"], "endpoint", d.get("note"))
        self.assertTrue(any(w["key"] == "seven_day" for w in d["windows"]))


if __name__ == "__main__":
    unittest.main()
