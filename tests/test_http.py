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
    discord_id = FAKE_DISCORD_CODES[discord_code]
    return discord_id, f"name-{discord_id}"


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


class _ZeroRand:
    """Deterministic stand-in for Store's rand= -- every reveal-delay draw
    comes out 0, so these tests (unrelated to the reveal-delay feature) keep
    seeing conditions/dispatch entries the instant they're ingested/enqueued.
    See test_reveal_delay.py for the feature's own dedicated tests."""
    def uniform(self, lo, hi):
        return 0.0


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt")
        registry = trust.Registry()
        registry.issue("fleet-bot-1", "test-owner", trust.SCOPE_FULL, SERVER, token=FULL_TOKEN)
        registry.issue("highway-crew-1", "test-owner", trust.SCOPE_MAINTAINER, SERVER, token=MAINTAINER_TOKEN)
        registry.issue("mod-1", "test-owner", trust.SCOPE_MODERATOR, SERVER, token=MODERATOR_TOKEN)
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8, require_ownership_proof=False)
        sess = sessions.SessionStore()
        # Dashboard access granted directly to a Discord identity (no bearer
        # token) -- what /admin/login resolves into a session for.
        registry.grant_to_discord("discord-admin-1", trust.SCOPE_ADMIN, SERVER, "test-owner")
        registry.grant_to_discord("discord-mod-1", trust.SCOPE_MODERATOR, SERVER, "test-owner")
        auth = Auth(registry, links=links, owner_hashes={Auth.hash_token(OWNER_TOKEN)}, sessions=sess,
                    bot_hashes={Auth.hash_token(BOT_TOKEN)})
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

    # --- writes: Tier M (maintainer scope, top tier) ------------------------------
    def test_maintainer_clears_unilaterally(self):
        r = self.a_report(cond="CLEAR", x=6002)
        code, body = req("POST", self.url("/report"), token=MAINTAINER_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["M"], "a CLEAR from a maintainer token auto-publishes")

    def test_maintainer_new_hazard_publishes_unilaterally(self):
        # M is the top tier (PROTOCOL.md SS6.1) -- a new hazard from a maintainer
        # token publishes immediately, same as its clears.
        r = self.a_report(cond="HOLE", x=6350)
        code, body = req("POST", self.url("/report"), token=MAINTAINER_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["M"])
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        self.assertTrue(any(c["cond"] == "HOLE" and c["tier"] == "M" for c in body["conditions"]))

    def test_full_scope_clear_is_not_unilateral(self):
        # An A (full-scope) holder's new hazard publishes immediately, but its CLEAR
        # of that hazard needs corroboration from OTHER sources -- the raiser's own
        # clear doesn't publish and doesn't even count toward the threshold.
        hole = self.a_report(cond="HOLE", x=6250)
        code, body = req("POST", self.url("/report"), token=FULL_TOKEN, body=hole)
        self.assertEqual(body["tiers"], ["A"])
        clear = self.a_report(cond="CLEAR", x=6250)
        code, body = req("POST", self.url("/report"), token=FULL_TOKEN, body=clear)
        self.assertEqual(code, 200)
        self.assertEqual(body["tiers"], ["A"])
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        conds = [c for c in body["conditions"] if c["along"] == hole["along"]]
        self.assertTrue(any(c["cond"] == "HOLE" for c in conds),
                        "the hazard stays published -- the raiser's own clear resolves nothing")
        self.assertFalse(any(c["cond"] == "CLEAR" for c in conds),
                         "an uncorroborated A clear is not published")

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
        # Same identity via 2 UIDs is still only 1 distinct source -- shows up
        # unconfirmed (a real Tier B identity reported it) but not published.
        self.assertEqual(len(at_spot), 1)
        self.assertEqual(at_spot[0]["distinctSources"], 1)
        self.assertFalse(at_spot[0]["published"])

        # A genuinely different identity IS a second distinct source -> publishes.
        _, i3 = req("POST", self.url("/link/init"), body={"mcUid": "mc-uid-other", "server": SERVER})
        _, l3 = req("POST", self.url("/link/complete"),
                     body={"linkCode": i3["code"], "discordCode": "good-code-2"})  # different identity
        req("POST", self.url("/report"), token=l3["token"], body=r)
        code, body = req("GET", self.url(f"/conditions/{SERVER}"))
        at_spot = [c for c in body["conditions"] if c["along"] == r["along"] and c["road"] == r["road"]]
        self.assertEqual(len(at_spot), 1)
        self.assertTrue(at_spot[0]["published"])

    def test_identity_suspend_requires_moderator(self):
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER}/discord-user-1/suspend"), token=FULL_TOKEN)[0], 403)
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER}/discord-user-1/suspend"), token=NOBODY_TOKEN)[0], 403)

    def test_identity_suspend_is_scoped_to_its_own_server(self):
        # MODERATOR_TOKEN is scoped to SERVER only -- no standing over SERVER2.
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER2}/discord-user-1/suspend"), token=MODERATOR_TOKEN)[0], 403)

    def test_identities_list_requires_moderator(self):
        self.assertEqual(req("GET", self.url(f"/identities/{SERVER}"), token=FULL_TOKEN)[0], 403)
        self.assertEqual(req("GET", self.url(f"/identities/{SERVER}"), token=NOBODY_TOKEN)[0], 403)

    def test_identities_list_shows_the_linked_roster_with_names(self):
        # Dedicated identity/UID so this doesn't collide with other tests sharing
        # this class's Store -- good-code-2 -> discord-user-2.
        _, init_body = req("POST", self.url("/link/init"),
                            body={"mcUid": "mc-uid-roster-1", "server": SERVER})
        req("POST", self.url("/link/complete"),
            body={"linkCode": init_body["code"], "discordCode": "good-code-2"})

        code, body = req("GET", self.url(f"/identities/{SERVER}"), token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        row = next(r for r in body["identities"] if r["discordId"] == "discord-user-2")
        self.assertEqual(row["discordUsername"], "name-discord-user-2")
        self.assertIn("mc-uid-roster-1", row["linkedUids"])
        self.assertFalse(row["suspended"])

    def test_identities_list_is_scoped_to_its_own_server(self):
        code, body = req("GET", self.url(f"/identities/{SERVER2}"), token=MODERATOR_TOKEN)
        self.assertEqual(code, 403, "MODERATOR_TOKEN has no standing on SERVER2")

    def test_identities_list_also_readable_by_the_bot(self):
        # The bot needs to read the roster itself to know which identities are
        # still missing a name before it can resolve and backfill them.
        code, _ = req("GET", self.url(f"/identities/{SERVER}"), token=BOT_TOKEN)
        self.assertEqual(code, 200)

    def test_identities_backfill_names_requires_bot_credential(self):
        self.assertEqual(
            req("POST", self.url(f"/identities/{SERVER}/names"), token=MODERATOR_TOKEN,
                body={"names": {"discord-user-1": "SomeName"}})[0], 401)
        self.assertEqual(
            req("POST", self.url(f"/identities/{SERVER}/names"), token=OWNER_TOKEN,
                body={"names": {"discord-user-1": "SomeName"}})[0], 401)

    def test_identities_backfill_names_fills_in_missing_names(self):
        # Linked directly against the LinkStore (bypassing HTTP/OAuth entirely,
        # same as a real pre-username-capture link would have been) so this
        # identity genuinely starts with no discordUsername -- every code this
        # class's fake_discord_verify knows about already resolves one.
        code = self.links.init_link("mc-uid-backfill-1", SERVER)
        self.links.complete_link(code, "discord-user-prebackfill")

        status, body = req("POST", self.url(f"/identities/{SERVER}/names"), token=BOT_TOKEN,
                            body={"names": {"discord-user-prebackfill": "BackfilledName",
                                            "discord-nobody": "X"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["updated"], 1, "only the identity that actually exists counts")

        _, roster = req("GET", self.url(f"/identities/{SERVER}"), token=MODERATOR_TOKEN)
        row = next(r for r in roster["identities"] if r["discordId"] == "discord-user-prebackfill")
        self.assertEqual(row["discordUsername"], "BackfilledName")

    def test_identities_backfill_names_rejects_bad_shape(self):
        code, _ = req("POST", self.url(f"/identities/{SERVER}/names"), token=BOT_TOKEN,
                       body={"names": "not-an-object"})
        self.assertEqual(code, 400)

    def test_identity_tier_requires_admin_not_just_moderator(self):
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER}/discord-user-1/tier"),
                token=MODERATOR_TOKEN, body={"tier": "A"})[0], 403)
        self.assertEqual(
            req("POST", self.url(f"/identity/{SERVER}/discord-user-1/tier"),
                token=FULL_TOKEN, body={"tier": "A"})[0], 403)

    def test_identity_tier_rejects_bad_value(self):
        code, _ = req("POST", self.url(f"/identity/{SERVER}/discord-user-1/tier"),
                       token=OWNER_TOKEN, body={"tier": "Z"})
        self.assertEqual(code, 400)

    def test_identity_tier_upgrade_to_full_mints_a_working_token(self):
        code = self.links.init_link("mc-uid-tier-a", SERVER)
        self.links.complete_link(code, "discord-tier-a")

        status, body = req("POST", self.url(f"/identity/{SERVER}/discord-tier-a/tier"),
                            token=OWNER_TOKEN, body={"tier": "A"})
        self.assertEqual(status, 200)
        self.assertEqual(body["tier"], "A")
        new_token = body["token"]

        r = self.a_report(cond="CLEAR", x=6100)
        status, report_body = req("POST", self.url("/report"), token=new_token, body=r)
        self.assertEqual(report_body["tiers"], ["A"], "the minted token really is a live Tier A credential")

        _, roster = req("GET", self.url(f"/identities/{SERVER}"), token=MODERATOR_TOKEN)
        row = next(rr for rr in roster["identities"] if rr["discordId"] == "discord-tier-a")
        self.assertEqual(row["tier"], "A")
        self.assertEqual(row["tierTokenId"], body["tokenId"])

    def test_identity_tier_downgrade_to_c_revokes_any_am_token_and_suspends(self):
        code = self.links.init_link("mc-uid-tier-down", SERVER)
        self.links.complete_link(code, "discord-tier-down")
        _, up = req("POST", self.url(f"/identity/{SERVER}/discord-tier-down/tier"),
                     token=OWNER_TOKEN, body={"tier": "M"})
        am_token = up["token"]

        status, body = req("POST", self.url(f"/identity/{SERVER}/discord-tier-down/tier"),
                            token=OWNER_TOKEN, body={"tier": "C"})
        self.assertEqual(status, 200)
        self.assertEqual(body["revokedTokens"], 1)

        r = self.a_report(cond="CLEAR", x=6110)
        status, report_body = req("POST", self.url("/report"), token=am_token, body=r)
        self.assertEqual(status, 200, "/report never 401s -- an unrecognized token just falls back")
        self.assertEqual(report_body["tiers"], ["C"], "the old Tier M token no longer resolves to anything")

        _, roster = req("GET", self.url(f"/identities/{SERVER}"), token=MODERATOR_TOKEN)
        row = next(rr for rr in roster["identities"] if rr["discordId"] == "discord-tier-down")
        self.assertEqual(row["tier"], "C")
        self.assertTrue(row["suspended"])

    def test_identity_tier_reissue_replaces_the_previous_am_token(self):
        code = self.links.init_link("mc-uid-tier-swap", SERVER)
        self.links.complete_link(code, "discord-tier-swap")
        _, first = req("POST", self.url(f"/identity/{SERVER}/discord-tier-swap/tier"),
                        token=OWNER_TOKEN, body={"tier": "M"})
        status, second = req("POST", self.url(f"/identity/{SERVER}/discord-tier-swap/tier"),
                              token=OWNER_TOKEN, body={"tier": "A"})
        self.assertEqual(status, 200)
        self.assertEqual(second["revokedTokens"], 1, "the earlier M token is revoked on the A re-grant")

        r = self.a_report(cond="CLEAR", x=6120)
        status, first_report_body = req("POST", self.url("/report"), token=first["token"], body=r)
        self.assertEqual(status, 200)
        self.assertEqual(first_report_body["tiers"], ["C"], "superseded token no longer resolves to M")
        status, report_body = req("POST", self.url("/report"), token=second["token"], body=r)
        self.assertEqual(report_body["tiers"], ["A"])

    def test_identity_tier_back_to_b_reinstates_a_suspended_identity(self):
        code = self.links.init_link("mc-uid-tier-b", SERVER)
        self.links.complete_link(code, "discord-tier-b")
        self.links.suspend("discord-tier-b", SERVER)

        status, body = req("POST", self.url(f"/identity/{SERVER}/discord-tier-b/tier"),
                            token=OWNER_TOKEN, body={"tier": "B"})
        self.assertEqual(status, 200)

        _, roster = req("GET", self.url(f"/identities/{SERVER}"), token=MODERATOR_TOKEN)
        row = next(rr for rr in roster["identities"] if rr["discordId"] == "discord-tier-b")
        self.assertEqual(row["tier"], "B")
        self.assertFalse(row["suspended"])

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
        # Still shows (unconfirmed, one real Tier B source left), but no longer published.
        self.assertEqual(len(at_spot), 1, "losing one of two corroborating sources drops it below k_tier_b")
        self.assertFalse(at_spot[0]["published"])

        req("POST", self.url(f"/identity/{SERVER}/discord-user-retract-test/reinstate"), token=MODERATOR_TOKEN)


class AdminDashboardTests(unittest.TestCase):
    """The Discord-login admin dashboard: session cookies resolved from
    trust.py discord_grants, layered on top of (never replacing) the existing
    bearer-token registry gates."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-admin")
        registry = trust.Registry()
        registry.grant_to_discord("discord-admin-1", trust.SCOPE_ADMIN, SERVER, "test-owner")
        registry.grant_to_discord("discord-mod-1", trust.SCOPE_MODERATOR, SERVER, "test-owner")
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8, require_ownership_proof=False)
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
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-bot")
        registry = trust.Registry()
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8, require_ownership_proof=False)
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


class OwnershipProofHttpTests(unittest.TestCase):
    """The Mojang ownership-proof gate end to end at the HTTP layer, with
    require_ownership_proof at its real default (True) -- every other HTTP test
    class disables it to test unrelated things. A fake mojang_verify stands in
    for the real network call (mojang_verify_join itself is unit-tested
    directly in test_identity.py)."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-ownership")
        registry = trust.Registry()
        cls.links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8)
        auth = Auth(registry, links=cls.links, bot_hashes={Auth.hash_token(BOT_TOKEN)})
        cls.verify_result = True

        def fake_mojang_verify(mc_uid, server_id):
            return cls.verify_result

        cls.app = App(store, auth, discord_verify=fake_discord_verify,
                      mojang_verify=fake_mojang_verify)
        cls.srv = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_link_init_returns_a_verify_server_id(self):
        status, body = req("POST", self.url("/link/init"),
                            body={"mcUid": "mc-uid-own-1", "server": SERVER})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("verifyServerId"))

    def test_complete_before_verifying_is_rejected(self):
        _, init_body = req("POST", self.url("/link/init"),
                            body={"mcUid": "mc-uid-own-2", "server": SERVER})
        status, body = req("POST", self.url("/link/complete"),
                            body={"linkCode": init_body["code"], "discordCode": "good-code-1"})
        self.assertEqual(status, 400)
        self.assertIn("not verified", body["error"])

    def test_verify_then_complete_succeeds(self):
        _, init_body = req("POST", self.url("/link/init"),
                            body={"mcUid": "mc-uid-own-3", "server": SERVER})
        code = init_body["code"]
        type(self).verify_result = True
        status, verify_body = req("POST", self.url("/link/verify-ownership"), body={"linkCode": code})
        self.assertEqual(status, 200)
        self.assertTrue(verify_body["verified"])
        status, body = req("POST", self.url("/link/complete"),
                            body={"linkCode": code, "discordCode": "good-code-1"})
        self.assertEqual(status, 200)
        self.assertIn("token", body)

    def test_failed_mojang_proof_is_rejected(self):
        _, init_body = req("POST", self.url("/link/init"),
                            body={"mcUid": "mc-uid-own-4", "server": SERVER})
        type(self).verify_result = False
        status, body = req("POST", self.url("/link/verify-ownership"),
                            body={"linkCode": init_body["code"]})
        self.assertEqual(status, 400)
        type(self).verify_result = True  # restore for later tests in this class

    def test_verify_ownership_unknown_code(self):
        status, body = req("POST", self.url("/link/verify-ownership"), body={"linkCode": "DEAD-BEEF"})
        self.assertEqual(status, 400)

    def test_verify_ownership_missing_field(self):
        status, _ = req("POST", self.url("/link/verify-ownership"), body={})
        self.assertEqual(status, 400)

    def test_bot_complete_also_requires_verification(self):
        # Both completion paths (website OAuth and bot-complete) share
        # complete_link, so the gate applies identically to each.
        _, init_body = req("POST", self.url("/link/init"),
                            body={"mcUid": "mc-uid-own-5", "server": SERVER})
        status, body = req("POST", self.url("/link/bot-complete"), token=BOT_TOKEN,
                            body={"linkCode": init_body["code"], "discordId": "discord-own-bot-1"})
        self.assertEqual(status, 400)
        self.assertIn("not verified", body["error"])


class BotLinkRateLimitTests(unittest.TestCase):
    """Isolated in its own class/server (own limiter instance) so hammering this
    budget can't leak 429s into BotLinkTests' other methods -- unittest runs
    methods in alphabetical order within a class, and a shared limiter bit us
    once before (see test_moderation_submit_is_public_but_independently_rate_limited)."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-bot-rl")
        registry = trust.Registry()
        links = identity.LinkStore(link_code_ttl=600, max_linked_uids=8, require_ownership_proof=False)
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
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt2")
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
        cls.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-proxy-trusted")
        cls.trusting_app = App(cls.store, Auth(trust.Registry()))  # default trusted_proxies (loopback)
        cls.trusting_srv = Server(("127.0.0.1", 0), cls.trusting_app)
        cls.trusting_port = cls.trusting_srv.server_address[1]
        cls.trusting_t = threading.Thread(target=cls.trusting_srv.serve_forever, daemon=True)
        cls.trusting_t.start()

        cls.store2 = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-proxy-untrusted")
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


class TrustedWriteCapTests(unittest.TestCase):
    """Per-token ceiling on registry-scoped (A/M) writes, with an alert on the trip.
    Own class/server: hammering this budget must not leak 429s elsewhere."""

    @classmethod
    def setUpClass(cls):
        import notify as notify_mod
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-cap")
        registry = trust.Registry()
        registry.issue("cap-bot", "test-owner", trust.SCOPE_FULL, SERVER, token=FULL_TOKEN)
        registry.issue("cap-crew", "test-owner", trust.SCOPE_MAINTAINER, SERVER, token=MAINTAINER_TOKEN)
        auth = Auth(registry, links=None)
        cls.alerts = []
        notifier = notify_mod.Notifier(
            url="https://ntfy.test/topic", async_send=False,
            transport=lambda title, msg, prio: cls.alerts.append((title, msg, prio)))
        cls.app = App(store, auth, notifier=notifier,
                      trusted_write_limit=5, trusted_write_window=60)
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

    def test_full_scope_token_is_capped_and_alerted(self):
        # Distinct along-buckets so every item is a distinct, valid report.
        results = []
        for i in range(8):
            r = self.a_report(x=10000 + i * 100)
            code, body = req("POST", self.url("/report"), token=FULL_TOKEN, body=r)
            self.assertEqual(code, 200)
            results.append((body["accepted"], body["rejected"]))
        accepted = sum(a for a, _ in results)
        rejected = [rej for _, rj in results for rej in rj]
        self.assertEqual(accepted, 5, "cap of 5 -> exactly 5 accepted")
        self.assertTrue(all(r["reason"] == "rate limited" for r in rejected))
        self.assertTrue(self.alerts, "tripping the cap raises an alert")
        title, msg, prio = self.alerts[0]
        self.assertIn("write cap", title)
        self.assertEqual(prio, "high")
        self.assertEqual(len(self.alerts), 1, "repeat trips inside min_interval stay throttled")

    def test_maintainer_token_has_its_own_independent_bucket(self):
        # The M token was untouched by the A token's exhaustion above (per-token,
        # not per-IP -- both arrive from loopback).
        r = self.a_report(x=20000, cond="CLEAR")
        code, body = req("POST", self.url("/report"), token=MAINTAINER_TOKEN, body=r)
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["tiers"], ["M"])


class DispatchHttpTests(unittest.TestCase):
    """SS6.7: auth surface for the dispatch queue -- fleet (A/M token) polls and
    claims, moderator can view/manually-enqueue/force-complete, the first-party
    bot can act on behalf of a Discord identity it vouches for (Highway
    Bot -- the Discord-role gating itself lives entirely bot-side, ARD only
    ever sees "the bot says this discordId"). Own class/server: a dedicated
    fresh Store."""

    @classmethod
    def setUpClass(cls):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, salt="testsalt-dispatch")
        registry = trust.Registry()
        registry.issue("dispatch-bot", "test-owner", trust.SCOPE_FULL, SERVER, token=FULL_TOKEN)
        registry.issue("dispatch-crew", "test-owner", trust.SCOPE_MAINTAINER, SERVER, token=MAINTAINER_TOKEN)
        registry.issue("dispatch-mod", "test-owner", trust.SCOPE_MODERATOR, SERVER, token=MODERATOR_TOKEN)
        registry.grant_to_discord("discord-dispatch-mod", trust.SCOPE_MODERATOR, SERVER, "test-owner")
        auth = Auth(registry, links=None, owner_hashes={Auth.hash_token(OWNER_TOKEN)},
                    bot_hashes={Auth.hash_token(BOT_TOKEN)})
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

    def manual_enqueue(self, x=9000):
        r = self.a_report(x)
        payload = {"road": r["road"], "seg": r["seg"], "along": r["along"]}
        code, body = req("POST", self.url(f"/dispatch/{SERVER}/queue"),
                         token=MODERATOR_TOKEN, body=payload)
        self.assertEqual(code, 200)
        return body["id"]

    def test_manual_enqueue_requires_moderator_scope(self):
        r = self.a_report(9000)
        payload = {"road": r["road"], "seg": r["seg"], "along": r["along"]}
        self.assertEqual(
            req("POST", self.url(f"/dispatch/{SERVER}/queue"), token=FULL_TOKEN, body=payload)[0], 403)
        self.assertEqual(
            req("POST", self.url(f"/dispatch/{SERVER}/queue"), token=NOBODY_TOKEN, body=payload)[0], 403)

    def test_list_requires_fleet_or_moderator_scope(self):
        self.manual_enqueue(9300)
        self.assertEqual(req("GET", self.url(f"/dispatch/{SERVER}"), token=NOBODY_TOKEN)[0], 403)
        code, body = req("GET", self.url(f"/dispatch/{SERVER}"), token=FULL_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(any(e["along"] == self.a_report(9300)["along"] for e in body["queue"]))
        self.assertEqual(req("GET", self.url(f"/dispatch/{SERVER}"), token=MODERATOR_TOKEN)[0], 200)

    def test_claim_requires_an_am_token_not_moderator(self):
        did = self.manual_enqueue(9600)
        self.assertEqual(
            req("POST", self.url(f"/dispatch/{did}/claim"), token=MODERATOR_TOKEN)[0], 403)
        self.assertEqual(
            req("POST", self.url(f"/dispatch/{did}/claim"), token=NOBODY_TOKEN)[0], 403)
        code, body = req("POST", self.url(f"/dispatch/{did}/claim"), token=FULL_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["claimed"])

    def test_double_claim_is_409(self):
        did = self.manual_enqueue(9900)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=FULL_TOKEN)
        code, body = req("POST", self.url(f"/dispatch/{did}/claim"), token=MAINTAINER_TOKEN)
        self.assertEqual(code, 409)

    def test_claim_unknown_id_is_404(self):
        code, _ = req("POST", self.url("/dispatch/999999/claim"), token=FULL_TOKEN)
        self.assertEqual(code, 404)

    def test_complete_by_a_different_token_without_moderator_scope_is_409(self):
        did = self.manual_enqueue(10200)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=FULL_TOKEN)
        code, _ = req("POST", self.url(f"/dispatch/{did}/complete"), token=MAINTAINER_TOKEN)
        self.assertEqual(code, 409)
        code, body = req("POST", self.url(f"/dispatch/{did}/complete"), token=FULL_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["completed"])

    def test_moderator_can_force_complete_someone_elses_claim(self):
        did = self.manual_enqueue(10500)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=FULL_TOKEN)
        code, body = req("POST", self.url(f"/dispatch/{did}/complete"), token=MODERATOR_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["completed"])

    def test_owner_can_force_complete_too(self):
        did = self.manual_enqueue(10800)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=MAINTAINER_TOKEN)
        code, body = req("POST", self.url(f"/dispatch/{did}/complete"), token=OWNER_TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(body["completed"])

    def test_complete_unclaimed_entry_is_409(self):
        did = self.manual_enqueue(11100)
        code, _ = req("POST", self.url(f"/dispatch/{did}/complete"), token=FULL_TOKEN)
        self.assertEqual(code, 409)

    def test_manual_enqueue_is_scoped_to_its_own_server(self):
        # MODERATOR_TOKEN is only scoped on SERVER, not SERVER2.
        payload = {"road": 0, "seg": 0, "along": 0}
        self.assertEqual(
            req("POST", self.url(f"/dispatch/{SERVER2}/queue"), token=MODERATOR_TOKEN, body=payload)[0], 403)

    # ---- bot-mediated (Discord) access, SS6.7 ----
    def test_bot_can_list_without_a_discord_id(self):
        # Listing is just polling to render the Discord queue view -- no
        # per-actor identity needed, unlike claim/complete.
        self.manual_enqueue(11400)
        code, _ = req("GET", self.url(f"/dispatch/{SERVER}"), token=BOT_TOKEN)
        self.assertEqual(code, 200)

    def test_bot_claim_without_discord_id_is_403(self):
        did = self.manual_enqueue(11700)
        code, _ = req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN, body={})
        self.assertEqual(code, 403)

    def test_bot_can_claim_on_behalf_of_a_discord_identity(self):
        did = self.manual_enqueue(12000)
        code, body = req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN,
                         body={"discordId": "discord-volunteer-1"})
        self.assertEqual(code, 200)
        self.assertTrue(body["claimed"])
        # A registry-scoped token can't then also claim the same entry.
        self.assertEqual(req("POST", self.url(f"/dispatch/{did}/claim"), token=FULL_TOKEN)[0], 409)

    def test_a_random_token_cannot_impersonate_the_bot(self):
        # Presenting a discordId only matters when the CALLER is the bot
        # itself (proven by ARD_BOT_SECRET) -- an arbitrary token can't just
        # tack a discordId onto the body to bypass the registry-token check.
        did = self.manual_enqueue(12300)
        code, _ = req("POST", self.url(f"/dispatch/{did}/claim"), token=NOBODY_TOKEN,
                      body={"discordId": "discord-volunteer-1"})
        self.assertEqual(code, 403)

    def test_bot_can_complete_its_own_claim(self):
        did = self.manual_enqueue(12600)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN,
            body={"discordId": "discord-volunteer-2"})
        code, body = req("POST", self.url(f"/dispatch/{did}/complete"), token=BOT_TOKEN,
                         body={"discordId": "discord-volunteer-2"})
        self.assertEqual(code, 200)
        self.assertTrue(body["completed"])

    def test_bot_cannot_complete_a_different_discord_identitys_claim(self):
        did = self.manual_enqueue(12900)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN,
            body={"discordId": "discord-volunteer-3"})
        code, _ = req("POST", self.url(f"/dispatch/{did}/complete"), token=BOT_TOKEN,
                      body={"discordId": "discord-someone-else"})
        self.assertEqual(code, 409)

    def test_bot_can_force_complete_for_a_discord_moderator_grant(self):
        # discord-dispatch-mod holds a discord_grants moderator scope on
        # SERVER (see setUpClass) -- the bot vouching for THAT identity can
        # force-close someone else's claim, same as a moderator token/session.
        did = self.manual_enqueue(13200)
        req("POST", self.url(f"/dispatch/{did}/claim"), token=BOT_TOKEN,
            body={"discordId": "discord-volunteer-4"})
        code, body = req("POST", self.url(f"/dispatch/{did}/complete"), token=BOT_TOKEN,
                         body={"discordId": "discord-dispatch-mod"})
        self.assertEqual(code, 200)
        self.assertTrue(body["completed"])



if __name__ == "__main__":
    unittest.main()
