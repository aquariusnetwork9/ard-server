"""TwoBTwoTVCPresence: cached tablist membership check, fails open (None) on any
fetch/parse problem -- a third party's outage must never suppress real reporting."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import presence  # noqa: E402


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


PLAYERS = [
    {"playerName": "Alice", "uuid": "aaaaaaaa-1111-1111-1111-111111111111", "prio": False, "bot": False},
    {"playerName": "Bob", "uuid": "bbbbbbbb-2222-2222-2222-222222222222", "prio": True, "bot": True},
]


class TwoBTwoTVCPresenceTests(unittest.TestCase):
    def make(self, players=None, raise_on_fetch=False):
        self.fetch_calls = 0

        def fetch():
            self.fetch_calls += 1
            if raise_on_fetch:
                raise OSError("unreachable")
            return players if players is not None else PLAYERS

        self.clock = FakeClock()
        return presence.TwoBTwoTVCPresence(clock=self.clock, fetch=fetch)

    def test_present_uuid_matches_dashed_form(self):
        p = self.make()
        self.assertTrue(p.is_present("aaaaaaaa-1111-1111-1111-111111111111"))

    def test_present_uuid_matches_dashless_form(self):
        # ARD stores/receives mc_uid in whatever form the producer sent -- the
        # check must not care about dash formatting.
        p = self.make()
        self.assertTrue(p.is_present("aaaaaaaa111111111111111111111111"))

    def test_case_insensitive(self):
        p = self.make()
        self.assertTrue(p.is_present("AAAAAAAA-1111-1111-1111-111111111111"))

    def test_absent_uuid_is_false(self):
        p = self.make()
        self.assertFalse(p.is_present("cccccccc-3333-3333-3333-333333333333"))

    def test_fetch_failure_is_none_not_false(self):
        p = self.make(raise_on_fetch=True)
        self.assertIsNone(p.is_present("aaaaaaaa-1111-1111-1111-111111111111"),
                          "unknown must never be treated as confirmed-absent")

    def test_result_is_cached_within_ttl(self):
        p = self.make()
        p.is_present("aaaaaaaa-1111-1111-1111-111111111111")
        p.is_present("bbbbbbbb-2222-2222-2222-222222222222")
        self.assertEqual(self.fetch_calls, 1, "a second check within CACHE_TTL reuses the fetch")

    def test_cache_expires_after_ttl(self):
        p = self.make()
        p.is_present("aaaaaaaa-1111-1111-1111-111111111111")
        self.clock.t += presence.TwoBTwoTVCPresence.CACHE_TTL + 1
        p.is_present("aaaaaaaa-1111-1111-1111-111111111111")
        self.assertEqual(self.fetch_calls, 2)

    def test_malformed_player_entry_is_none(self):
        p = self.make(players=[{"no_uuid_here": True}])
        self.assertIsNone(p.is_present("aaaaaaaa-1111-1111-1111-111111111111"))


if __name__ == "__main__":
    unittest.main()
