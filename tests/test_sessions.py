"""Admin dashboard sessions (sessions.py): create/resolve/expire/revoke."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import sessions  # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = sessions.SessionStore(session_ttl=3600, clock=self.clock)

    def test_created_session_resolves_to_its_discord_id(self):
        token = self.store.create("disc-1")
        self.assertEqual(self.store.discord_id_for(token), "disc-1")

    def test_unknown_token_resolves_to_none(self):
        self.assertIsNone(self.store.discord_id_for("never-issued"))
        self.assertIsNone(self.store.discord_id_for(None))
        self.assertIsNone(self.store.discord_id_for(""))

    def test_two_sessions_for_the_same_identity_are_independent_tokens(self):
        t1 = self.store.create("disc-2")
        t2 = self.store.create("disc-2")
        self.assertNotEqual(t1, t2)
        self.assertEqual(self.store.discord_id_for(t1), "disc-2")
        self.assertEqual(self.store.discord_id_for(t2), "disc-2")

    def test_session_expires_after_ttl(self):
        token = self.store.create("disc-3")
        self.clock.t += 3601
        self.assertIsNone(self.store.discord_id_for(token))

    def test_session_still_live_just_under_ttl(self):
        token = self.store.create("disc-4")
        self.clock.t += 3599
        self.assertEqual(self.store.discord_id_for(token), "disc-4")

    def test_revoke_takes_effect_immediately(self):
        token = self.store.create("disc-5")
        self.assertTrue(self.store.revoke(token))
        self.assertIsNone(self.store.discord_id_for(token))

    def test_revoke_unknown_token_is_a_noop(self):
        self.assertFalse(self.store.revoke("never-issued"))

    def test_revoke_all_for_kills_every_session_for_that_identity_only(self):
        a1 = self.store.create("disc-6")
        a2 = self.store.create("disc-6")
        other = self.store.create("disc-7")
        killed = self.store.revoke_all_for("disc-6")
        self.assertEqual(killed, 2)
        self.assertIsNone(self.store.discord_id_for(a1))
        self.assertIsNone(self.store.discord_id_for(a2))
        self.assertEqual(self.store.discord_id_for(other), "disc-7", "a different identity's session is untouched")


if __name__ == "__main__":
    unittest.main()
