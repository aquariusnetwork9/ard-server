"""Notifier: per-key throttling, disabled-by-default, real headers on the wire."""
import pathlib
import sys
import unittest
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import notify  # noqa: E402


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class NotifierTests(unittest.TestCase):
    def make(self, **kw):
        self.sent = []
        kw.setdefault("url", "https://ntfy.test/topic")
        kw.setdefault("clock", FakeClock())
        kw.setdefault("async_send", False)
        kw.setdefault("transport", lambda title, msg, prio: self.sent.append((title, msg, prio)))
        return notify.Notifier(**kw)

    def test_disabled_without_url(self):
        n = self.make(url=None)
        self.assertFalse(n.enabled)
        self.assertFalse(n.send("k", "t", "m"))
        self.assertEqual(self.sent, [])

    def test_sends_and_throttles_per_key(self):
        n = self.make()
        self.assertTrue(n.send("k1", "title", "msg"))
        self.assertFalse(n.send("k1", "title", "msg"), "same key inside min_interval is dropped")
        self.assertEqual(len(self.sent), 1)
        n.clock.t += notify.DEFAULT_MIN_INTERVAL + 1
        self.assertTrue(n.send("k1", "title", "msg"), "interval elapsed -> fires again")
        self.assertEqual(len(self.sent), 2)

    def test_distinct_keys_are_independent(self):
        n = self.make()
        self.assertTrue(n.send("k1", "t", "m"))
        self.assertTrue(n.send("k2", "t", "m"))
        self.assertEqual(len(self.sent), 2)

    def test_transport_failure_is_swallowed(self):
        def boom(title, msg, prio):
            raise RuntimeError("down")
        n = self.make(transport=boom)
        self.assertTrue(n.send("k", "t", "m"), "dispatch reported; failure stays internal")

    def test_post_sets_real_user_agent_and_bearer(self):
        n = notify.Notifier(url="https://ntfy.test/topic", token="tok123", async_send=False)
        with patch("notify.urllib.request.urlopen") as uo:
            uo.return_value.__enter__ = MagicMock()
            uo.return_value.__exit__ = MagicMock(return_value=False)
            n._post("Title", "body", "high")
            req = uo.call_args[0][0]
        self.assertEqual(req.get_header("User-agent"), notify.USER_AGENT,
                          "a default urllib signature gets blocked by Cloudflare-fronted ntfy")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok123")
        self.assertEqual(req.get_header("Title"), "Title")
        self.assertEqual(req.get_header("Priority"), "high")


if __name__ == "__main__":
    unittest.main()
