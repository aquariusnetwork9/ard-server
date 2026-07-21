"""Tier B account linking: pending codes, identity/UID linking, dedup,
suspension, per-server scoping."""
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import identity  # noqa: E402

S1 = "2b2t.org"
S2 = "6b6t.org"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LinkStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = identity.LinkStore(link_code_ttl=600, max_linked_uids=2, clock=self.clock)

    def test_full_link_flow_resolves_to_discord_identity(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.assertRegex(code, r"^[0-9A-F]{4}-[0-9A-F]{4}$")
        token_id, token = self.store.complete_link(code, "discord-1")
        self.assertIsNotNone(token_id)
        self.assertEqual(self.store.discord_identity_for(token, S1), "discord-1")

    def test_token_does_not_resolve_on_a_different_server(self):
        # The server rides through from /link/init automatically -- a token minted
        # for 2b2t.org has no standing on 6b6t.org.
        code = self.store.init_link("mc-uid-1", S1)
        _id, token = self.store.complete_link(code, "discord-1")
        self.assertEqual(self.store.discord_identity_for(token, S1), "discord-1")
        self.assertIsNone(self.store.discord_identity_for(token, S2))

    def test_same_identity_links_independently_on_two_servers(self):
        c1 = self.store.init_link("mc-uid-1", S1)
        self.store.complete_link(c1, "discord-1")
        c2 = self.store.init_link("mc-uid-1", S2)
        self.store.complete_link(c2, "discord-1")
        # Each server's linked-UID cap (max_linked_uids=2) is independent -- this
        # is the SAME uid+identity on a second server, not a second UID against S1's cap.
        c3 = self.store.init_link("mc-uid-2", S1)
        self.store.complete_link(c3, "discord-1")  # must not raise (S1 has room)

    def test_unknown_code_rejected(self):
        with self.assertRaises(ValueError):
            self.store.complete_link("DEAD-BEEF", "discord-1")

    def test_init_link_requires_server(self):
        with self.assertRaises(ValueError):
            self.store.init_link("mc-uid-1", "")

    def test_code_is_single_use(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.store.complete_link(code, "discord-1")
        with self.assertRaises(ValueError):
            self.store.complete_link(code, "discord-2")

    def test_code_expires(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.clock.t += 601  # past link_code_ttl=600
        with self.assertRaises(ValueError):
            self.store.complete_link(code, "discord-1")

    def test_peek_pending_server_returns_server_for_a_live_code(self):
        code = self.store.init_link("mc-uid-1", S2)
        self.assertEqual(self.store.peek_pending_server(code), S2)

    def test_peek_pending_server_does_not_consume_the_code(self):
        # A peek must not mark the code used -- complete_link still has to work
        # afterward, exactly as if the peek never happened.
        code = self.store.init_link("mc-uid-1", S1)
        self.store.peek_pending_server(code)
        self.store.peek_pending_server(code)
        token_id, token = self.store.complete_link(code, "discord-1")
        self.assertIsNotNone(token_id)

    def test_peek_pending_server_unknown_code(self):
        self.assertIsNone(self.store.peek_pending_server("DEAD-BEEF"))

    def test_peek_pending_server_used_code(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.store.complete_link(code, "discord-1")
        self.assertIsNone(self.store.peek_pending_server(code))

    def test_peek_pending_server_expired_code(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.clock.t += 601
        self.assertIsNone(self.store.peek_pending_server(code))

    def test_unresolved_token_has_no_identity(self):
        self.assertIsNone(self.store.discord_identity_for("never-issued", S1))
        self.assertIsNone(self.store.discord_identity_for(None, S1))

    def test_multiple_uids_resolve_to_the_same_identity(self):
        # This is the load-bearing dedup rule (PROTOCOL.md SS6.2): every UID linked
        # to one identity must resolve back to that SAME discord_id, so a caller
        # counting distinct sources by discord_id (not by token/UID) naturally dedups.
        c1 = self.store.init_link("mc-uid-1", S1)
        c2 = self.store.init_link("mc-uid-2", S1)
        _id1, tok1 = self.store.complete_link(c1, "discord-1")
        _id2, tok2 = self.store.complete_link(c2, "discord-1")
        self.assertEqual(self.store.discord_identity_for(tok1, S1),
                          self.store.discord_identity_for(tok2, S1))

    def test_max_linked_uids_enforced(self):
        # max_linked_uids=2 in setUp.
        for i in (1, 2):
            code = self.store.init_link(f"mc-uid-{i}", S1)
            self.store.complete_link(code, "discord-1")
        code3 = self.store.init_link("mc-uid-3", S1)
        with self.assertRaises(ValueError):
            self.store.complete_link(code3, "discord-1")

    def test_relinking_the_same_uid_to_the_same_identity_does_not_count_twice_against_the_cap(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.store.complete_link(code, "discord-1")
        code2 = self.store.init_link("mc-uid-1", S1)  # re-link the SAME uid
        token_id, token = self.store.complete_link(code2, "discord-1")
        self.assertEqual(self.store.discord_identity_for(token, S1), "discord-1")
        # Should still have room for one more distinct uid under the cap of 2.
        code3 = self.store.init_link("mc-uid-2", S1)
        self.store.complete_link(code3, "discord-1")  # must not raise

    def test_relinking_uid_to_a_different_identity_revokes_the_old_link(self):
        code = self.store.init_link("mc-uid-1", S1)
        _id1, tok1 = self.store.complete_link(code, "discord-1")
        code2 = self.store.init_link("mc-uid-1", S1)
        _id2, tok2 = self.store.complete_link(code2, "discord-2")
        self.assertIsNone(self.store.discord_identity_for(tok1, S1), "old link revoked")
        self.assertEqual(self.store.discord_identity_for(tok2, S1), "discord-2")

    def test_suspended_identity_tokens_stop_resolving(self):
        code = self.store.init_link("mc-uid-1", S1)
        _id, token = self.store.complete_link(code, "discord-1")
        self.assertEqual(self.store.discord_identity_for(token, S1), "discord-1")
        self.assertTrue(self.store.suspend("discord-1", S1))
        self.assertIsNone(self.store.discord_identity_for(token, S1))
        self.assertTrue(self.store.reinstate("discord-1", S1))
        self.assertEqual(self.store.discord_identity_for(token, S1), "discord-1")

    def test_suspension_does_not_cross_servers(self):
        c1 = self.store.init_link("mc-uid-1", S1)
        _id1, tok1 = self.store.complete_link(c1, "discord-1")
        c2 = self.store.init_link("mc-uid-1", S2)
        _id2, tok2 = self.store.complete_link(c2, "discord-1")
        self.assertTrue(self.store.suspend("discord-1", S1))
        self.assertIsNone(self.store.discord_identity_for(tok1, S1), "suspended on S1")
        self.assertEqual(self.store.discord_identity_for(tok2, S2), "discord-1",
                          "S2's link for the same identity is untouched")

    def test_complete_link_rejected_for_suspended_identity(self):
        code = self.store.init_link("mc-uid-1", S1)
        self.store.complete_link(code, "discord-1")
        self.store.suspend("discord-1", S1)
        code2 = self.store.init_link("mc-uid-2", S1)
        with self.assertRaises(ValueError):
            self.store.complete_link(code2, "discord-1")

    def test_suspend_unknown_identity_is_a_noop(self):
        self.assertFalse(self.store.suspend("never-linked", S1))


class MigrationTests(unittest.TestCase):
    def test_pre_server_scoping_db_backfills_existing_rows_to_2b2t(self):
        # Simulate a real pre-existing DB: identities/linked_uids with bare
        # (discord_id) / (mc_uid) primary keys and no `server` column at all.
        with tempfile.TemporaryDirectory() as d:
            db_path = str(pathlib.Path(d) / "identity.db")
            conn = sqlite3.connect(db_path)
            conn.executescript("""
            CREATE TABLE pending_links(
              code TEXT PRIMARY KEY, mc_uid TEXT NOT NULL,
              created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE identities(
              discord_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
              suspended INTEGER NOT NULL DEFAULT 0, suspended_at REAL
            );
            CREATE TABLE linked_uids(
              mc_uid TEXT PRIMARY KEY, discord_id TEXT NOT NULL,
              token_id TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
              linked_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, revoked_at REAL
            );
            """)
            conn.execute("INSERT INTO identities(discord_id,created_at,suspended)"
                         " VALUES('discord-old',1000.0,0)")
            conn.execute(
                "INSERT INTO linked_uids(mc_uid,discord_id,token_id,token_hash,linked_at,revoked)"
                " VALUES('mc-uid-old','discord-old','oldtokid','oldtokhash',1000.0,0)")
            conn.commit()
            conn.close()

            store = identity.LinkStore(db_path=db_path)
            try:
                row = store.db.execute(
                    "SELECT discord_id, server FROM linked_uids WHERE mc_uid='mc-uid-old'").fetchone()
                self.assertIsNotNone(row, "the pre-existing row must survive the migration")
                self.assertEqual(row[0], "discord-old")
                self.assertEqual(row[1], S1, "back-filled to the only server that could have created it")
                id_row = store.db.execute(
                    "SELECT server FROM identities WHERE discord_id='discord-old'").fetchone()
                self.assertEqual(id_row[0], S1)
            finally:
                store.db.close()  # Windows can't rmtree a tempdir with an open file handle in it


def _mock_response(payload):
    m = MagicMock()
    m.read.return_value = json.dumps(payload).encode()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class DiscordExchangeTests(unittest.TestCase):
    """discord_exchange() itself -- the real network-calling implementation, not
    the fake_discord_verify() stand-in test_http.py uses everywhere else.
    Regression coverage for a real production outage: both /link and /admin
    login failed live with 'token exchange failed: 403 ... error code: 1010'
    (Cloudflare) because neither outbound request carried a User-Agent --
    Discord's API docs say a default HTTP-tool signature gets blocked outright."""

    @patch("identity.urllib.request.urlopen")
    def test_every_outbound_request_carries_a_real_user_agent(self, mock_urlopen):
        mock_urlopen.side_effect = [_mock_response({"access_token": "AT"}),
                                     _mock_response({"id": "discord-123"})]

        result = identity.discord_exchange("CID", "CSECRET", "https://example.test/link.html", "CODE")

        self.assertEqual(result, "discord-123")
        self.assertEqual(mock_urlopen.call_count, 2)
        for call in mock_urlopen.call_args_list:
            req = call.args[0]
            ua = req.get_header("User-agent")
            self.assertTrue(ua, "every Discord API request must carry an explicit User-Agent")
            self.assertNotIn("python-urllib", ua.lower(), "Discord blocks default HTTP-tool signatures")

    @patch("identity.urllib.request.urlopen")
    def test_token_exchange_http_error_is_wrapped(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://discord.com/api/oauth2/token", 403, "Forbidden", {}, io.BytesIO(b"error code: 1010"))
        with self.assertRaises(identity.DiscordOAuthError):
            identity.discord_exchange("CID", "CSECRET", "https://example.test/link.html", "CODE")

    @patch("identity.urllib.request.urlopen")
    def test_missing_access_token_is_rejected(self, mock_urlopen):
        mock_urlopen.side_effect = [_mock_response({"no_access_token_here": True})]
        with self.assertRaises(identity.DiscordOAuthError):
            identity.discord_exchange("CID", "CSECRET", "https://example.test/link.html", "CODE")


if __name__ == "__main__":
    unittest.main()
