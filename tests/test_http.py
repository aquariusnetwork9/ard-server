"""End-to-end HTTP: reads are public (PROTOCOL.md SS7), writes work, unknown fields
rejected on the wire, trust-registry scopes (full/maintainer/moderator) gate what
each token can do."""
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
import sessions  # noqa: E402

GEO_DIR = ROOT / "geometry"
SERVER = "2b2t.org"
SERVER2 = "6b6t.org"
FULL_TOKEN = "FULL-SECRET"
MAINTAINER_TOKEN = "MAINTAINER-SECRET"
MODERATOR_TOKEN = "MODERATOR-SECRET"
OWNER_TOKEN = "OWNER-SECRET"
BOT_TOKEN = "BOT-SECRET"
# Maps to no registry scope and no linked identity -- proves a gate actually checks
# scope rather than "any non-empty token".
NOBODY_TOKEN = "SOME-RANDOM-TOKEN"

# A fake Discord OAuth verifier -- tests never make a real network call to
# discord.com, same posture as Store's injectable clock.
FAKE_DISCORD_CODES = {"good-code-1": "discord-user-1", "good-code-2": "discord-user-2",
                       "good-code-3": "discord-user-suspend-test",
                       "good-code-e2e": "discord-user-e2e",
                       "good-code-retract": "discord-user-retract-test",
                       "good-code-retract-2": "discord-user-retract-other",
                       "admin-login-code": "discord-admin-1", "mod-login-code": "discord-mod-1"}


def fake_discord_verify(discord_code):
    if discord_code not in FAKE_DISCORD_CODES:
        raise identity.DiscordOAuthError("unrecognized code")
    return FAKE_DISCORD_CODES[discord_code]


def req(method, url, token=None, body=None, cookie=None, extra_headers=None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = token
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def req_headers(method, url, token=None, body=None, cookie=None):
    """Like req() but also returns response headers -- needed to read Set-Cookie."""
    headers = {}
    data = None
    if token:
        headers["Authorization"] = token
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def session_cookie(headers):
    raw = headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("ard_session="):
            return part
    return None


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt")
        registry = trust.Registry()
        registry.issue("fleet-bot-1", "test-owner", trust.SCOPE_FULL, SERVER, token=FULL_TOKEN)
        registry.issue("highway-crew-1", "test-owner", trust.SCOPE_MAINTAINER, SERVER, token=MAINTAINER_TOKEN)
        registry.issue("mod-1", "test-owner", trust.SCOPE_MODERATOR, SERVER, token=MODERATOR_TOKEN)
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8)
        sess = sessions.SessionStore()
        # Dashboard access granted directly to a Discord identity (no bearer
        # token) -- what /admin/login resolves into a session for.
        registry.grant_to_discord("discord-admin-1", trust.SCOPE_ADMIN, SERVER, "test-owner")
        registry.grant_to_discord("discord-mod-1", trust.SCOPE_MODERATOR, SERVER, "test-owner")
        auth = Auth(registry, links=links, owner_hashes={Auth.hash_token(OWNER_TOKEN)}, sessions=sess)
        cls.registry = registry
        cls.links = links
        cls.sessions = sess
        cls.app = App(store, auth, discord_verify=fake_discord_verify,
                      discord_client_id="TEST-CLIENT-ID",
                      discord_redirect_uri="https://example.test/link.html")
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.net = store.networks[SERVER]
        cls.map = store.map_hashes[SERVER]
        cls.net2 = store.networks[SERVER2]
        cls.map2 = store.map_hashes[SERVER2]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def raw_get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def a_report(self, cond="HOLE", x=6000):
        return reference_client.build_report(x, 120, 0, "NETHER", self.net, self.map,
                                             SERVER, cond=cond)

    # --- reads are public (PROTOCOL.md SS7) --------------------------------------
    def test_health_is_open(self):
        code, body = req("GET", self.url("/health"))
        self.assertEqual(code, 200)
        self.assertIn(SERVER, body["servers"])

    def test_conditions_is_public(self):
        code, _ = req("GET", self.url(f"/conditions/{SERVER}"))
        self.assertEqual(code, 200)

    def test_geometry_is_public(self):
        code, body = req("GET", self.url(f"/geometry/{SERVER}"))
        self.assertEqual(code, 200)
        self.assertEqual(body["map"], self.map)

    def test_no_bulk_scrape_route(self):
        # Reads being public doesn't mean there's an "all servers / dump
        # everything" route -- server is still a required path segment.
        self.assertEqual(req("GET", self.url("/conditions"))[0], 404)
        self.assertEqual(req("GET", self.url("/dump"))[0], 404)

    # --- Phase 3: the public map website is static files off the same server -----
    def test_website_root_serves_index_html(self):
        code, r = self.raw_get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Aquarius Road Department", r)

    def test_website_asset_served_with_content_type(self):
        code, r = self.raw_get("/app.js")
        self.assertEqual(code, 200)

    def test_static_assets_are_never_cached(self):
        # Real production incident: Cloudflare (and browsers) cache common
        # static extensions like .js/.css by default even with NO Cache-Control
        # header at all -- a deploy went stale at the edge because of exactly
        # this. Every static response must opt out explicitly.
        with urllib.request.urlopen(self.url("/app.js"), timeout=5) as resp:
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_privacy_policy_is_served(self):
        code, r = self.raw_get("/privacy.html")
        self.assertEqual(code, 200)
        self.assertIn(b"Privacy Policy", r)

    def test_terms_of_service_is_served(self):
        code, r = self.raw_get("/terms.html")
        self.assertEqual(code, 200)
        self.assertIn(b"Terms of Service", r)

    def test_static_path_traversal_is_blocked(self):
        # Escaping website/ (e.g. to read server/highway_conditions.py) must 404,
        # not serve the file.
        code, _ = self.raw_get("/../server/highway_conditions.py")
        self.assertEqual(code, 404)

    # --- writes: Tier A (full scope) ---------------------------------------------
    def test_full_scope_write_then_public_read(self):
        r = self.a_report(x=6000)
        code, body = req("POST", self.url("/report"), token=FULL_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["tiers"], ["A"])
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        self.assertEqual(code, 200)
        self.assertTrue(any(c["cond"] == "HOLE" for c in body["conditions"]))

    def test_wire_rejects_unknown_field(self):
        r = self.a_report(x=6001)
        r["x"] = -1_200_000   # a smuggled base coordinate
        code, body = req("POST", self.url("/report"), token=FULL_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 0)
        self.assertEqual(len(body["rejected"]), 1)
        self.assertIn("unknown field", body["rejected"][0]["reason"])

    # --- writes: Tier M (maintainer scope) ---------------------------------------
    def test_maintainer_clears_unilaterally(self):
        r = self.a_report(cond="CLEAR", x=6002)
        code, body = req("POST", self.url("/report"), token=MAINTAINER_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["M"], "a CLEAR from a maintainer token auto-publishes")

    def test_maintainer_new_hazard_is_not_unilateral(self):
        # A maintainer grant deliberately does NOT include new-hazard publish rights
        # (PROTOCOL.md SS6.1) -- a HOLE report from this token falls through to the
        # same anonymous/Tier-C corroboration path as no token at all.
        r = self.a_report(cond="HOLE", x=6003)
        code, body = req("POST", self.url("/report"), token=MAINTAINER_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["C"])

    # --- moderation: submission is public, but the queue itself needs moderator scope
    def test_moderation_submit_is_public_but_independently_rate_limited(self):
        payload = {"v": 1, "server": SERVER, "map": self.map, "road": 0, "seg": 0,
                   "along": 2, "cond": "OBSTRUCTION_FULL", "observedY": 113}
        # No token at all -- SS7 opened this up same as the data reads, since it
        # used to piggyback its auth check on _require_read.
        self.assertEqual(req("POST", self.url("/moderation"), body=payload)[0], 200)
        # Decoupled from the (now-public, higher-limit) read gate on purpose --
        # otherwise opening up reads would have silently opened the moderation
        # queue to unlimited spam too. Hammer it (same test, same IP-keyed budget
        # as the call above) and expect a 429 eventually.
        codes = [req("POST", self.url("/moderation"), body=payload)[0] for _ in range(90)]
        self.assertIn(429, codes)

    def test_moderation_list_requires_moderator_scope_not_full(self):
        payload = {"v": 1, "server": SERVER, "map": self.map, "road": 0, "seg": 0,
                   "along": 3, "cond": "OBSTRUCTION_FULL", "observedY": 113}
        req("POST", self.url("/moderation"), body=payload)
        # Tier A (full scope) is NOT automatically moderator -- that conflation was
        # the actual bug this registry redesign fixed.
        self.assertEqual(req("GET", self.url(f"/moderation/{SERVER}"), token=FULL_TOKEN)[0], 403)
        self.assertEqual(req("GET", self.url(f"/moderation/{SERVER}"), token=NOBODY_TOKEN)[0], 403)
        code, body = req("GET", self.url(f"/moderation/{SERVER}"), token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(body["pending"]), 1)

    def test_moderation_list_is_scoped_to_its_own_server(self):
        # A moderator token scoped to 2b2t.org has no standing over 6b6t.org's queue.
        self.assertEqual(req("GET", self.url(f"/moderation/{SERVER2}"), token=MODERATOR_TOKEN)[0], 403)

    def test_moderation_resolve_requires_moderator_scope(self):
        payload = {"v": 1, "server": SERVER, "map": self.map, "road": 0, "seg": 0,
                   "along": 4, "cond": "OBSTRUCTION_FULL", "observedY": 113}
        req("POST", self.url("/moderation"), body=payload)
        pending = req("GET", self.url(f"/moderation/{SERVER}"), token=MODERATOR_TOKEN)[1]["pending"]
        mid = pending[-1]["id"]
        self.assertEqual(req("POST", self.url(f"/moderation/{mid}/approve"), token=FULL_TOKEN)[0], 403)
        code, body = req("POST", self.url(f"/moderation/{mid}/approve"), token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["resolved"])

    def test_quash_requires_moderator_scope(self):
        r = self.a_report(x=6500)
        req("POST", self.url("/report"), token=FULL_TOKEN, body=r)
        payload = {"server": SERVER, "road": r["road"], "seg": r["seg"],
                   "along": r["along"], "cond": r["cond"]}
        self.assertEqual(req("POST", self.url("/moderation/quash"), token=FULL_TOKEN, body=payload)[0], 403)
        self.assertEqual(req("POST", self.url("/moderation/quash"), token=NOBODY_TOKEN, body=payload)[0], 403)

    def test_quash_removes_a_published_condition(self):
        r = self.a_report(x=6501)
        req("POST", self.url("/report"), token=FULL_TOKEN, body=r)
        payload = {"server": SERVER, "road": r["road"], "seg": r["seg"],
                   "along": r["along"], "cond": r["cond"]}
        code, body = req("POST", self.url("/moderation/quash"), token=MODERATOR_TOKEN, body=payload)
        self.assertEqual(code, 200)
        self.assertTrue(body["quashed"])
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        self.assertFalse(any(c["along"] == r["along"] and c["road"] == r["road"]
                              for c in body["conditions"]))

    def test_quash_unknown_condition_is_404(self):
        payload = {"server": SERVER, "road": 0, "seg": 0, "along": 999999, "cond": "HOLE"}
        code, body = req("POST", self.url("/moderation/quash"), token=MODERATOR_TOKEN, body=payload)
        self.assertEqual(code, 404)
        self.assertFalse(body["quashed"])

    def test_quash_rejects_unknown_server(self):
        payload = {"server": "9b9t.org", "road": 0, "seg": 0, "along": 0, "cond": "HOLE"}
        self.assertEqual(req("POST", self.url("/moderation/quash"), token=OWNER_TOKEN, body=payload)[0], 400)

    def test_quash_is_scoped_to_its_own_server(self):
        payload = {"server": SERVER2, "road": 0, "seg": 0, "along": 0, "cond": "HOLE"}
        self.assertEqual(req("POST", self.url("/moderation/quash"), token=MODERATOR_TOKEN, body=payload)[0], 403)

    # --- registry: Owner only -----------------------------------------------------
    def test_registry_routes_require_owner(self):
        self.assertEqual(req("GET", self.url("/registry"), token=FULL_TOKEN)[0], 403)
        self.assertEqual(req("GET", self.url("/registry"), token=MODERATOR_TOKEN)[0], 403)
        self.assertEqual(req("POST", self.url("/registry"), token=FULL_TOKEN,
                              body={"holderLabel": "x", "scope": "full", "server": SERVER})[0], 403)
        code, body = req("GET", self.url("/registry"), token=OWNER_TOKEN)
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(body["active"]), 3)

    def test_registry_issue_rejects_unknown_server(self):
        code, body = req("POST", self.url("/registry"), token=OWNER_TOKEN,
                          body={"holderLabel": "x", "scope": "full", "server": "9b9t.org"})
        self.assertEqual(code, 400)

    def test_registry_issue_then_new_token_can_write_then_revoke_kills_it(self):
        code, body = req("POST", self.url("/registry"), token=OWNER_TOKEN,
                          body={"holderLabel": "temp-crew", "scope": "maintainer", "server": SERVER})
        self.assertEqual(code, 200)
        new_token, token_id = body["token"], body["tokenId"]

        r = self.a_report(cond="CLEAR", x=6004)
        code, body = req("POST", self.url("/report"), token=new_token, body=r)
        self.assertEqual(body["tiers"], ["M"], "a freshly issued maintainer token works immediately")

        code, body = req("DELETE", self.url(f"/registry/{token_id}"), token=OWNER_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["revoked"])

        r2 = self.a_report(cond="CLEAR", x=6005)
        code, body = req("POST", self.url("/report"), token=new_token, body=r2)
        self.assertEqual(body["tiers"], ["C"], "revocation takes effect on the very next request")

    def test_full_token_has_no_authority_on_a_different_server(self):
        # FULL_TOKEN was issued scoped to SERVER only -- reporting to SERVER2 with
        # it must fall through to anonymous, not silently ride on Tier A.
        r = reference_client.build_report(6100, 120, 0, "NETHER", self.net2, self.map2,
                                          SERVER2, cond="HOLE")
        code, body = req("POST", self.url("/report"), token=FULL_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["C"], "a 2b2t.org-scoped token has no standing on 6b6t.org")

    # --- account linking: Tier B (PROTOCOL.md SS6.2) ------------------------------
    def test_link_config_exposes_client_id_and_redirect_but_never_a_secret(self):
        code, body = req("GET", self.url("/link/config"))
        self.assertEqual(code, 200)
        self.assertTrue(body["configured"])
        self.assertEqual(body["clientId"], "TEST-CLIENT-ID")
        self.assertEqual(body["redirectUri"], "https://example.test/link.html")
        self.assertEqual(body["authorizeUrl"], "https://discord.com/oauth2/authorize")
        self.assertNotIn("secret", json.dumps(body).lower())

    def test_link_page_and_script_are_served(self):
        code, body = self.raw_get("/link.html")
        self.assertEqual(code, 200)
        self.assertIn(b"Link your Minecraft account", body)
        self.assertEqual(self.raw_get("/link.js")[0], 200)

    def test_link_flow_end_to_end(self):
        code, body = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-e2e", "server": SERVER})
        self.assertEqual(code, 200)
        link_code = body["code"]

        # Dedicated identity (good-code-e2e), not shared with any other test -- this
        # test submits a report (x=7000), and the reputation layer's travel-plausibility
        # check means reusing an identity that reports somewhere else too, close in real
        # wall-clock time (this class shares one Store/real clock across all its test
        # methods), would spuriously look like impossible travel between unrelated tests.
        code, body = req("POST", self.url("/link/complete"),
                          body={"linkCode": link_code, "discordCode": "good-code-e2e"})
        self.assertEqual(code, 200)
        b_token = body["token"]

        r = self.a_report(x=7000)
        code, body = req("POST", self.url("/report"), token=b_token, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["B"])

    def test_link_init_requires_mc_uid(self):
        self.assertEqual(req("POST", self.url("/link/init"), body={"server": SERVER})[0], 400)

    def test_link_init_rejects_unknown_server(self):
        code, _ = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-x", "server": "9b9t.org"})
        self.assertEqual(code, 400)

    def test_link_complete_rejects_unknown_link_code(self):
        code, _ = req("POST", self.url("/link/complete"),
                       body={"linkCode": "DEAD-BEEF", "discordCode": "good-code-1"})
        self.assertEqual(code, 400)

    def test_link_complete_rejects_bad_discord_code(self):
        _, body = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-bad-discord", "server": SERVER})
        code, _ = req("POST", self.url("/link/complete"),
                       body={"linkCode": body["code"], "discordCode": "not-a-real-code"})
        self.assertEqual(code, 400)

    def test_tier_b_corroboration_dedup_by_identity(self):
        # Two different UIDs linked to the SAME Discord identity must count as ONE
        # source -- the whole point of SS6.2's dedup rule.
        _, i1 = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-alt-1", "server": SERVER})
        _, l1 = req("POST", self.url("/link/complete"),
                     body={"linkCode": i1["code"], "discordCode": "good-code-1"})
        _, i2 = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-alt-2", "server": SERVER})
        _, l2 = req("POST", self.url("/link/complete"),
                     body={"linkCode": i2["code"], "discordCode": "good-code-1"})  # same identity
        tok1, tok2 = l1["token"], l2["token"]

        r = self.a_report(x=7100)
        req("POST", self.url("/report"), token=tok1, body=r)
        req("POST", self.url("/report"), token=tok2, body=r)
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(at_spot, [], "same identity via 2 UIDs is still only 1 distinct source")

        # A genuinely different identity IS a second distinct source -> publishes.
        _, i3 = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-other", "server": SERVER})
        _, l3 = req("POST", self.url("/link/complete"),
                     body={"linkCode": i3["code"], "discordCode": "good-code-2"})  # different identity
        req("POST", self.url("/report"), token=l3["token"], body=r)
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(len(at_spot), 1)

    def test_identity_suspend_requires_moderator(self):
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER}/discord-user-1/suspend"), token=FULL_TOKEN)[0], 403)
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER}/discord-user-1/suspend"), token=NOBODY_TOKEN)[0], 403)

    def test_identity_suspend_is_scoped_to_its_own_server(self):
        # MODERATOR_TOKEN is scoped to SERVER only -- no standing over SERVER2.
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER2}/discord-user-1/suspend"), token=MODERATOR_TOKEN)[0], 403)

    def test_moderator_suspend_demotes_tier_b_to_anonymous(self):
        # Dedicated identity (good-code-3), never touched by other tests -- suspend
        # actions are shared state (cls.links persists across the whole class), so
        # this must not collide with tests that expect discord-user-1/2 to stay live.
        _, i = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-suspend-me", "server": SERVER})
        _, l = req("POST", self.url("/link/complete"),
                    body={"linkCode": i["code"], "discordCode": "good-code-3"})
        token = l["token"]

        code, body = req("POST", self.url(f"/identity/{SERVER}/discord-user-suspend-test/suspend"),
                          token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["suspended"])

        r = self.a_report(x=7200)
        code, body = req("POST", self.url("/report"), token=token, body=r)
        self.assertEqual(body["tiers"], ["C"], "a suspended identity's token falls back to anonymous")

        code, body = req("POST", self.url(f"/identity/{SERVER}/discord-user-suspend-test/reinstate"),
                          token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["reinstated"])

    def test_moderator_suspend_retracts_past_corroboration(self):
        # Two dedicated identities (good-code-retract/-2), never touched by any other
        # test -- reusing an identity that also reports elsewhere in this class would
        # spuriously trip the reputation layer's travel-plausibility check, since this
        # whole class shares one Store/real clock across all its test methods (see
        # test_link_flow_end_to_end's identical note).
        _, i1 = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-retract-1", "server": SERVER})
        _, l1 = req("POST", self.url("/link/complete"),
                     body={"linkCode": i1["code"], "discordCode": "good-code-retract"})
        _, i2 = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-retract-2", "server": SERVER})
        _, l2 = req("POST", self.url("/link/complete"),
                     body={"linkCode": i2["code"], "discordCode": "good-code-retract-2"})  # a second, distinct identity

        r = self.a_report(x=7300)
        req("POST", self.url("/report"), token=l1["token"], body=r)
        req("POST", self.url("/report"), token=l2["token"], body=r)
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(len(at_spot), 1, "sanity: two distinct identities corroborate a publish")

        code, body = req("POST", self.url(f"/identity/{SERVER}/discord-user-retract-test/suspend"),
                          token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["suspended"])
        self.assertEqual(body["retracted"], 1, "the suspended identity's one corroborating source is removed")

        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(at_spot, [], "losing one of two corroborating sources drops it below k_tier_b")

        req("POST", self.url(f"/identity/{SERVER}/discord-user-retract-test/reinstate"), token=MODERATOR_TOKEN)


class AdminDashboardTests(unittest.TestCase):
    """The Discord-login admin dashboard: session cookies resolved from
    trust.py discord_grants, layered on top of (never replacing) the existing
    bearer-token registry gates."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-admin")
        registry = trust.Registry()
        registry.grant_to_discord("discord-admin-1", trust.SCOPE_ADMIN, SERVER, "test-owner")
        registry.grant_to_discord("discord-mod-1", trust.SCOPE_MODERATOR, SERVER, "test-owner")
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8)
        sess = sessions.SessionStore()
        auth = Auth(registry, links=links, owner_hashes={Auth.hash_token(OWNER_TOKEN)}, sessions=sess)
        cls.registry = registry
        cls.sessions = sess
        cls.app = App(store, auth, discord_verify=fake_discord_verify,
                      discord_client_id="TEST-CLIENT-ID",
                      discord_redirect_uri="https://example.test/link.html")
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def raw_get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def login(self, discord_code):
        code, body, headers = req_headers("POST", self.url("/admin/login"), body={"discordCode": discord_code})
        return code, body, session_cookie(headers)

    def test_admin_session_requires_login(self):
        self.assertEqual(req("GET", self.url("/admin/session"))[0], 401)

    def test_login_rejects_a_discord_identity_with_no_dashboard_grant(self):
        code, body, _ = self.login("good-code-1")  # discord-user-1: linked identity, no dashboard grant
        self.assertEqual(code, 403)

    def test_login_rejects_unrecognized_discord_code(self):
        code, body, _ = self.login("not-a-real-code")
        self.assertEqual(code, 400)

    def test_login_sets_a_working_session_cookie(self):
        code, body, cookie = self.login("admin-login-code")  # discord-admin-1
        self.assertEqual(code, 200)
        self.assertEqual(body["discordId"], "discord-admin-1")
        self.assertIn({"server": SERVER, "scope": trust.SCOPE_ADMIN}, body["grants"])
        self.assertIsNotNone(cookie, "Set-Cookie must be present on a successful login")

        code, body = req("GET", self.url("/admin/session"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(body["discordId"], "discord-admin-1")

    def test_logout_revokes_the_session(self):
        _, _, cookie = self.login("admin-login-code")
        code, body = req("POST", self.url("/admin/logout"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertEqual(req("GET", self.url("/admin/session"), cookie=cookie)[0], 401,
                          "the cookie must stop working the instant it's revoked")

    def test_moderator_session_can_reach_the_moderation_queue(self):
        _, _, cookie = self.login("mod-login-code")  # discord-mod-1: moderator on SERVER only
        payload = {"v": 1, "server": SERVER, "map": self.app.store.map_hashes[SERVER],
                   "road": 0, "seg": 0, "along": 10, "cond": "OBSTRUCTION_FULL", "observedY": 113}
        req("POST", self.url("/moderation"), body=payload)
        code, body = req("GET", self.url(f"/moderation/{SERVER}"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertGreaterEqual(len(body["pending"]), 1)

    def test_moderator_session_has_no_standing_on_a_different_server(self):
        _, _, cookie = self.login("mod-login-code")
        self.assertEqual(req("GET", self.url(f"/moderation/{SERVER2}"), cookie=cookie)[0], 403)

    def test_moderator_session_cannot_reach_admin_only_registry(self):
        _, _, cookie = self.login("mod-login-code")
        code, body = req("POST", self.url("/registry"), cookie=cookie,
                          body={"holderLabel": "x", "scope": "full", "server": SERVER})
        self.assertEqual(code, 403)

    def test_admin_session_can_issue_and_revoke_registry_tokens_on_its_own_server(self):
        _, _, cookie = self.login("admin-login-code")
        code, body = req("POST", self.url("/registry"), cookie=cookie,
                          body={"holderLabel": "session-issued", "scope": "maintainer", "server": SERVER})
        self.assertEqual(code, 200)
        token_id = body["tokenId"]

        code, body = req("GET", self.url("/registry"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(all(t["server"] == SERVER for t in body["active"]),
                         "a per-server admin's registry view is filtered to their own server(s)")

        code, body = req("DELETE", self.url(f"/registry/{token_id}"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(body["revoked"])

    def test_admin_session_has_no_registry_standing_on_a_different_server(self):
        _, _, cookie = self.login("admin-login-code")
        code, _ = req("POST", self.url("/registry"), cookie=cookie,
                       body={"holderLabel": "x", "scope": "full", "server": SERVER2})
        self.assertEqual(code, 403)

    def test_admin_session_can_grant_and_revoke_moderator_dashboard_access(self):
        _, _, cookie = self.login("admin-login-code")
        code, body = req("POST", self.url("/admin/grants"), cookie=cookie,
                          body={"discordId": "new-mod", "server": SERVER, "scope": trust.SCOPE_MODERATOR})
        self.assertEqual(code, 200)
        grant_id = body["grantId"]

        code, body = req("GET", self.url(f"/admin/grants?server={SERVER}"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(any(g["grantId"] == grant_id for g in body["grants"]))

        code, body = req("DELETE", self.url(f"/admin/grants/{grant_id}"), cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(body["revoked"])

    def test_admin_session_alone_cannot_mint_another_admin(self):
        # The one dashboard-adjacent action that stays gated behind the cold
        # Owner secret, never reachable from a session alone.
        _, _, cookie = self.login("admin-login-code")
        code, body = req("POST", self.url("/admin/grants"), cookie=cookie,
                          body={"discordId": "new-admin", "server": SERVER, "scope": trust.SCOPE_ADMIN})
        self.assertEqual(code, 403)

    def test_owner_token_can_mint_a_new_admin(self):
        code, body = req("POST", self.url("/admin/grants"), token=OWNER_TOKEN,
                          body={"discordId": "new-admin-2", "server": SERVER, "scope": trust.SCOPE_ADMIN})
        self.assertEqual(code, 200)
        self.assertEqual(self.registry.discord_scopes("new-admin-2", SERVER), {trust.SCOPE_ADMIN})

    def test_admin_dashboard_static_page_is_served(self):
        code, r = self.raw_get("/admin/")
        self.assertEqual(code, 200)
        self.assertIn(b"Aquarius Road Department", r)


class BotLinkTests(unittest.TestCase):
    """POST /link/bot-complete: a first-party Discord bot's alternate, non-OAuth
    completion path -- proves ITSELF with ARD_BOT_SECRET (Auth.is_bot) instead of
    a discordCode, since it already has a Discord-verified discord_id from its own
    gateway/interaction signature."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-bot")
        registry = trust.Registry()
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8)
        auth = Auth(registry, links=links, owner_hashes={Auth.hash_token(OWNER_TOKEN)},
                    bot_hashes={Auth.hash_token(BOT_TOKEN)})
        cls.links = links
        cls.app = App(store, auth)
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def init_code(self, mc_uid="mc-uid-bot-1", server=SERVER):
        _, body = req("POST", self.url("/link/init"), body={"mcUid": mc_uid, "server": server})
        return body["code"]

    def test_bot_credential_completes_a_link_and_reports_the_server(self):
        code = self.init_code(server=SERVER2)
        status, body = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                            body={"linkCode": code, "discordId": "discord-bot-user-1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["server"], SERVER2)
        self.assertIn("token", body)
        self.assertEqual(self.links.discord_identity_for(body["token"], SERVER2), "discord-bot-user-1")

    def test_missing_bot_credential_rejected(self):
        code = self.init_code()
        status, _ = req("POST", self.url("/link/bot-complete"),
                         body={"linkCode": code, "discordId": "discord-bot-user-2"})
        self.assertEqual(status, 401)

    def test_wrong_bot_credential_rejected(self):
        code = self.init_code()
        status, _ = req("POST", self.url("/link/bot-complete"), token="NOT-THE-BOT-SECRET",
                         body={"linkCode": code, "discordId": "discord-bot-user-3"})
        self.assertEqual(status, 401)

    def test_owner_token_alone_does_not_satisfy_the_bot_gate(self):
        # Owner and bot are separate, narrower-scoped trust classes -- Owner
        # shouldn't silently also unlock a route meant for a specific service.
        code = self.init_code()
        status, _ = req("POST", self.url("/link/bot-complete"), token=OWNER_TOKEN,
                         body={"linkCode": code, "discordId": "discord-bot-user-4"})
        self.assertEqual(status, 401)

    def test_unknown_code_rejected(self):
        status, body = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                            body={"linkCode": "DEAD-BEEF", "discordId": "discord-bot-user-5"})
        self.assertEqual(status, 400)

    def test_used_code_rejected_on_second_attempt(self):
        # Exercises the same peek_pending_server-returns-None branch that a
        # genuinely expired code would hit -- expiry itself is covered at the
        # LinkStore layer in test_identity.py (peek_pending_server + complete_link
        # both consult the same clock/TTL check).
        code = self.init_code()
        status, _ = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                         body={"linkCode": code, "discordId": "discord-bot-user-6"})
        self.assertEqual(status, 200)
        status, body = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                            body={"linkCode": code, "discordId": "discord-bot-user-7"})
        self.assertEqual(status, 400)

    def test_missing_fields_rejected(self):
        status, _ = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN, body={})
        self.assertEqual(status, 400)

class BotLinkRateLimitTests(unittest.TestCase):
    """Isolated in its own class/server (own limiter instance) so hammering this
    budget can't leak 429s into BotLinkTests' other methods -- unittest runs
    methods in alphabetical order within a class, and a shared limiter bit us
    once before (see test_moderation_submit_is_public_but_independently_rate_limited)."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-bot-rl")
        registry = trust.Registry()
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8)
        auth = Auth(registry, links=links, bot_hashes={Auth.hash_token(BOT_TOKEN)})
        cls.links = links
        cls.app = App(store, auth)
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_rate_limited(self):
        # Generate codes directly against the store (bypassing /link/init's own,
        # separately-keyed rate limit) so this purely exercises /link/bot-complete's
        # own budget.
        codes = [self.links.init_link(f"mc-uid-bot-rl-{i}", SERVER) for i in range(70)]
        statuses = [req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                         body={"linkCode": c, "discordId": f"discord-bot-rl-{i}"})[0]
                    for i, c in enumerate(codes)]
        self.assertIn(429, statuses)


class LinkConfigUnconfiguredTests(unittest.TestCase):
    """A deployment with no Discord app configured yet -- link.js needs a clean
    'not set up' signal here rather than a broken/partial authorize URL."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt2")
        auth = Auth(trust.Registry())
        cls.app = App(store, auth)  # no discord_client_id/redirect_uri/verify at all
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_link_config_reports_not_configured(self):
        code, body = req("GET", self.url("/link/config"))
        self.assertEqual(code, 200)
        self.assertEqual(body, {"configured": False})


class TrustedProxyTests(unittest.TestCase):
    """CF-Connecting-IP is honored only when the request's socket peer is itself
    trusted (default: loopback) -- exercises the real end-to-end HTTP path, on
    top of the pure-function coverage in test_client_ip.py."""

    @classmethod
    def setUpClass(cls):
        cls.store = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-proxy-trusted")
        cls.trusting_app = App(cls.store, Auth(trust.Registry()))  # default trusted_proxies (loopback)
        cls.trusting_srv = Server(("127.0.0.1", 0), cls.trusting_app)
        cls.trusting_port = cls.trusting_srv.server_address[1]
        cls.trusting_t = threading.Thread(target=cls.trusting_srv.serve_forever, daemon=True)
        cls.trusting_t.start()

        cls.store2 = Store(str(GEO_DIR), k_anon=2, ttl=1000, salt="testsalt-proxy-untrusted")
        cls.untrusting_app = App(cls.store2, Auth(trust.Registry()), trusted_proxies=[])
        cls.untrusting_srv = Server(("127.0.0.1", 0), cls.untrusting_app)
        cls.untrusting_port = cls.untrusting_srv.server_address[1]
        cls.untrusting_t = threading.Thread(target=cls.untrusting_srv.serve_forever, daemon=True)
        cls.untrusting_t.start()

        cls.net = cls.store.networks[SERVER]
        cls.map = cls.store.map_hashes[SERVER]
        cls.net2 = cls.store2.networks[SERVER]
        cls.map2 = cls.store2.map_hashes[SERVER]

    @classmethod
    def tearDownClass(cls):
        cls.trusting_srv.shutdown()
        cls.untrusting_srv.shutdown()

    def a_report(self, net, map_hash, x, cond="HOLE"):
        return reference_client.build_report(x, 120, 0, "NETHER", net, map_hash, SERVER, cond=cond)

    def test_cf_connecting_ip_is_honored_from_a_trusted_peer(self):
        url = f"http://127.0.0.1:{self.trusting_port}"
        r = self.a_report(self.net, self.map, x=8000)
        req("POST", url + "/report", body=r, extra_headers={"CF-Connecting-IP": "203.0.113.11"})
        req("POST", url + "/report", body=r, extra_headers={"CF-Connecting-IP": "203.0.113.12"})
        code, body = req("GET", url + f"/conditions/{SERVER}")
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(len(at_spot), 1, "two distinct claimed addresses must corroborate as two sources")

    def test_cf_connecting_ip_is_ignored_from_an_untrusted_peer(self):
        url = f"http://127.0.0.1:{self.untrusting_port}"
        r = self.a_report(self.net2, self.map2, x=8001)
        req("POST", url + "/report", body=r, extra_headers={"CF-Connecting-IP": "203.0.113.21"})
        req("POST", url + "/report", body=r, extra_headers={"CF-Connecting-IP": "203.0.113.22"})
        code, body = req("GET", url + f"/conditions/{SERVER}")
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(at_spot, [], "both requests share the real peer address -- still just one source")


if __name__ == "__main__":
    unittest.main()
