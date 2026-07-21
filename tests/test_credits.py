"""Survey (confirmed-report credit) and Road Crew (repair) leaderboards, plus the
bot/moderator-only /conditions/<server>/all radar feed -- PROTOCOL.md SS6.7, all
shipped together (0.1.9). Own class/server per concern, dedicated fresh Store,
coordinates spaced 300+ apart within a class (this codebase's own along-bucket
collision lesson -- see DispatchHttpTests in test_http.py)."""
import json
import pathlib
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import Store, Auth, App, Server  # noqa: E402
import reference_client  # noqa: E402
import trust  # noqa: E402
import identity  # noqa: E402

GEO_DIR = ROOT / "geometry"
SERVER = "2b2t.org"
FULL_TOKEN = "FULL-SECRET-CREDITS"
MODERATOR_TOKEN = "MODERATOR-SECRET-CREDITS"
BOT_TOKEN = "BOT-SECRET-CREDITS"
NOBODY_TOKEN = "SOME-RANDOM-TOKEN-CREDITS"


def req(method, url, token=None, body=None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = token
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class SurveyCreditTests(unittest.TestCase):
    """POST /link/credit-opt-in + the credits ledger written from Store.ingest()."""

    @classmethod
    def setUpClass(cls):
        # k_tier_b=1 -- a single opted-in Tier B report is enough to publish a
        # hazard condition on its own, keeping most tests here to one report.
        store = Store(str(GEO_DIR), k_anon=2, k_tier_b=1, ttl=1000, salt="testsalt-credits")
        registry = trust.Registry()
        registry.issue("credits-fleet", "test-owner", trust.SCOPE_FULL, SERVER, token=FULL_TOKEN)
        registry.issue("credits-mod", "test-owner", trust.SCOPE_MODERATOR, SERVER, token=MODERATOR_TOKEN)
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8, require_ownership_proof=False)
        auth = Auth(registry, links=links, bot_hashes={Auth.hash_token(BOT_TOKEN)})
        cls.links = links
        cls.app = App(store, auth)
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.net = store.networks[SERVER]
        cls.map = store.map_hashes[SERVER]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def a_report(self, x, cond="HOLE"):
        return reference_client.build_report(x, 120, 0, "NETHER", self.net, self.map,
                                             SERVER, cond=cond)

    def link_and_get_token(self, mc_uid, discord_id):
        """Full Tier B link flow (bot path) -- returns the live bearer token."""
        _, init_body = req("POST", self.url("/link/init"), body={"mcUid": mc_uid, "server": SERVER})
        code = init_body["code"]
        status, body = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                            body={"linkCode": code, "discordId": discord_id})
        self.assertEqual(status, 200)
        return body["token"]

    def opt_in(self, discord_id, value=True):
        return req("POST", self.url("/link/credit-opt-in"), token=BOT_TOKEN,
                    body={"discordId": discord_id, "server": SERVER, "optIn": value})

    def leaderboard(self, kind="survey"):
        status, body = req("GET", self.url(f"/credits/{SERVER}/leaderboard?kind={kind}"), token=BOT_TOKEN)
        self.assertEqual(status, 200)
        return {row["discordId"]: row["count"] for row in body["leaderboard"]}

    def test_opt_in_requires_bot_credential(self):
        self.link_and_get_token("mc-credit-auth", "discord-credit-authcheck")
        status, _ = self.opt_in("discord-credit-authcheck")
        self.assertEqual(status, 200)  # bot token used by the helper -- sanity baseline
        status, _ = req("POST", self.url("/link/credit-opt-in"), token=FULL_TOKEN,
                         body={"discordId": "discord-credit-authcheck", "server": SERVER, "optIn": True})
        self.assertEqual(status, 401)
        status, _ = req("POST", self.url("/link/credit-opt-in"),
                         body={"discordId": "discord-credit-authcheck", "server": SERVER, "optIn": True})
        self.assertEqual(status, 401)

    def test_opt_in_requires_an_existing_linked_identity(self):
        status, _ = self.opt_in("discord-never-linked")
        self.assertEqual(status, 404)

    def test_opted_out_identity_earns_no_credit(self):
        token = self.link_and_get_token("mc-credit-1", "discord-credit-optout")
        # Never opts in.
        status, _ = req("POST", self.url("/report"), token=token, body=self.a_report(9000))
        self.assertEqual(status, 200)
        self.assertNotIn("discord-credit-optout", self.leaderboard())

    def test_opted_in_identity_earns_credit_on_a_confirmed_report(self):
        token = self.link_and_get_token("mc-credit-2", "discord-credit-optin")
        status, _ = self.opt_in("discord-credit-optin")
        self.assertEqual(status, 200)
        status, _ = req("POST", self.url("/report"), token=token, body=self.a_report(9300))
        self.assertEqual(status, 200)
        board = self.leaderboard()
        self.assertEqual(board.get("discord-credit-optin"), 1)

    def test_resending_the_same_confirmed_report_does_not_double_credit(self):
        token = self.link_and_get_token("mc-credit-3", "discord-credit-resend")
        self.opt_in("discord-credit-resend")
        r = self.a_report(9600)
        for _ in range(3):
            status, _ = req("POST", self.url("/report"), token=token, body=r)
            self.assertEqual(status, 200)
        board = self.leaderboard()
        self.assertEqual(board.get("discord-credit-resend"), 1)

    def test_opting_out_again_stops_future_credit_but_keeps_the_past_award(self):
        token = self.link_and_get_token("mc-credit-4", "discord-credit-toggle")
        self.opt_in("discord-credit-toggle", True)
        req("POST", self.url("/report"), token=token, body=self.a_report(9900))
        self.assertEqual(self.leaderboard().get("discord-credit-toggle"), 1)
        self.opt_in("discord-credit-toggle", False)
        req("POST", self.url("/report"), token=token, body=self.a_report(10200))
        # New condition at a different key never gets credited once opted out again,
        # but the earlier award (a different cond_id) isn't retracted -- opting out
        # only gates future writes, matching the "permanent once awarded" retention
        # decision.
        self.assertEqual(self.leaderboard().get("discord-credit-toggle"), 1)

    def test_tier_c_anonymous_reports_never_appear_on_the_survey_leaderboard(self):
        # No token at all -> Tier C. Even with k_anon=2, two anonymous reports could
        # publish this condition, but there's no discord_id to credit at all.
        r = self.a_report(10500)
        req("POST", self.url("/report"), body=r)
        req("POST", self.url("/report"), body=r)
        board = self.leaderboard()
        self.assertEqual(board, {k: v for k, v in board.items() if not k.startswith("discord-credit-anon")})

    def test_leaderboard_requires_bot_or_moderator_credential(self):
        status, _ = req("GET", self.url(f"/credits/{SERVER}/leaderboard"), token=NOBODY_TOKEN)
        self.assertEqual(status, 403)
        status, _ = req("GET", self.url(f"/credits/{SERVER}/leaderboard"), token=MODERATOR_TOKEN)
        self.assertEqual(status, 200)

    def test_bad_kind_rejected(self):
        status, _ = req("GET", self.url(f"/credits/{SERVER}/leaderboard?kind=bogus"), token=BOT_TOKEN)
        self.assertEqual(status, 400)


class RepairLeaderboardTests(unittest.TestCase):
    """The Road Crew leaderboard reads straight off the existing dispatch table --
    no opt-in needed since a dispatch claim is never anonymous."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-repairs")
        registry = trust.Registry()
        registry.issue("repairs-fleet", "test-owner", trust.SCOPE_FULL, SERVER, token=FULL_TOKEN)
        registry.issue("repairs-mod", "test-owner", trust.SCOPE_MODERATOR, SERVER, token=MODERATOR_TOKEN)
        auth = Auth(registry, links=None, bot_hashes={Auth.hash_token(BOT_TOKEN)})
        cls.store = store
        cls.app = App(store, auth)
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.net = store.networks[SERVER]
        cls.map = store.map_hashes[SERVER]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def a_report(self, x, cond="HOLE"):
        return reference_client.build_report(x, 120, 0, "NETHER", self.net, self.map,
                                             SERVER, cond=cond)

    def enqueue_and_get_id(self, x):
        r = self.a_report(x)
        payload = {"road": r["road"], "seg": r["seg"], "along": r["along"]}
        _, body = req("POST", self.url(f"/dispatch/{SERVER}/queue"), token=MODERATOR_TOKEN, body=payload)
        return body["id"]

    def leaderboard(self, kind="crew"):
        status, body = req("GET", self.url(f"/credits/{SERVER}/leaderboard?kind={kind}"), token=BOT_TOKEN)
        self.assertEqual(status, 200)
        return {row["discordId"]: row["count"] for row in body["leaderboard"]}

    def test_a_human_completion_counts_toward_the_crew_leaderboard(self):
        did = self.enqueue_and_get_id(11000)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN, body={"discordId": "discord-crew-1"})
        req("POST", self.url(f"/dispatch/{did}/complete"), token=BOT_TOKEN, body={"discordId": "discord-crew-1"})
        self.assertEqual(self.leaderboard().get("discord-crew-1"), 1)

    def test_two_completions_by_the_same_person_count_twice(self):
        for x in (11300, 11600):
            did = self.enqueue_and_get_id(x)
            req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN, body={"discordId": "discord-crew-2"})
            req("POST", self.url(f"/dispatch/{did}/complete"), token=BOT_TOKEN, body={"discordId": "discord-crew-2"})
        self.assertEqual(self.leaderboard().get("discord-crew-2"), 2)

    def test_a_fleet_bot_tok_completion_never_appears_on_the_crew_leaderboard(self):
        did = self.enqueue_and_get_id(11900)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=FULL_TOKEN)
        req("POST", self.url(f"/dispatch/{did}/complete"), token=FULL_TOKEN)
        board = self.leaderboard()
        self.assertFalse(any(k.startswith("credits-fleet") for k in board))

    def test_survey_leaderboard_is_untouched_by_repair_activity(self):
        did = self.enqueue_and_get_id(12200)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN, body={"discordId": "discord-crew-3"})
        req("POST", self.url(f"/dispatch/{did}/complete"), token=BOT_TOKEN, body={"discordId": "discord-crew-3"})
        self.assertNotIn("discord-crew-3", self.leaderboard(kind="survey"))


class ConditionsAllHttpTests(unittest.TestCase):
    """GET /conditions/<server>/all -- the bot/moderator-only radar feed, the one
    place unpublished conditions are ever readable at all (the public
    /conditions/<server> never includes them, by design)."""

    @classmethod
    def setUpClass(cls):
        # k_anon=2 so a single anonymous report stays clearly unpublished (needs a
        # second corroborating source) -- exactly the "lone sketchy report" case
        # radar needs to see that the public route can't provide.
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-condall")
        registry = trust.Registry()
        registry.issue("condall-mod", "test-owner", trust.SCOPE_MODERATOR, SERVER, token=MODERATOR_TOKEN)
        auth = Auth(registry, links=None, bot_hashes={Auth.hash_token(BOT_TOKEN)})
        cls.app = App(store, auth)
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.net = store.networks[SERVER]
        cls.map = store.map_hashes[SERVER]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def a_report(self, x, cond="HOLE"):
        return reference_client.build_report(x, 120, 0, "NETHER", self.net, self.map,
                                             SERVER, cond=cond)

    def test_a_lone_unpublished_report_is_invisible_to_the_public_route(self):
        req("POST", self.url("/report"), body=self.a_report(13000))
        _, body = req("GET", self.url(f"/conditions/{SERVER}"))
        self.assertFalse(any(c["along"] == self.a_report(13000)["along"] for c in body["conditions"]))

    def test_the_same_lone_unpublished_report_is_visible_via_the_all_route(self):
        req("POST", self.url("/report"), body=self.a_report(13300))
        status, body = req("GET", self.url(f"/conditions/{SERVER}/all"), token=BOT_TOKEN)
        self.assertEqual(status, 200)
        match = [c for c in body["conditions"] if c["along"] == self.a_report(13300)["along"]]
        self.assertEqual(len(match), 1)
        self.assertFalse(match[0]["published"])

    def test_all_route_requires_bot_or_moderator_credential(self):
        status, _ = req("GET", self.url(f"/conditions/{SERVER}/all"))
        self.assertEqual(status, 403)
        status, _ = req("GET", self.url(f"/conditions/{SERVER}/all"), token=MODERATOR_TOKEN)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
