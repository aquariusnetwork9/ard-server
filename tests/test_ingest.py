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


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt")
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

    def test_tier_m_publishes_clear_unilaterally_but_not_new_hazard(self):
        # Tier M is what App._report assigns for a maintainer-scope token on a CLEAR
        # report specifically -- the Store itself just needs to auto-publish tier "M"
        # the same way it does "A". (The cond==CLEAR gating lives in App._report, not
        # here -- Store.ingest trusts whatever tier it's handed.)
        v = self.store.ingest(self.report(2100, 0, cond="CLEAR"), "10.0.0.1", "M")
        self.assertTrue(v["published"])
        self.assertEqual(v["tier"], "M")

    def test_tier_rank_a_beats_m_beats_c_on_merge(self):
        r = self.report(2200, 0)
        v1 = self.store.ingest(r, "10.0.0.1", "C")
        self.assertEqual(v1["tier"], "C")
        v2 = self.store.ingest(r, "10.0.0.2", "M")
        self.assertEqual(v2["tier"], "M", "M outranks C on merge")
        v3 = self.store.ingest(r, "10.0.0.3", "A")
        self.assertEqual(v3["tier"], "A", "A outranks M on merge")
        v4 = self.store.ingest(r, "10.0.0.4", "C")
        self.assertEqual(v4["tier"], "A", "a later low-tier report never downgrades the merged tier")

    # --- CLEAR <-> hazard reconciliation (PROTOCOL.md SS6.4) --------------------------
    def test_clear_suppresses_published_hazard_then_reopen_reveals_it_again(self):
        hole = self.report(7000, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "A")
        self.assertTrue(any(v["cond"] == "HOLE" and v["along"] == hole["along"]
                             for v in self.store.query(SERVER)), "hazard published before any clear")

        self.clock.t += 5
        self.store.ingest(self.report(7000, 0, cond="CLEAR"), "10.0.0.2", "A")  # Tier A clears unilaterally
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
        self.store.ingest(self.report(7100, 0, cond="CLEAR"), "10.0.0.2", "A")
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
        store = Store(str(GEO_DIR), k_anon=2, ttl=1000, reopen_window=50, clock=self.clock, salt="testsalt")
        net, mh = store.networks[SERVER], store.map_hashes[SERVER]

        def r(cond):
            return reference_client.build_report(7400, 120, 0, "NETHER", net, mh, SERVER,
                                                   cond=cond, now=self.clock.t)
        store.ingest(r("HOLE"), "10.0.0.1", "A")
        self.clock.t += 5
        store.ingest(r("CLEAR"), "10.0.0.2", "A")
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
        self.store = Store(str(GEO_DIR), k_anon=2, ttl=1000, clock=self.clock, salt="testsalt")
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
        store = Store(str(GEO_DIR), k_anon=1.8, ttl=1000, clock=self.clock, salt="testsalt")
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


if __name__ == "__main__":
    unittest.main()
