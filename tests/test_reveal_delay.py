"""Reveal delay (anti-tracking, 0.2.x): every report sits for a randomized
window -- Tier A/M shorter, Tier B/C longer -- before it's visible ANYWHERE:
the public /conditions feed, the bot/moderator-only /conditions/<server>/all
feed, the SSE stream, and the dispatch queue. This is a DISPLAY gate, layered
on top of (not a replacement for) the existing publish/corroboration logic --
see REVEAL_DELAY_RANGE and the visible_at plumbing in ingest()/query()/
list_dispatch()/enqueue_dispatch() in highway_conditions.py."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import Store, REVEAL_DELAY_RANGE, PUBLIC_EXTRA_DELAY_RANGE  # noqa: E402
import reference_client  # noqa: E402

GEO_DIR = ROOT / "geometry"
SERVER = "2b2t.org"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _FixedRand:
    """Always returns `lo` (the shorter edge of whatever range is passed) --
    makes the exact reveal moment a deterministic, assertable instant instead
    of a range, without needing to mock random.Random's internals."""
    def uniform(self, lo, hi):
        return lo


class RevealDelayTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_FixedRand(), k_anon=2, ttl=100000,
                           clock=self.clock, salt="testsalt-reveal")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def by_along(self, rows, along):
        return next((r for r in rows if r["along"] == along), None)

    # ---- basic gate: hidden until visible_at, both feeds alike ----
    def test_tier_m_report_is_published_but_not_yet_visible(self):
        r = self.report(1000, 0)
        v = self.store.ingest(r, "tok:m1", "M")
        self.assertTrue(v["published"], "publish/corroboration logic is untouched by the delay")
        self.assertEqual(self.by_along(self.store.query(SERVER), r["along"]), None,
                          "public feed must not show it before its reveal time")
        self.assertEqual(
            self.by_along(self.store.query(SERVER, include_unpublished=True), r["along"]), None,
            "the mod/bot-only feed (include_unpublished=True) gets the SAME gate -- private too")

    def test_becomes_visible_once_its_window_elapses(self):
        r = self.report(1100, 0)
        self.store.ingest(r, "tok:m1", "M")
        delay = REVEAL_DELAY_RANGE["M"][0]  # _FixedRand always draws the low edge
        self.clock.t += delay - 1
        self.assertIsNone(self.by_along(self.store.query(SERVER), r["along"]), "not quite yet")
        self.clock.t += 2
        self.assertIsNotNone(self.by_along(self.store.query(SERVER), r["along"]), "now revealed")

    # ---- tier-specific window ----
    def test_tier_am_gets_the_shorter_window(self):
        r = self.report(1200, 0)
        self.store.ingest(r, "tok:a1", "A")
        self.clock.t += REVEAL_DELAY_RANGE["A"][0] + 1
        self.assertIsNotNone(self.by_along(self.store.query(SERVER), r["along"]))

    def test_tier_bc_gets_the_longer_window(self):
        r = self.report(1300, 0)
        self.store.ingest(r, "tok:m-not-used", "M")  # placeholder to keep spacing obvious below
        r2 = self.report(1400, 0)
        self.store.ingest(r2, "10.0.0.1", "C")
        self.store.ingest(r2, "10.0.0.2", "C")  # k_anon=2 -> published
        # Advance past the Tier A/M window but nowhere near Tier B/C's -- must still be hidden.
        self.clock.t += REVEAL_DELAY_RANGE["A"][1] + 1
        self.assertIsNone(self.by_along(self.store.query(SERVER), r2["along"]),
                          "Tier C must use the longer B/C window, not A/M's")
        self.clock.t += REVEAL_DELAY_RANGE["C"][0]
        self.assertIsNotNone(self.by_along(self.store.query(SERVER), r2["along"]))

    # ---- corroboration can only pull reveal time forward, never push it back ----
    def test_better_tier_corroboration_pulls_reveal_time_forward(self):
        r = self.report(1500, 0)
        self.store.ingest(r, "10.0.0.1", "C")  # long C window starts now
        self.clock.t += 5
        self.store.ingest(r, "tok:m1", "M")  # a fleet M confirms shortly after -- shorter window
        # From the M report's own moment, M's (short) window should govern -- well before C's would.
        self.clock.t += REVEAL_DELAY_RANGE["M"][0] + 1
        self.assertIsNotNone(self.by_along(self.store.query(SERVER), r["along"]),
                             "an M corroboration must pull reveal time forward, not leave it at C's")

    def test_worse_tier_rereport_does_not_extend_the_timer(self):
        r = self.report(1600, 0)
        self.store.ingest(r, "tok:m1", "M")  # short M window
        self.clock.t += 5
        self.store.ingest(r, "10.0.0.1", "C")  # a later anon report must not push reveal back out
        self.clock.t += REVEAL_DELAY_RANGE["M"][0] - 3
        self.assertIsNotNone(self.by_along(self.store.query(SERVER), r["along"]),
                             "a same-or-worse tier re-report must never delay an already-set reveal")

    # ---- dispatch queue: same gate ----
    def test_auto_triggered_dispatch_entry_is_hidden_until_reveal_time(self):
        hole = self.report(7000, 0, cond="HOLE")
        self.store.ingest(hole, "10.0.0.1", "A")
        self.clock.t += 5
        self.store.ingest(self.report(7000, 0, cond="CLEAR"), "tok:m2", "M")
        self.clock.t += 5
        # A hazard reported after a recent clear triggers a "reopen" auto-enqueue (tier="A" here).
        self.store.ingest(self.report(7000, 0, cond="HOLE"), "10.0.0.3", "A")
        self.assertEqual(self.store.list_dispatch(SERVER), [],
                         "an auto-triggered dispatch entry must respect its own reveal delay")
        self.clock.t += REVEAL_DELAY_RANGE["A"][0] + 1
        self.assertEqual(len(self.store.list_dispatch(SERVER)), 1, "now within the reveal window")

    def test_manually_queued_dispatch_entry_is_immediately_visible(self):
        r = self.report(7200, 0)
        self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        self.assertEqual(len(self.store.list_dispatch(SERVER)), 1,
                         "a moderator manually queuing a spot has already decided to expose it now")

    # ---- SSE broadcast: same gate ----
    def drain(self, q):
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    def test_broadcast_does_not_fire_before_reveal_time(self):
        q = self.store.subscribe(SERVER)
        self.store.ingest(self.report(8000, 0), "tok:m1", "M")
        self.assertEqual(self.drain(q), [], "must not broadcast a not-yet-revealed report, published or not")

    def test_dispatch_entries_still_carry_coordinates_once_revealed(self):
        r = self.report(7300, 0)
        self.store.enqueue_dispatch(SERVER, r["road"], r["seg"], r["along"], "manual")
        entry = self.store.list_dispatch(SERVER)[0]
        self.assertIsInstance(entry["x"], float)
        self.assertIsInstance(entry["z"], float)

    # ---- public-only extra delay (stacks on top of the tier-based one) ----
    def test_mod_only_feed_is_unaffected_by_the_public_extra_delay(self):
        r = self.report(9000, 0)
        self.store.ingest(r, "tok:m1", "M")
        self.clock.t += REVEAL_DELAY_RANGE["M"][0] + 1  # past the tier delay only
        self.assertIsNotNone(self.by_along(self.store.query(SERVER, include_unpublished=True), r["along"]),
                             "mod/bot feed (public=False) only ever waits on the tier-based delay")

    def test_public_feed_also_waits_for_the_extra_delay(self):
        r = self.report(9100, 0)
        self.store.ingest(r, "tok:m1", "M")
        self.clock.t += REVEAL_DELAY_RANGE["M"][0] + 1  # tier delay elapsed, extra one hasn't
        self.assertIsNone(self.by_along(self.store.query(SERVER, public=True), r["along"]),
                          "public feed needs the tier delay AND the extra public-only delay")
        self.clock.t += PUBLIC_EXTRA_DELAY_RANGE[0] + 1
        self.assertIsNotNone(self.by_along(self.store.query(SERVER, public=True), r["along"]))

    def test_public_extra_delay_applies_regardless_of_tier(self):
        r = self.report(9200, 0)
        self.store.ingest(r, "10.0.0.1", "C")
        self.store.ingest(r, "10.0.0.2", "C")  # k_anon=2 -> published
        self.clock.t += REVEAL_DELAY_RANGE["C"][0] + 1
        self.assertIsNone(self.by_along(self.store.query(SERVER, public=True), r["along"]))
        self.clock.t += PUBLIC_EXTRA_DELAY_RANGE[0] + 1
        self.assertIsNotNone(self.by_along(self.store.query(SERVER, public=True), r["along"]))

    def test_broadcast_waits_for_the_public_extra_delay_too(self):
        q = self.store.subscribe(SERVER)
        r = self.report(9300, 0)
        self.store.ingest(r, "tok:m1", "M")
        self.clock.t += REVEAL_DELAY_RANGE["M"][0] + 1
        self.assertEqual(self.drain(q), [], "SSE is a public surface -- it needs the extra delay too")
        self.clock.t += PUBLIC_EXTRA_DELAY_RANGE[0] + 1
        self.store.ingest(r, "tok:m1", "M")  # a resend, once fully past both delays
        events = self.drain(q)
        self.assertEqual(len(events), 1, "now both delays have elapsed -- the resend broadcasts")
