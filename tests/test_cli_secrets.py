"""apply_env_secrets: production (systemd) secret injection via env vars, additive
with -- never overriding -- whatever CLI flags already supplied. Keeps secrets out
of `ps aux` on shared boxes without breaking the CLI-flag path local/dev runs use."""
import argparse
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import trust  # noqa: E402
from highway_conditions import apply_env_secrets, _seed_discord_admins  # noqa: E402


def _args(**overrides):
    base = {"owner_token": None, "seed_token": None, "discord_client_secret": None,
            "discord_admin": None}
    base.update(overrides)
    return argparse.Namespace(**base)


class ApplyEnvSecretsTests(unittest.TestCase):
    def test_no_env_leaves_args_untouched(self):
        args = apply_env_secrets(_args(), environ={})
        self.assertIsNone(args.owner_token)
        self.assertIsNone(args.seed_token)
        self.assertIsNone(args.discord_client_secret)

    def test_owner_token_env_is_picked_up(self):
        args = apply_env_secrets(_args(), environ={"ARD_OWNER_TOKEN": "OWNER-FROM-ENV"})
        self.assertEqual(args.owner_token, ["OWNER-FROM-ENV"])

    def test_owner_token_env_is_additive_with_cli(self):
        args = apply_env_secrets(_args(owner_token=["OWNER-FROM-CLI"]),
                                  environ={"ARD_OWNER_TOKEN": "OWNER-FROM-ENV"})
        self.assertEqual(args.owner_token, ["OWNER-FROM-CLI", "OWNER-FROM-ENV"])

    def test_seed_tokens_env_parses_comma_separated_list(self):
        args = apply_env_secrets(
            _args(), environ={"ARD_SEED_TOKENS": "TOK1:full:bot-1,TOK2:maintainer:crew-1"})
        self.assertEqual(args.seed_token, ["TOK1:full:bot-1", "TOK2:maintainer:crew-1"])

    def test_seed_tokens_env_trims_whitespace_and_drops_empties(self):
        args = apply_env_secrets(_args(), environ={"ARD_SEED_TOKENS": " TOK1:full:bot-1 , , "})
        self.assertEqual(args.seed_token, ["TOK1:full:bot-1"])

    def test_seed_tokens_env_is_additive_with_cli(self):
        args = apply_env_secrets(_args(seed_token=["TOK0:full:cli-seeded"]),
                                  environ={"ARD_SEED_TOKENS": "TOK1:full:bot-1"})
        self.assertEqual(args.seed_token, ["TOK0:full:cli-seeded", "TOK1:full:bot-1"])

    def test_discord_secret_env_is_picked_up_when_cli_absent(self):
        args = apply_env_secrets(_args(), environ={"ARD_DISCORD_CLIENT_SECRET": "SECRET-FROM-ENV"})
        self.assertEqual(args.discord_client_secret, "SECRET-FROM-ENV")

    def test_discord_secret_cli_wins_over_env(self):
        args = apply_env_secrets(_args(discord_client_secret="SECRET-FROM-CLI"),
                                  environ={"ARD_DISCORD_CLIENT_SECRET": "SECRET-FROM-ENV"})
        self.assertEqual(args.discord_client_secret, "SECRET-FROM-CLI")

    def test_discord_admins_env_parses_comma_separated_list(self):
        args = apply_env_secrets(
            _args(), environ={"ARD_DISCORD_ADMINS": "disc-1:2b2t.org,disc-2:6b6t.org"})
        self.assertEqual(args.discord_admin, ["disc-1:2b2t.org", "disc-2:6b6t.org"])

    def test_discord_admins_env_is_additive_with_cli(self):
        args = apply_env_secrets(_args(discord_admin=["disc-0:2b2t.org"]),
                                  environ={"ARD_DISCORD_ADMINS": "disc-1:2b2t.org"})
        self.assertEqual(args.discord_admin, ["disc-0:2b2t.org", "disc-1:2b2t.org"])

    def test_trusted_proxies_env_parses_comma_separated_list(self):
        args = apply_env_secrets(_args(), environ={"ARD_TRUSTED_PROXIES": "10.0.0.5,10.0.0.6/32"})
        self.assertEqual(args.trusted_proxy, ["10.0.0.5", "10.0.0.6/32"])

    def test_trusted_proxies_env_is_additive_with_cli(self):
        args = apply_env_secrets(_args(trusted_proxy=["10.0.0.1/32"]),
                                  environ={"ARD_TRUSTED_PROXIES": "10.0.0.2/32"})
        self.assertEqual(args.trusted_proxy, ["10.0.0.1/32", "10.0.0.2/32"])


class SeedDiscordAdminsTests(unittest.TestCase):
    def test_seeds_a_live_admin_grant(self):
        reg = trust.Registry()
        _seed_discord_admins(reg, ["disc-1:2b2t.org"])
        self.assertEqual(reg.discord_scopes("disc-1", "2b2t.org"), {trust.SCOPE_ADMIN})

    def test_malformed_spec_exits(self):
        reg = trust.Registry()
        with self.assertRaises(SystemExit):
            _seed_discord_admins(reg, ["not-a-valid-spec"])

    def test_idempotent_across_restarts(self):
        # Same shape as --seed-token: a persistent registry means this spec is
        # re-applied on every process restart, so it must not error or duplicate.
        reg = trust.Registry()
        _seed_discord_admins(reg, ["disc-2:2b2t.org"])
        _seed_discord_admins(reg, ["disc-2:2b2t.org"])
        self.assertEqual(len(reg.list_discord_grants(server="2b2t.org")), 1)


if __name__ == "__main__":
    unittest.main()
