"""Owner-issued token registry: scopes, revocation-by-id, independent grants,
per-server scoping."""
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import trust  # noqa: E402

S1 = "2b2t.org"
S2 = "6b6t.org"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.reg = trust.Registry(clock=self.clock)

    def test_issued_token_has_live_scope(self):
        token_id, token = self.reg.issue("fleet-bot-1", "owner", trust.SCOPE_FULL, S1)
        self.assertEqual(self.reg.scope_of(token, S1), trust.SCOPE_FULL)
        self.assertTrue(self.reg.has_scope(token, trust.SCOPE_FULL, S1))
        self.assertIsNotNone(token_id)

    def test_unknown_token_has_no_scope(self):
        self.assertIsNone(self.reg.scope_of("never-issued", S1))
        self.assertIsNone(self.reg.scope_of(None, S1))
        self.assertIsNone(self.reg.scope_of("", S1))

    def test_token_has_no_scope_on_a_different_server(self):
        # The whole point of per-server scoping: a grant for one anarchy server
        # carries no authority on another sharing this deployment.
        _id, token = self.reg.issue("fleet-bot-1", "owner", trust.SCOPE_FULL, S1)
        self.assertEqual(self.reg.scope_of(token, S1), trust.SCOPE_FULL)
        self.assertIsNone(self.reg.scope_of(token, S2))

    def test_scope_of_requires_a_server(self):
        _id, token = self.reg.issue("fleet-bot-1", "owner", trust.SCOPE_FULL, S1)
        self.assertIsNone(self.reg.scope_of(token, None))
        self.assertIsNone(self.reg.scope_of(token, ""))

    def test_bad_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.issue("someone", "owner", "super-admin", S1)

    def test_missing_holder_label_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.issue("", "owner", trust.SCOPE_FULL, S1)

    def test_missing_server_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.issue("someone", "owner", trust.SCOPE_FULL, "")

    def test_revoke_by_token_id_takes_effect_immediately(self):
        token_id, token = self.reg.issue("mod-1", "owner", trust.SCOPE_MODERATOR, S1)
        self.assertEqual(self.reg.scope_of(token, S1), trust.SCOPE_MODERATOR)
        self.assertTrue(self.reg.revoke(token_id))
        self.assertIsNone(self.reg.scope_of(token, S1), "revoked token has no live scope")

    def test_revoke_does_not_need_the_raw_secret(self):
        # The whole point of revoking is often that you no longer have (or trust)
        # the holder's copy of the secret -- revocation must work by the public
        # token_id alone.
        token_id, _lost_token = self.reg.issue("departed-contributor", "owner", trust.SCOPE_MAINTAINER, S1)
        self.assertTrue(self.reg.revoke(token_id))

    def test_revoke_unknown_id_is_a_noop(self):
        self.assertFalse(self.reg.revoke("deadbeefdeadbeef"))

    def test_revoke_twice_is_a_noop_second_time(self):
        token_id, _ = self.reg.issue("someone", "owner", trust.SCOPE_FULL, S1)
        self.assertTrue(self.reg.revoke(token_id))
        self.assertFalse(self.reg.revoke(token_id))

    def test_holder_can_have_independent_scopes(self):
        # A group can hold a maintainer token and a separate full token -- they're
        # independent grants, not a hierarchy (PROTOCOL.md SS6.3).
        _id_a, full_tok = self.reg.issue("highway-group", "owner", trust.SCOPE_FULL, S1)
        _id_m, maint_tok = self.reg.issue("highway-group", "owner", trust.SCOPE_MAINTAINER, S1)
        self.assertEqual(self.reg.scope_of(full_tok, S1), trust.SCOPE_FULL)
        self.assertEqual(self.reg.scope_of(maint_tok, S1), trust.SCOPE_MAINTAINER)
        self.assertTrue(self.reg.revoke(_id_m))
        self.assertEqual(self.reg.scope_of(full_tok, S1), trust.SCOPE_FULL, "revoking one grant doesn't touch the other")
        self.assertIsNone(self.reg.scope_of(maint_tok, S1))

    def test_holder_can_have_the_same_scope_on_two_servers_via_two_tokens(self):
        # Trust doesn't transfer automatically -- someone trusted on both servers
        # gets two independent tokens, one per server.
        _id_1, tok_1 = self.reg.issue("highway-group", "owner", trust.SCOPE_FULL, S1)
        _id_2, tok_2 = self.reg.issue("highway-group", "owner", trust.SCOPE_FULL, S2)
        self.assertNotEqual(tok_1, tok_2)
        self.assertEqual(self.reg.scope_of(tok_1, S1), trust.SCOPE_FULL)
        self.assertIsNone(self.reg.scope_of(tok_1, S2))
        self.assertEqual(self.reg.scope_of(tok_2, S2), trust.SCOPE_FULL)
        self.assertIsNone(self.reg.scope_of(tok_2, S1))

    def test_seeded_token_bootstraps_an_existing_secret(self):
        # CLI/deploy-time bootstrap: register a token that already exists elsewhere
        # (e.g. a fleet bot's own config) instead of minting a fresh one.
        token_id, returned = self.reg.issue("fleet-bot-2", "cli-seed", trust.SCOPE_FULL, S1,
                                             token="ALREADY-EXISTING-SECRET")
        self.assertEqual(returned, "ALREADY-EXISTING-SECRET")
        self.assertEqual(self.reg.scope_of("ALREADY-EXISTING-SECRET", S1), trust.SCOPE_FULL)

    def test_list_active_never_exposes_the_raw_token_or_hash(self):
        token_id, token = self.reg.issue("fleet-bot-3", "owner", trust.SCOPE_FULL, S1)
        active = self.reg.list_active()
        self.assertEqual(len(active), 1)
        entry = active[0]
        self.assertEqual(entry["tokenId"], token_id)
        self.assertEqual(entry["holderLabel"], "fleet-bot-3")
        self.assertEqual(entry["scope"], trust.SCOPE_FULL)
        self.assertEqual(entry["server"], S1)
        dumped = str(entry)
        self.assertNotIn(token, dumped)
        self.assertNotIn(trust.Registry.hash_token(token), dumped)

    def test_list_active_excludes_revoked(self):
        keep_id, _ = self.reg.issue("keep", "owner", trust.SCOPE_FULL, S1)
        drop_id, _ = self.reg.issue("drop", "owner", trust.SCOPE_FULL, S1)
        self.reg.revoke(drop_id)
        ids = {e["tokenId"] for e in self.reg.list_active()}
        self.assertEqual(ids, {keep_id})

    def test_reseeding_the_same_known_token_is_idempotent(self):
        # A persistent registry DB means a CLI --seed-token spec gets re-issued on
        # EVERY process restart -- this must not crash on the token_hash UNIQUE
        # constraint the second time around (real bug hit deploying to ohv-2).
        id1, tok1 = self.reg.issue("fleet-bot-4", "cli-seed", trust.SCOPE_FULL, S1, token="STABLE-SEED")
        id2, tok2 = self.reg.issue("fleet-bot-4", "cli-seed", trust.SCOPE_FULL, S1, token="STABLE-SEED")
        self.assertEqual(id1, id2)
        self.assertEqual(tok1, tok2)
        self.assertEqual(self.reg.scope_of("STABLE-SEED", S1), trust.SCOPE_FULL)
        self.assertEqual(len(self.reg.list_active()), 1, "must not create a duplicate row")

    def test_reseeding_updates_label_scope_and_server_in_place(self):
        # A redeploy that changes a seed token's scope/label/server should apply,
        # not be silently ignored just because the token string itself didn't change.
        token_id, tok = self.reg.issue("old-label", "cli-seed", trust.SCOPE_MAINTAINER, S1, token="STABLE-SEED-2")
        self.reg.issue("new-label", "cli-seed", trust.SCOPE_FULL, S2, token="STABLE-SEED-2")
        self.assertEqual(self.reg.scope_of(tok, S2), trust.SCOPE_FULL)
        self.assertIsNone(self.reg.scope_of(tok, S1), "server should have moved, not been added to")
        active = self.reg.list_active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["tokenId"], token_id)
        self.assertEqual(active[0]["holderLabel"], "new-label")
        self.assertEqual(active[0]["server"], S2)

    def test_reseeding_a_revoked_token_does_not_resurrect_it(self):
        # If the Owner has since revoked a seed token, a routine restart re-running
        # the same --seed-token spec must NOT silently bring it back.
        token_id, tok = self.reg.issue("fleet-bot-5", "cli-seed", trust.SCOPE_FULL, S1, token="STABLE-SEED-3")
        self.reg.revoke(token_id)
        self.reg.issue("fleet-bot-5", "cli-seed", trust.SCOPE_FULL, S1, token="STABLE-SEED-3")
        self.assertIsNone(self.reg.scope_of(tok, S1), "re-seeding must not undo an explicit revoke")
        self.assertEqual(self.reg.list_active(), [])


class DiscordGrantTests(unittest.TestCase):
    """Admin dashboard access granted directly to a Discord identity (no bearer
    token involved) -- the credential the sessions.py cookie flow resolves."""

    def setUp(self):
        self.clock = Clock()
        self.reg = trust.Registry(clock=self.clock)

    def test_granted_identity_has_the_scope_on_that_server_only(self):
        self.reg.grant_to_discord("disc-1", trust.SCOPE_MODERATOR, S1, "owner")
        self.assertEqual(self.reg.discord_scopes("disc-1", S1), {trust.SCOPE_MODERATOR})
        self.assertEqual(self.reg.discord_scopes("disc-1", S2), set())

    def test_ungranted_identity_has_no_scopes(self):
        self.assertEqual(self.reg.discord_scopes("nobody", S1), set())
        self.assertEqual(self.reg.discord_scopes(None, S1), set())
        self.assertEqual(self.reg.discord_scopes("disc-1", None), set())

    def test_identity_can_hold_both_moderator_and_admin_independently(self):
        self.reg.grant_to_discord("disc-2", trust.SCOPE_MODERATOR, S1, "owner")
        self.reg.grant_to_discord("disc-2", trust.SCOPE_ADMIN, S1, "owner")
        self.assertEqual(self.reg.discord_scopes("disc-2", S1), {trust.SCOPE_MODERATOR, trust.SCOPE_ADMIN})

    def test_bad_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.grant_to_discord("disc-3", trust.SCOPE_FULL, S1, "owner")

    def test_missing_discord_id_or_server_rejected(self):
        with self.assertRaises(ValueError):
            self.reg.grant_to_discord("", trust.SCOPE_MODERATOR, S1, "owner")
        with self.assertRaises(ValueError):
            self.reg.grant_to_discord("disc-4", trust.SCOPE_MODERATOR, "", "owner")

    def test_revoke_by_grant_id_takes_effect_immediately(self):
        grant_id = self.reg.grant_to_discord("disc-5", trust.SCOPE_MODERATOR, S1, "owner")
        self.assertTrue(self.reg.revoke_discord_grant(grant_id))
        self.assertEqual(self.reg.discord_scopes("disc-5", S1), set())

    def test_revoke_unknown_grant_is_a_noop(self):
        self.assertFalse(self.reg.revoke_discord_grant("deadbeefdeadbeef"))

    def test_regranting_after_revoke_reactivates_the_same_row(self):
        grant_id = self.reg.grant_to_discord("disc-6", trust.SCOPE_MODERATOR, S1, "owner")
        self.reg.revoke_discord_grant(grant_id)
        grant_id_2 = self.reg.grant_to_discord("disc-6", trust.SCOPE_MODERATOR, S1, "owner")
        self.assertEqual(grant_id, grant_id_2, "re-granting reuses the row rather than duplicating")
        self.assertEqual(self.reg.discord_scopes("disc-6", S1), {trust.SCOPE_MODERATOR})

    def test_admin_servers_for_only_counts_admin_scope(self):
        self.reg.grant_to_discord("disc-7", trust.SCOPE_MODERATOR, S1, "owner")
        self.reg.grant_to_discord("disc-7", trust.SCOPE_ADMIN, S2, "owner")
        self.assertEqual(self.reg.admin_servers_for("disc-7"), {S2})

    def test_grants_for_discord_lists_across_servers(self):
        self.reg.grant_to_discord("disc-8", trust.SCOPE_ADMIN, S1, "owner")
        self.reg.grant_to_discord("disc-8", trust.SCOPE_MODERATOR, S2, "owner")
        grants = self.reg.grants_for_discord("disc-8")
        self.assertEqual({(g["server"], g["scope"]) for g in grants},
                          {(S1, trust.SCOPE_ADMIN), (S2, trust.SCOPE_MODERATOR)})

    def test_grants_for_discord_excludes_revoked(self):
        grant_id = self.reg.grant_to_discord("disc-9", trust.SCOPE_MODERATOR, S1, "owner")
        self.reg.revoke_discord_grant(grant_id)
        self.assertEqual(self.reg.grants_for_discord("disc-9"), [])

    def test_discord_grant_lookup_reports_server_and_scope(self):
        grant_id = self.reg.grant_to_discord("disc-10", trust.SCOPE_ADMIN, S1, "owner")
        self.assertEqual(self.reg.discord_grant_lookup(grant_id), {"server": S1, "scope": trust.SCOPE_ADMIN})
        self.assertIsNone(self.reg.discord_grant_lookup("nope"))

    def test_list_discord_grants_scoped_by_server(self):
        self.reg.grant_to_discord("disc-11", trust.SCOPE_MODERATOR, S1, "owner")
        self.reg.grant_to_discord("disc-12", trust.SCOPE_MODERATOR, S2, "owner")
        s1_grants = self.reg.list_discord_grants(server=S1)
        self.assertEqual([g["discordId"] for g in s1_grants], ["disc-11"])

    def test_token_server_reports_the_issuing_server(self):
        token_id, _tok = self.reg.issue("fleet-bot-x", "owner", trust.SCOPE_FULL, S1)
        self.assertEqual(self.reg.token_server(token_id), S1)
        self.assertIsNone(self.reg.token_server("unknown"))

    def test_list_active_can_be_scoped_by_server(self):
        self.reg.issue("a", "owner", trust.SCOPE_FULL, S1)
        self.reg.issue("b", "owner", trust.SCOPE_FULL, S2)
        s1_active = self.reg.list_active(server=S1)
        self.assertEqual([e["holderLabel"] for e in s1_active], ["a"])


class MigrationTests(unittest.TestCase):
    def test_pre_server_scoping_db_backfills_existing_rows_to_2b2t(self):
        # Simulate a real ohv-2-shaped DB from before per-server scoping existed:
        # a tokens table with no `server` column at all, holding one live row.
        with tempfile.TemporaryDirectory() as d:
            db_path = str(pathlib.Path(d) / "registry.db")
            conn = sqlite3.connect(db_path)
            conn.executescript("""
            CREATE TABLE tokens(
              token_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
              holder_label TEXT NOT NULL, issued_by TEXT NOT NULL,
              issued_at REAL NOT NULL, scope TEXT NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0, revoked_at REAL
            );
            """)
            conn.execute(
                "INSERT INTO tokens(token_id,token_hash,holder_label,issued_by,issued_at,scope,revoked)"
                " VALUES('oldid','oldhash','owner-admin','cli-seed',1000.0,'full',0)")
            conn.commit()
            conn.close()

            reg = trust.Registry(db_path=db_path)
            try:
                active = reg.list_active()
                self.assertEqual(len(active), 1, "the pre-existing row must survive the migration")
                self.assertEqual(active[0]["server"], S1, "back-filled to the only server that could have issued it")
                self.assertEqual(active[0]["tokenId"], "oldid")
            finally:
                reg.db.close()  # Windows can't rmtree a tempdir with an open file handle in it


if __name__ == "__main__":
    unittest.main()
