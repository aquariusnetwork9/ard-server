"""Aggregation: trust tiers, k-anonymity, TTL/decay, moderation queue."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import Store, TRUST_BASELINE, TRUST_MIN, TRUST_PENALTY, TRUST_BOOST  # noqa: E402
import reference_client  # noqa: E402

GEO_DIR = ROOT / "geometry"
SERVER = "2b2t.org"
SERVER2 = "6b6t.org"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _ZeroRand:
    """Deterministic stand-in for Store's rand= -- every reveal-delay draw
    comes out 0, so these tests (unrelated to the reveal-delay feature) keep
    seeing conditions the instant they're ingested. See test_reveal_delay.py
    for the feature's own dedicated tests."""
    def uniform(self, lo, hi):
        return 0.0


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r, "test position must be on-road")
        return r

    def test_tier_a_publishes_immediately(self):
        v = self.store.ingest(self.report(1000, 0), "10.0.0.1", "A")
        self.assertTrue(v["published"])
        self.assertEqual(v["tier"], "A")
        self.assertIsNotNone(v["x"])  # server re-derives an ON-road coordinate for display

    def test_tier_c_needs_k_distinct_sources(self):
        r = self.report(2000, 0)
        v1 = self.store.ingest(r, "10.0.0.1", "C")
        self.assertFalse(v1["published"], "one anonymous report is tentative")
        # same IP again -> still one distinct source
        v1b = self.store.ingest(r, "10.0.0.1", "C")
        self.assertEqual(v1b["distinctSources"], 1)
        self.assertFalse(v1b["published"])
        # a second distinct source reaches k=2 -> published
        v2 = self.store.ingest(r, "10.0.0.2", "C")
        self.assertEqual(v2["distinctSources"], 2)
        self.assertTrue(v2["published"])

    def test_tier_m_publishes_any_cond_unilaterally(self):
        # M is the top tier -- both a new hazard and a CLEAR from it auto-publish.
        v = self.store.ingest(self.report(2100, 0, cond="HOLE"), "tok:m1", "M")
        self.assertTrue(v["published"])
        self.assertEqual(v["tier"], "M")
        v = self.store.ingest(self.report(2150, 0, cond="CLEAR"), "tok:m1", "M")
        self.assertTrue(v["published"])
        self.assertEqual(v["tier"], "M")

    def test_tier_m_raise_then_own_clear_publishes(self):
        # The inspector cycle: find a clog, report it, physically clear it, report the
        # clear -- the SAME holder's clear publishes immediately, unlike every tier
        # below M where the raiser's own clear doesn't even count toward corroboration.
        hole = self.report(2170, 0, cond="HOLE")
        self.store.ingest(hole, "tok:inspector", "M")
        self.clock.t += 5
        v = self.store.ingest(self.report(2170, 0, cond="CLEAR"), "tok:inspector", "M")
        self.assertTrue(v["published"])
        rows = self.store.query(SERVER)
        self.assertFalse(any(r["cond"] == "HOLE" and r["along"] == hole["along"] for r in rows),
                         "the inspector's own clear resolves the hazard they raised")

    def test_tier_a_clear_needs_corroboration(self):
        # A raises unilaterally, but its CLEAR doesn't -- and its own raise never
        # counts toward its own clear.
        hole = self.report(2180, 0, cond="HOLE")
        self.store.ingest(hole, "tok:bot1", "A")
        self.clock.t += 5
        clear = self.report(2180, 0, cond="CLEAR")
        v = self.store.ingest(clear, "tok:bot1", "A")  # the raiser's own clear
        self.assertFalse(v["published"])
        self.assertEqual(v["distinctSources"], 0, "raiser's own clear doesn't corroborate")
        # Other, distinct A holders corroborate it (threshold = k_tier_b * clear_factor = 4 here).
        self.store.ingest(clear, "tok:bot2", "A")
        self.store.ingest(clear, "tok:bot3", "A")
        self.store.ingest(clear, "tok:bot4", "A")
        v = self.store.ingest(clear, "tok:bot5", "A")
        self.assertTrue(v["published"])
        rows = self.store.query(SERVER)
        self.assertFalse(any(r["cond"] == "HOLE" and r["along"] == hole["along"] for r in rows),
                         "the corroborated clear resolves the hazard")

    def test_tier_rank_m_beats_a_beats_c_on_merge(self):
        r = self.report(2200, 0)
        v1 = self.store.ingest(r, "10.0.0.1", "C")
        self.assertEqual(v1["tier"], "C")
        v2 = self.store.ingest(r, "tok:a1", "A")
        self.assertEqual(v2["tier"], "A", "A outranks C on merge")
        v3 = self.store.ingest(r, "tok:m1", "M")
        self.assertEqual(v3["tier"], "M", "M outranks A on merge")
        v4 = self.store.ingest(r, "10.0.0.4", "C")
        self.assertEqual(v4["tier"], "M", "a later low-tier report never downgrades the merged tier")

    # --- CLEAR <-> hazard reconciliation (PROTOCOL.md SS6.4) --------------------------
    def test_clear_suppresses_published_hazard_then_reopen_reveals_it_again(self):
        hole = self.report(7000, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "A")
        self.assertTrue(any(v["cond"] == "HOLE" and v["along"] == hole["along"]
                             for v in self.store.query(SERVER)), "hazard published before any clear")

        self.clock.t += 5
        self.store.ingest(self.report(7000, 0, cond="CLEAR"), "tok:m2", "M")  # Tier M clears unilaterally
        rows = self.store.query(SERVER)
        self.assertFalse(any(v["cond"] == "HOLE" and v["along"] == hole["along"] for v in rows),
                          "a newer published CLEAR suppresses the hazard it resolves")
        self.assertTrue(any(v["cond"] == "CLEAR" and v["along"] == hole["along"] for v in rows))

        # Reopen: a hazard reported AFTER the clear reappears -- no special flag
        # needed for visibility, it's simply newer than the clear now.
        self.clock.t += 5
        self.store.ingest(self.report(7000, 0, cond="HOLE"), "10.0.0.3", "A")
        rows = self.store.query(SERVER)
        self.assertTrue(any(v["cond"] == "HOLE" and v["along"] == hole["along"] for v in rows),
                         "a hazard reported after the clear is not suppressed")

    def test_reopen_within_window_flags_moderation(self):
        hole = self.report(7100, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "A")
        self.clock.t += 5
        self.store.ingest(self.report(7100, 0, cond="CLEAR"), "tok:m2", "M")
        self.assertEqual(self.store.list_moderation("pending"), [])

        self.clock.t += 5  # well within reopen_window (3600s default, ttl=1000s in this test)
        self.store.ingest(self.report(7100, 0, cond="HOLE"), "10.0.0.3", "A")
        pending = self.store.list_moderation("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "reopen")
        # The reopen is still ingested normally -- flagging doesn't drop the report.
        self.assertTrue(any(v["cond"] == "HOLE" and v["along"] == hole["along"]
                             for v in self.store.query(SERVER)))

    def test_reopen_outside_window_is_not_flagged(self):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, reopen_window=50, clock=self.clock, salt="testsalt")
        net, mh = store.networks[SERVER], store.map_hashes[SERVER]

        def r(cond):
            return reference_client.build_report(7400, 120, 0, "NETHER", net, mh, SERVER,
                                                   cond=cond, now=self.clock.t)
        store.ingest(r("HOLE"), "10.0.0.1", "A")
        self.clock.t += 5
        store.ingest(r("CLEAR"), "tok:m2", "M")
        self.clock.t += 100  # past reopen_window (50s) but well within ttl (1000s) -- clear still active
        store.ingest(r("HOLE"), "10.0.0.3", "A")
        self.assertEqual(store.list_moderation("pending"), [], "reopen outside the window isn't flagged")

    def test_clear_needs_clear_factor_times_normal_threshold(self):
        # k_anon=2 (setUp), clear_factor=2 (Store default) -> CLEAR needs 4 distinct sources.
        hole = self.report(7200, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "C")
        self.store.ingest(hole, "10.0.0.2", "C")  # k=2 -> HOLE published

        self.clock.t += 5
        clear = self.report(7200, 0, cond="CLEAR")
        self.store.ingest(clear, "10.0.0.10", "C")
        v = self.store.ingest(clear, "10.0.0.11", "C")
        self.assertEqual(v["distinctSources"], 2)
        self.assertFalse(v["published"], "2 distinct CLEAR sources < clear_factor*k_anon=4")

        self.store.ingest(clear, "10.0.0.12", "C")
        v = self.store.ingest(clear, "10.0.0.13", "C")
        self.assertEqual(v["distinctSources"], 4)
        self.assertTrue(v["published"])

    def test_clear_non_overlap_hazard_source_does_not_corroborate(self):
        hole = self.report(7300, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "C")
        self.store.ingest(hole, "10.0.0.2", "C")  # published HOLE; sources = {ip1, ip2}

        self.clock.t += 5
        clear = self.report(7300, 0, cond="CLEAR")
        # The SAME two sources try to clear what they just reported -- must not count.
        self.store.ingest(clear, "10.0.0.1", "C")
        v = self.store.ingest(clear, "10.0.0.2", "C")
        self.assertEqual(v["distinctSources"], 0, "hazard-raising sources are excluded from the clear's count")
        self.assertFalse(v["published"])

        # A genuinely new source DOES count.
        v = self.store.ingest(clear, "10.0.0.3", "C")
        self.assertEqual(v["distinctSources"], 1)

    def test_query_hides_unpublished(self):
        self.store.ingest(self.report(3000, 0), "10.0.0.1", "C")  # tentative
        self.assertEqual(self.store.query(SERVER), [])
        self.store.ingest(self.report(3000, 0), "10.0.0.2", "C")  # now k=2
        rows = self.store.query(SERVER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cond"], "HOLE")

    def test_ttl_expiry(self):
        self.store.ingest(self.report(4000, 0), "10.0.0.1", "A")
        self.assertEqual(len(self.store.query(SERVER)), 1)
        self.clock.t += 1001  # past ttl
        self.assertEqual(self.store.query(SERVER), [], "expired conditions drop out")

    def test_tier_b_survives_past_the_ordinary_ttl(self):
        # This class's Store uses ttl=1000; TIER_TTL_OVERRIDE gives Tier B a
        # 24h leash regardless, on the theory that a Tier B report's own decay
        # was raised independently of every other tier's.
        self.store.ingest(self.report(4100, 0), "discord-ttl-1", "B")
        self.clock.t += 1001  # well past the ordinary ttl=1000
        self.assertEqual(len(self.store.query(SERVER, include_unpublished=True)), 1,
                         "a Tier B condition must not expire on the ordinary (short) ttl")

    def test_tier_b_still_expires_after_24h(self):
        self.store.ingest(self.report(4200, 0), "discord-ttl-2", "B")
        self.clock.t += 24 * 3600 + 1
        self.assertEqual(self.store.query(SERVER, include_unpublished=True), [],
                         "Tier B's own 24h window still eventually expires it")

    def test_road_filter(self):
        self.store.ingest(self.report(5000, 0), "10.0.0.1", "A")   # z=0 axis
        # Query a road index that has no data -> empty; the data road -> present.
        data_road = self.store.query(SERVER)[0]["road"]
        self.assertEqual(len(self.store.query(SERVER, road_idx=data_road)), 1)
        other = 0 if data_road != 0 else 1
        self.assertEqual(self.store.query(SERVER, road_idx=other), [])

    def test_rejects_wrong_map(self):
        r = self.report(1000, 0)
        r["map"] = "sha256:ffffffffffffffff"
        with self.assertRaises(ValueError):
            self.store.ingest(r, "10.0.0.1", "A")

    def test_rejects_out_of_range_spatial(self):
        r = self.report(1000, 0)
        r["road"] = 999999
        with self.assertRaises(ValueError):
            self.store.ingest(r, "10.0.0.1", "A")

    # --- obstruction: lane-span union + road-width bound (defense-in-depth layer 2) --------
    def obstruction_report(self, x, z, lane_min, lane_max):
        r = self.report(x, z, cond="OBSTRUCTION_PARTIAL")
        r["laneMin"], r["laneMax"] = lane_min, lane_max
        return r

    def test_partial_obstruction_stores_lane_span(self):
        v = self.store.ingest(self.obstruction_report(6000, 0, -1, 1), "10.0.0.1", "A")
        self.assertEqual((v["laneMin"], v["laneMax"]), (-1, 1))
        self.assertEqual(v["cond"], "OBSTRUCTION_PARTIAL")

    def test_full_obstruction_has_null_lane_fields(self):
        r = self.report(6100, 0, cond="OBSTRUCTION_FULL")
        v = self.store.ingest(r, "10.0.0.1", "A")
        self.assertIsNone(v["laneMin"])
        self.assertIsNone(v["laneMax"])

    def test_corroborating_lane_spans_union_widen(self):
        r = self.obstruction_report(6200, 0, -1, 0)
        v1 = self.store.ingest(r, "10.0.0.1", "A")
        self.assertEqual((v1["laneMin"], v1["laneMax"]), (-1, 0))
        # a second, wider observation of the SAME (road,seg,along,cond) widens the union
        r2 = self.obstruction_report(6200, 0, 0, 2)
        v2 = self.store.ingest(r2, "10.0.0.2", "A")
        self.assertEqual((v2["laneMin"], v2["laneMax"]), (-1, 2))
        # a narrower follow-up observation does NOT shrink it back
        r3 = self.obstruction_report(6200, 0, 0, 0)
        v3 = self.store.ingest(r3, "10.0.0.3", "A")
        self.assertEqual((v3["laneMin"], v3["laneMax"]), (-1, 2))

    def test_lane_span_bounded_by_actual_road_width(self):
        # structurally valid (within the generic +-32 schema bound) but wider than this
        # road's real width -> the server's geometry-aware check must still reject it.
        r = self.obstruction_report(6300, 0, -10, 10)
        with self.assertRaises(ValueError):
            self.store.ingest(r, "10.0.0.1", "A")

    def test_moderation_queue(self):
        payload = {"v": 1, "server": SERVER, "map": self.map, "road": 0, "seg": 0,
                   "along": 3, "cond": "OBSTRUCTION_FULL", "observedY": 113}
        mid = self.store.add_moderation(payload)
        pending = self.store.list_moderation("pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], mid)
        self.assertTrue(self.store.resolve_moderation(mid, "approved"))
        self.assertEqual(self.store.list_moderation("pending"), [])

    def test_quash_removes_a_published_condition(self):
        r = self.report(6400, 0)
        self.store.ingest(r, "10.0.0.1", "A")
        rows = self.store.query(SERVER, road_idx=r["road"])
        self.assertTrue(any(c["along"] == r["along"] for c in rows))
        self.assertTrue(self.store.quash(SERVER, r["road"], r["seg"], r["along"], r["cond"]))
        rows = self.store.query(SERVER, road_idx=r["road"], include_unpublished=True)
        self.assertFalse(any(c["along"] == r["along"] for c in rows), "quash removes the row outright")

    def test_quash_unknown_condition_returns_false(self):
        self.assertFalse(self.store.quash(SERVER, 0, 0, 99999, "HOLE"))

    def test_quash_unknown_server_raises(self):
        with self.assertRaises(KeyError):
            self.store.quash("9b9t.org", 0, 0, 0, "HOLE")

    def test_quash_out_of_range_road_raises(self):
        with self.assertRaises(ValueError):
            self.store.quash(SERVER, 9999, 0, 0, "HOLE")

    def test_quash_logs_an_approved_moderation_record(self):
        r = self.report(6401, 0)
        self.store.ingest(r, "10.0.0.1", "A")
        self.store.quash(SERVER, r["road"], r["seg"], r["along"], r["cond"])
        approved = self.store.list_moderation("approved", server=SERVER)
        self.assertTrue(any(m["kind"] == "quash" for m in approved))

    def test_retract_identity_removes_matching_source_only(self):
        r = self.report(6402, 0)
        self.store.ingest(r, "discord-a", "B")
        v = self.store.ingest(r, "discord-b", "B")
        self.assertEqual(v["distinctSources"], 2)
        self.assertTrue(v["published"])
        removed = self.store.retract_identity(SERVER, "discord-a")
        self.assertEqual(removed, 1)
        rows = self.store.query(SERVER, road_idx=r["road"], include_unpublished=True)
        v2 = next(c for c in rows if c["along"] == r["along"])
        self.assertEqual(v2["distinctSources"], 1)
        self.assertFalse(v2["published"], "removing one of two corroborating sources drops below k_tier_b")

    def test_retract_identity_does_not_touch_other_sources(self):
        r = self.report(6403, 0)
        self.store.ingest(r, "discord-a", "B")
        self.store.ingest(r, "discord-b", "B")
        self.store.retract_identity(SERVER, "discord-a")
        rows = self.store.query(SERVER, road_idx=r["road"], include_unpublished=True)
        v = next(c for c in rows if c["along"] == r["along"])
        self.assertEqual(v["distinctSources"], 1, "the other identity's source must survive")

    def test_retract_identity_unknown_server_returns_zero(self):
        self.assertEqual(self.store.retract_identity("9b9t.org", "discord-a"), 0)


class ReputationTests(unittest.TestCase):
    """Pragmatic reputation layer: travel-plausibility + trust-weighted corroboration
    (PROTOCOL.md SS6.1's planned 'Phase 5, blind-token reputation', lighter version)."""

    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r, "test position must be on-road")
        return r

    def test_implausible_travel_excludes_report_from_corroboration(self):
        # "10.0.0.1" reports at x=1000, establishing a last-known position.
        self.store.ingest(self.report(1000, 0), "10.0.0.1", "C")
        self.clock.t += 2  # 2s later...
        # ...same source claims a spot ~99000 blocks away -- physically impossible in 2s.
        far = self.report(100000, 0)
        v = self.store.ingest(far, "10.0.0.1", "C")
        self.assertEqual(v["distinctSources"], 0,
                          "travel-implausible report must not count, even as this key's first source")
        # A genuinely different source for the SAME spot counts normally.
        v = self.store.ingest(far, "10.0.0.2", "C")
        self.assertEqual(v["distinctSources"], 1)
        self.assertFalse(v["published"], "1 real distinct source < k_anon=2")
        # A third distinct source reaches k=2 (the excluded first attempt never counted).
        v = self.store.ingest(far, "10.0.0.3", "C")
        self.assertEqual(v["distinctSources"], 2)
        self.assertTrue(v["published"])

    def test_plausible_travel_is_never_flagged(self):
        self.store.ingest(self.report(1000, 0), "10.0.0.1", "C")
        self.clock.t += 5  # 5s later, 10 blocks away -> 2 b/s, nowhere near the threshold
        near = self.report(1010, 0)
        v = self.store.ingest(near, "10.0.0.1", "C")
        self.assertEqual(v["distinctSources"], 1, "a physically reasonable move must still count")
        v = self.store.ingest(near, "10.0.0.2", "C")
        self.assertEqual(v["distinctSources"], 2)
        self.assertTrue(v["published"], "no false-positive suppression from ordinary travel")

    def test_fresh_identity_first_report_has_no_reference_point(self):
        # No prior claim exists yet, so there is nothing to compare against -- even an
        # objectively enormous jump from "nowhere" must never be flagged.
        v = self.store.ingest(self.report(100000, 0), "10.0.0.1", "C")
        self.assertEqual(v["distinctSources"], 1)

    def test_penalized_identity_contributes_reduced_weight_afterward(self):
        # Trip the penalty once...
        self.store.ingest(self.report(1000, 0), "10.0.0.1", "C")
        self.clock.t += 1
        v = self.store.ingest(self.report(100000, 0), "10.0.0.1", "C")
        self.assertEqual(v["distinctSources"], 0, "sanity: the penalty-triggering report itself excluded")
        identity_hash = self.store._identity_hash(SERVER, "10.0.0.1")
        self.assertAlmostEqual(self.store._get_trust(identity_hash), TRUST_BASELINE - TRUST_PENALTY)

        # ...then have the SAME identity make a perfectly plausible follow-up report
        # (small hop from its now-updated last-known position) at a NEW spot. HOLE, not
        # CLEAR -- CLEAR carries its own separate clear_factor multiplier on top of
        # k_anon, which would confound what this test is actually isolating.
        self.clock.t += 5
        spot = self.report(100010, 0, cond="HOLE")
        v = self.store.ingest(spot, "10.0.0.1", "C")
        self.assertEqual(v["distinctSources"], 1, "this report is itself plausible, so it counts")
        self.assertFalse(v["published"], "but at reduced (<1.0) weight, 1 source alone is short of k_anon=2")

        # A second, never-penalized source brings the WEIGHT sum, not just the headcount,
        # to k_anon=2's threshold worth of real trust.
        v = self.store.ingest(spot, "10.0.0.2", "C")
        self.assertEqual(v["distinctSources"], 2, "headcount is a plain count regardless of weight")
        self.assertFalse(v["published"],
                          "2 distinct sources but weighted sum (0.85 + 1.0 = 1.85) still < k_anon=2")

        v = self.store.ingest(spot, "10.0.0.3", "C")
        self.assertTrue(v["published"], "a third full-weight source finally clears the weighted threshold")

    def test_trust_recovers_toward_baseline_after_a_corroborated_contribution(self):
        # A lower (non-integer, allowed at the Store-object level even though the CLI
        # only accepts ints) threshold so this test can put the PENALIZED identity's
        # own contribution at the exact tipping point -- 1 full-weight source (1.0)
        # alone stays under 1.8, but adding the penalized 0.85 crosses it (1.85).
        # That's what proves the boost lands on the identity whose own report actually
        # helped, not just "whoever happened to publish something" some other way.
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=1.8, ttl=1000, clock=self.clock, salt="testsalt")
        net, mh = store.networks[SERVER], store.map_hashes[SERVER]

        def r(x, z, cond="HOLE"):
            return reference_client.build_report(x, 120, z, "NETHER", net, mh, SERVER,
                                                  cond=cond, now=self.clock.t)

        store.ingest(r(1000, 0), "10.0.0.1", "C")
        self.clock.t += 1
        store.ingest(r(100000, 0), "10.0.0.1", "C")  # trips the penalty
        identity_hash = store._identity_hash(SERVER, "10.0.0.1")
        penalized = store._get_trust(identity_hash)
        self.assertAlmostEqual(penalized, TRUST_BASELINE - TRUST_PENALTY)

        self.clock.t += 5
        spot = r(100010, 0, cond="HOLE")
        store.ingest(spot, "10.0.0.2", "C")               # 1.0 alone, < 1.8 -> not published
        v = store.ingest(spot, "10.0.0.1", "C")            # 1.0 + 0.85 = 1.85 -> crosses it
        self.assertTrue(v["published"], "sanity: this is the report that should tip it over")
        boosted = store._get_trust(identity_hash)
        self.assertAlmostEqual(boosted, penalized + TRUST_BOOST,
                                msg="contributing to a condition that reaches publication earns some trust back")
        self.assertLess(boosted, TRUST_BASELINE, "recovering, but not instantly back to full baseline")

    def test_trust_never_drops_below_the_floor(self):
        identity_hash = self.store._identity_hash(SERVER, "10.0.0.1")
        # Repeated penalties (far more than enough to go negative without a floor).
        for _ in range(20):
            self.store._adjust_trust(identity_hash, -TRUST_PENALTY, self.clock.t)
        self.assertEqual(self.store._get_trust(identity_hash), TRUST_MIN)

    def test_tier_a_bypasses_travel_plausibility(self):
        # Tier A's source_key is the caller IP, and this project's own fleet runs
        # several distinct bots behind the same VPS IP -- Tier A must never be
        # travel-plausibility-checked, or that alone would misfire against real usage.
        self.store.ingest(self.report(1000, 0), "10.0.0.1", "A")
        far = self.report(100000, 0)
        v = self.store.ingest(far, "10.0.0.1", "A")  # 0 elapsed time, ~99000 blocks -- would
        self.assertTrue(v["published"], "Tier A auto-publishes and is never travel-checked")


class StoreEventTests(unittest.TestCase):
    """on_event hook: reopen flags and trust-floor crossings surface as events."""

    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt",
                           on_event=lambda kind, d: self.events.append((kind, d)))
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def test_reopen_fires_event(self):
        self.store.ingest(self.report(8000, 0, cond="HOLE"), "10.0.0.1", "A")
        self.clock.t += 5
        self.store.ingest(self.report(8000, 0, cond="CLEAR"), "tok:m1", "M")
        self.clock.t += 5  # within reopen_window
        self.store.ingest(self.report(8000, 0, cond="HOLE"), "10.0.0.3", "A")
        kinds = [k for k, _ in self.events]
        self.assertIn("reopen", kinds)
        d = dict(self.events)["reopen"]
        self.assertEqual(d["server"], SERVER)
        self.assertEqual(d["cond"], "HOLE")

    def test_trust_floor_crossing_fires_once(self):
        ih = self.store._identity_hash(SERVER, "10.0.0.9")
        for _ in range(10):
            self.store._adjust_trust(ih, -TRUST_PENALTY, self.clock.t)
        floors = [d for k, d in self.events if k == "trust_floor"]
        self.assertEqual(len(floors), 1, "fires on the crossing, not on every further penalty")
        self.assertEqual(floors[0]["identityHash"], ih)

    def test_event_callback_error_never_breaks_ingest(self):
        self.store.on_event = lambda kind, d: (_ for _ in ()).throw(RuntimeError("boom"))
        self.store.ingest(self.report(8100, 0, cond="HOLE"), "10.0.0.1", "A")
        self.clock.t += 5
        self.store.ingest(self.report(8100, 0, cond="CLEAR"), "tok:m1", "M")
        self.clock.t += 5
        v = self.store.ingest(self.report(8100, 0, cond="HOLE"), "10.0.0.3", "A")
        self.assertTrue(v["published"], "a raising event callback must not fail the ingest")


class FakeOracle:
    def __init__(self, present_uuids=None, always_none=False):
        self.present_uuids = present_uuids or set()
        self.always_none = always_none
        self.calls = []

    def is_present(self, mc_uid):
        self.calls.append(mc_uid)
        if self.always_none:
            return None
        return mc_uid in self.present_uuids


class PresenceCheckTests(unittest.TestCase):
    """Tier B reports get checked against a per-server presence oracle when one
    is configured (A4's second rung). Excludes from corroboration + trust
    penalty on a confirmed-absent verdict; a None (unknown/unreachable) verdict
    must cost nothing."""

    def setUp(self):
        self.clock = Clock()
        self.oracle = FakeOracle(present_uuids={"mc-uid-present"})
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt",
                           presence_oracles={SERVER: self.oracle})
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def test_present_reporter_corroborates_normally(self):
        v = self.store.ingest(self.report(3000, 0), "discord-1", "B", mc_uid="mc-uid-present")
        self.assertEqual(v["distinctSources"], 1)

    def test_absent_reporter_excluded_from_corroboration(self):
        v = self.store.ingest(self.report(3100, 0), "discord-1", "B", mc_uid="mc-uid-absent")
        self.assertEqual(v["distinctSources"], 0,
                          "a confirmed-absent reporter's report must not corroborate")

    def test_absent_reporter_takes_a_trust_penalty(self):
        identity_hash = self.store._identity_hash(SERVER, "discord-1")
        self.store.ingest(self.report(3200, 0), "discord-1", "B", mc_uid="mc-uid-absent")
        self.assertAlmostEqual(self.store._get_trust(identity_hash), TRUST_BASELINE - TRUST_PENALTY)

    def test_unknown_oracle_verdict_costs_nothing(self):
        oracle = FakeOracle(always_none=True)
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt2",
                     presence_oracles={SERVER: oracle})
        net, mh = store.networks[SERVER], store.map_hashes[SERVER]
        r = reference_client.build_report(3300, 120, 0, "NETHER", net, mh, SERVER,
                                          cond="HOLE", now=self.clock.t)
        identity_hash = store._identity_hash(SERVER, "discord-1")
        v = store.ingest(r, "discord-1", "B", mc_uid="some-uid")
        self.assertEqual(v["distinctSources"], 1, "an unreachable oracle must not exclude the report")
        self.assertAlmostEqual(store._get_trust(identity_hash), TRUST_BASELINE, "and must not penalize")

    def test_no_oracle_configured_for_server_is_a_no_op(self):
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt3")
        net, mh = store.networks[SERVER], store.map_hashes[SERVER]
        r = reference_client.build_report(3400, 120, 0, "NETHER", net, mh, SERVER,
                                          cond="HOLE", now=self.clock.t)
        v = store.ingest(r, "discord-1", "B", mc_uid="whatever-uid")
        self.assertEqual(v["distinctSources"], 1)

    def test_no_mc_uid_is_a_no_op_even_with_an_oracle_configured(self):
        # Shouldn't happen in practice (Tier B always has a linked mc_uid), but
        # the check must degrade gracefully rather than crash on missing data.
        v = self.store.ingest(self.report(3500, 0), "discord-1", "B", mc_uid=None)
        self.assertEqual(v["distinctSources"], 1)
        self.assertEqual(self.oracle.calls, [], "no mc_uid means no oracle call at all")

    def test_tier_c_is_never_checked_against_presence(self):
        # Tier C has no linked mc_uid at all -- presence-check is Tier B only.
        v = self.store.ingest(self.report(3600, 0), "10.0.0.1", "C", mc_uid="mc-uid-absent")
        self.assertEqual(v["distinctSources"], 1, "mc_uid is meaningless for Tier C; must be ignored")



class IdentitySaltPersistenceTests(unittest.TestCase):
    """A5: identity_hash uses its own salt, separate from the per-condition
    source-hash salt, and MUST be stable across restarts for the reputation
    layer to mean anything -- see the module-level comment on Store.__init__."""

    def setUp(self):
        self.clock = Clock()

    def store(self, salt, identity_salt):
        return Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock,
                     salt=salt, identity_salt=identity_salt)

    def test_identity_hash_ignores_the_source_hash_salt(self):
        a = self.store(salt="source-salt-A", identity_salt="identity-salt-X")
        b = self.store(salt="source-salt-B", identity_salt="identity-salt-X")
        self.assertEqual(a._identity_hash(SERVER, "discord-1"), b._identity_hash(SERVER, "discord-1"),
                          "changing only the source-hash salt must not affect identity_hash")

    def test_identity_hash_changes_with_its_own_salt(self):
        a = self.store(salt="source-salt-A", identity_salt="identity-salt-X")
        b = self.store(salt="source-salt-A", identity_salt="identity-salt-Y")
        self.assertNotEqual(a._identity_hash(SERVER, "discord-1"), b._identity_hash(SERVER, "discord-1"))

    def test_source_hash_still_uses_its_own_salt_unaffected_by_identity_salt(self):
        net = self.store(salt="source-salt-A", identity_salt="X").networks[SERVER]
        canon = net["_canon"][0]
        a = self.store(salt="source-salt-A", identity_salt="identity-salt-X")
        b = self.store(salt="source-salt-A", identity_salt="identity-salt-Y")
        self.assertEqual(a._source_hash(SERVER, canon, 0, 0, "HOLE", "10.0.0.1"),
                          b._source_hash(SERVER, canon, 0, 0, "HOLE", "10.0.0.1"),
                          "changing only identity_salt must not affect the source hash")

    def test_trust_score_survives_a_simulated_restart(self):
        # Same identity_salt + same persistent db_path across two Store instances
        # models a real restart (a fresh process, the same on-disk state).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db_path = str(pathlib.Path(d) / "conditions.db")
            store1 = Store(str(GEO_DIR), rand=_ZeroRand(), db_path=db_path, k_anon=2, ttl=1000,
                           clock=self.clock, salt="rotating-1", identity_salt="stable-salt")
            ih = store1._identity_hash(SERVER, "discord-1")
            store1._adjust_trust(ih, -TRUST_PENALTY, self.clock.t)
            store1.db.commit()  # _adjust_trust relies on its caller to commit (ingest() does)
            self.assertAlmostEqual(store1._get_trust(ih), TRUST_BASELINE - TRUST_PENALTY)
            store1.db.close()

            # "Restart": a fresh Store, source-hash salt rotated (as it always
            # does), identity_salt held stable.
            store2 = Store(str(GEO_DIR), rand=_ZeroRand(), db_path=db_path, k_anon=2, ttl=1000,
                           clock=self.clock, salt="rotating-2", identity_salt="stable-salt")
            try:
                ih2 = store2._identity_hash(SERVER, "discord-1")
                self.assertEqual(ih, ih2, "same identity_salt -> same identity_hash across restart")
                self.assertAlmostEqual(store2._get_trust(ih2), TRUST_BASELINE - TRUST_PENALTY,
                                       msg="trust score must survive the restart")
            finally:
                store2.db.close()

    def test_trust_score_does_not_survive_if_identity_salt_also_changes(self):
        # The bug this fixes: build_app previously had no way to persist this at
        # all, so every restart silently reset every trust score to baseline.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db_path = str(pathlib.Path(d) / "conditions.db")
            store1 = Store(str(GEO_DIR), rand=_ZeroRand(), db_path=db_path, k_anon=2, ttl=1000,
                           clock=self.clock, identity_salt="salt-before-restart")
            ih = store1._identity_hash(SERVER, "discord-1")
            store1._adjust_trust(ih, -TRUST_PENALTY, self.clock.t)
            store1.db.commit()
            store1.db.close()

            store2 = Store(str(GEO_DIR), rand=_ZeroRand(), db_path=db_path, k_anon=2, ttl=1000,
                           clock=self.clock, identity_salt="salt-after-restart")
            try:
                ih2 = store2._identity_hash(SERVER, "discord-1")
                self.assertNotEqual(ih, ih2)
                self.assertAlmostEqual(store2._get_trust(ih2), TRUST_BASELINE,
                                       msg="a changed identity_salt orphans the old row -> baseline")
            finally:
                store2.db.close()



class BroadcastPublishOnlyTests(unittest.TestCase):
    """A6: SSE only ever streams published-state views -- an unpublished
    (tentative) report must never reach a subscriber, or it hands live
    corroboration-progress feedback to anyone watching the stream."""

    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def drain(self, q):
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    def test_unpublished_report_is_not_broadcast(self):
        q = self.store.subscribe(SERVER)
        v = self.store.ingest(self.report(4000, 0), "10.0.0.1", "C")  # k_anon=2, tentative
        self.assertFalse(v["published"])
        self.assertEqual(self.drain(q), [], "a below-threshold report must not reach subscribers")

    def test_publish_transition_is_broadcast(self):
        q = self.store.subscribe(SERVER)
        r = self.report(4100, 0)
        self.store.ingest(r, "10.0.0.1", "C")  # tentative, no broadcast
        self.assertEqual(self.drain(q), [])
        v2 = self.store.ingest(r, "10.0.0.2", "C")  # crosses k_anon=2 -> published
        self.assertTrue(v2["published"])
        events = self.drain(q)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["published"])

    def test_auto_publish_tier_broadcasts_immediately(self):
        q = self.store.subscribe(SERVER)
        self.store.ingest(self.report(4200, 0), "tok:m1", "M")
        events = self.drain(q)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["published"])

    def test_other_servers_subscribers_unaffected(self):
        q = self.store.subscribe(SERVER2)
        self.store.ingest(self.report(4300, 0), "tok:m1", "M")
        self.assertEqual(self.drain(q), [], "a subscriber to a different server gets nothing")

    def test_unsubscribe_stops_delivery(self):
        q = self.store.subscribe(SERVER)
        self.store.unsubscribe(q)
        self.store.ingest(self.report(4400, 0), "tok:m1", "M")
        self.assertEqual(self.drain(q), [])


class DispatchQueueTests(unittest.TestCase):
    """SS6.5: the dispatch queue itself (enqueue/claim/complete/sweep/priority)
    plus ingest()'s auto-triggers (reopen/conflict/low_trust) and auto-resolve
    on a fleet Tier A/M re-report."""

    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=10000, reopen_window=600,
                           clock=self.clock, salt="testsalt",
                           dispatch_ttl=100000, dispatch_claim_timeout=500)
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def queue(self):
        return self.store.list_dispatch(SERVER)

    # ---- raw queue mechanics ----
    def test_enqueue_then_list(self):
        r = self.report(9000, 0)
        did = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        q = self.queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["id"], did)
        self.assertEqual(q[0]["status"], "queued")
        self.assertEqual(q[0]["trigger"], "manual")

    def test_dispatch_entries_carry_rederived_coordinates(self):
        # Without x/z, a dispatch entry is just an opaque road/seg/along triple --
        # nothing a human reading the Discord embed can actually place on a map.
        r = self.report(9000, 0)
        self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        entry = self.queue()[0]
        self.assertIsInstance(entry["x"], float)
        self.assertIsInstance(entry["z"], float)
        self.assertAlmostEqual(entry["x"], 9000, delta=500)
        self.assertAlmostEqual(entry["z"], 0, delta=500)

    def test_repeated_enqueue_is_idempotent_and_escalates_priority(self):
        r = self.report(9010, 0)
        did1 = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "low_trust")
        did2 = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "conflict")
        self.assertEqual(did1, did2, "same spatial key -> same entry, not a duplicate")
        q = self.queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["trigger"], "conflict", "trigger field reflects the escalation")

    def test_lower_priority_reenqueue_does_not_downgrade(self):
        r = self.report(9020, 0)
        self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "conflict")
        before = self.queue()[0]["priority"]
        self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "low_trust")
        after = self.queue()[0]["priority"]
        self.assertEqual(before, after, "a lower-weight trigger must not lower an existing priority")

    def test_closer_to_spawn_ranks_higher_for_the_same_trigger(self):
        near = self.report(1000, 0)
        far = self.report(9500, 0)
        self.store.enqueue_dispatch(SERVER, far["road"], far["seg"], far["along"], "low_trust")
        self.store.enqueue_dispatch(SERVER, near["road"], near["seg"], near["along"], "low_trust")
        q = self.queue()
        self.assertEqual(len(q), 2)
        self.assertEqual(q[0]["along"], near["along"], "closer-to-spawn target sorts first")

    def test_claim_then_second_claim_fails(self):
        r = self.report(9030, 0)
        did = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        self.assertTrue(self.store.claim_dispatch(did, "tok:bot1"))
        self.assertFalse(self.store.claim_dispatch(did, "tok:bot2"), "already claimed")

    def test_claim_nonexistent_id_returns_none(self):
        self.assertIsNone(self.store.claim_dispatch(999999, "tok:bot1"))

    def test_complete_by_a_different_token_without_force_fails(self):
        r = self.report(9040, 0)
        did = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        self.store.claim_dispatch(did, "tok:bot1")
        self.assertFalse(self.store.complete_dispatch(did, "tok:bot2"))
        self.assertTrue(self.store.complete_dispatch(did, "tok:bot1"))

    def test_complete_with_force_ignores_claimant(self):
        r = self.report(9050, 0)
        did = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        self.store.claim_dispatch(did, "tok:bot1")
        self.assertTrue(self.store.complete_dispatch(did, "tok:someone-else", force=True))

    def test_claim_reverts_to_queued_after_claim_timeout(self):
        r = self.report(9060, 0)
        did = self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        self.store.claim_dispatch(did, "tok:bot1")
        self.clock.t += 501  # past dispatch_claim_timeout=500
        q = self.queue()  # triggers the lazy sweep
        self.assertEqual(q[0]["status"], "queued")
        self.assertIsNone(q[0]["claimedBy"])
        # and it's claimable again
        self.assertTrue(self.store.claim_dispatch(did, "tok:bot2"))

    def test_queued_entry_expires_after_dispatch_ttl(self):
        r = self.report(9070, 0)
        self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        self.clock.t += 100001  # past dispatch_ttl=100000
        self.assertEqual(self.queue(), [], "an unclaimed, expired entry drops out of the open queue")

    # ---- ingest() auto-triggers ----
    def test_reopen_triggers_a_dispatch_entry(self):
        hole = self.report(9100, 0, cond="HOLE")
        self.store.ingest(hole, "tok:m1", "M")
        self.store.ingest(self.report(9100, 0, cond="CLEAR"), "tok:m1", "M")
        self.clock.t += 10  # well within reopen_window=600
        self.store.ingest(self.report(9100, 0, cond="HOLE"), "10.0.0.9", "A")
        q = self.queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["trigger"], "reopen")

    def test_fast_clear_over_a_fresh_hazard_triggers_conflict(self):
        hole = self.report(9110, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "A")  # Tier A hazard auto-publishes
        self.clock.t += 10  # well within reopen_window=600 -- still "fresh"
        self.store.ingest(self.report(9110, 0, cond="CLEAR"), "tok:m1", "M")  # M clears unilaterally
        q = self.queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["trigger"], "conflict")

    def test_clear_over_a_stale_hazard_is_not_a_conflict(self):
        hole = self.report(9120, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "A")
        self.clock.t += 700  # past reopen_window=600 -- no longer "fresh"
        self.store.ingest(self.report(9120, 0, cond="CLEAR"), "tok:m1", "M")
        self.assertEqual(self.queue(), [])

    def test_tier_c_only_publish_triggers_low_trust(self):
        r = self.report(9130, 0)
        self.store.ingest(r, "10.0.0.1", "C")  # tentative
        v = self.store.ingest(r, "10.0.0.2", "C")  # k_anon=2 -> published, tier stays C
        self.assertTrue(v["published"])
        self.assertEqual(v["tier"], "C")
        q = self.queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["trigger"], "low_trust")

    def test_a_report_never_triggers_low_trust(self):
        self.store.ingest(self.report(9140, 0), "10.0.0.1", "A")  # publishes immediately, tier A
        self.assertEqual(self.queue(), [])

    def test_a_fresh_am_report_auto_resolves_a_claimed_entry(self):
        r = self.report(9150, 0)
        self.store.ingest(r, "10.0.0.1", "C")
        self.store.ingest(r, "10.0.0.2", "C")  # published, tier C -> low_trust queued
        did = self.queue()[0]["id"]
        self.store.claim_dispatch(did, "tok:bot1")
        # The dispatched bot's own Tier A/M observation at the exact same spot
        # settles it through the ordinary corroboration math -- no new verdict
        # logic, this just closes the queue entry.
        self.store.ingest(self.report(9150, 0, cond="CLEAR"), "tok:bot1", "M")
        self.assertEqual(self.queue(), [], "a completed (done) entry drops out of the open queue")

    def test_a_report_does_not_resolve_a_still_queued_entry(self):
        r = self.report(9160, 0)
        self.store.ingest(r, "10.0.0.1", "C")
        self.store.ingest(r, "10.0.0.2", "C")  # published, tier C -> low_trust queued, unclaimed
        self.store.ingest(self.report(9160, 0, cond="CLEAR"), "tok:bot1", "M")
        # Not claimed, so nothing to auto-resolve -- the entry is still open
        # (still worth a look even though this particular CLEAR already landed).
        self.assertEqual(len(self.queue()), 1)


if __name__ == "__main__":
    unittest.main()
