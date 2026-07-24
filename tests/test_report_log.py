"""Per-report audit log (Tier B and above only): ingest() writes one report_log
row per Tier A/M/B report -- naming who reported what -- and Store.list_report_log
reads it back joined against its condition's road/seg/along/cond. A deliberate,
narrow exception to the "no identity-to-specific-report link" privacy stance
elsewhere in this file (see the module-level note above ingest()'s trust-layer
code) -- Tier C (anonymous) is never logged here at all."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import Store  # noqa: E402
import reference_client  # noqa: E402

GEO_DIR = ROOT / "geometry"
SERVER = "2b2t.org"


class _ZeroRand:
    def uniform(self, lo, hi):
        return 0.0


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ReportLogTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=100000,
                           clock=self.clock, salt="testsalt-reportlog")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def test_tier_b_report_is_logged_with_discord_id(self):
        r = self.report(1000, 0)
        self.store.ingest(r, "discord-user-1", "B", mc_uid="mc-uid-1")
        rows = self.store.list_report_log(SERVER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tier"], "B")
        self.assertEqual(rows[0]["discordId"], "discord-user-1")
        self.assertIsNone(rows[0]["tokenId"])
        self.assertEqual(rows[0]["mcUid"], "mc-uid-1")
        self.assertEqual(rows[0]["cond"], "HOLE")
        self.assertIsInstance(rows[0]["x"], float)

    def test_tier_a_and_m_reports_are_logged_with_token_id(self):
        self.store.ingest(self.report(1100, 0), "tok:fleet-1", "A")
        self.store.ingest(self.report(1200, 0), "tok:inspector-1", "M")
        rows = self.store.list_report_log(SERVER)
        by_tier = {r["tier"]: r for r in rows}
        self.assertEqual(by_tier["A"]["tokenId"], "fleet-1")
        self.assertIsNone(by_tier["A"]["discordId"])
        self.assertEqual(by_tier["M"]["tokenId"], "inspector-1")

    def test_tier_c_anonymous_reports_are_never_logged(self):
        r = self.report(1300, 0)
        self.store.ingest(r, "10.0.0.1", "C")
        self.store.ingest(r, "10.0.0.2", "C")
        self.assertEqual(self.store.list_report_log(SERVER), [])

    def test_each_report_gets_its_own_row_not_deduped(self):
        # Unlike `sources` (INSERT OR REPLACE, corroboration-only), the audit
        # log keeps every individual report event -- a real activity trail.
        r = self.report(1400, 0)
        for _ in range(3):
            self.store.ingest(r, "discord-user-2", "B")
        rows = self.store.list_report_log(SERVER)
        self.assertEqual(len(rows), 3)

    def test_newest_first_and_limit_is_respected(self):
        for i in range(5):
            self.store.ingest(self.report(1500 + i * 10, 0), f"discord-user-{i}", "B")
        rows = self.store.list_report_log(SERVER, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["discordId"], "discord-user-4", "most recent first")

    def test_since_filters_out_older_entries(self):
        r1 = self.report(1600, 0)
        self.store.ingest(r1, "discord-user-a", "B")
        self.clock.t += 100
        cutoff = self.clock.t
        self.clock.t += 100
        r2 = self.report(1700, 0)
        self.store.ingest(r2, "discord-user-b", "B")
        rows = self.store.list_report_log(SERVER, since=cutoff)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["discordId"], "discord-user-b")

    def test_scoped_per_server(self):
        self.store.ingest(self.report(1800, 0), "discord-user-x", "B")
        self.assertEqual(self.store.list_report_log("6b6t.org"), [])

    def test_counts_toward_corroboration_reflected_in_log(self):
        # A raiser's own CLEAR of the same hazard doesn't count toward
        # corroboration (SS6.4 non-overlap) -- the log should say so.
        hole = self.report(1900, 0, cond="HOLE")
        self.store.ingest(hole, "discord-user-raiser", "B")
        clear = self.report(1900, 0, cond="CLEAR")
        self.store.ingest(clear, "discord-user-raiser", "B")
        rows = self.store.list_report_log(SERVER)
        clear_row = next(r for r in rows if r["cond"] == "CLEAR")
        self.assertFalse(clear_row["countsTowardCorroboration"])


class ReportLogPublicTests(unittest.TestCase):
    """list_report_log_public: Tier B only, gated by published + visiblePublic
    (see test_reveal_delay.py for the delay gate itself, exercised here with
    _ZeroRand so it's a non-factor and only the content/tier-filtering rules
    are under test)."""

    def setUp(self):
        self.clock = Clock()
        # k_tier_b=1 -- a single Tier B report is enough to publish, keeping
        # most of these tests to one report.
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, k_tier_b=1, ttl=100000,
                           clock=self.clock, salt="testsalt-reportlog-public")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def test_tier_b_report_appears_fully_anonymous(self):
        r = self.report(2000, 0)
        self.store.ingest(r, "discord-pub-1", "B", mc_uid="mc-pub-1")
        rows = self.store.list_report_log_public(SERVER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tier"], "B")
        self.assertNotIn("discordId", rows[0], "never even read off report_log, let alone returned")
        self.assertNotIn("mcUid", rows[0], "mc_uid could link two entries into one traveler's route")
        self.assertEqual(rows[0]["cond"], "HOLE")
        self.assertIsInstance(rows[0]["x"], float)

    def test_tier_a_and_m_never_appear(self):
        self.store.ingest(self.report(2100, 0), "tok:fleet-1", "A")
        self.store.ingest(self.report(2200, 0), "tok:inspector-1", "M")
        self.assertEqual(self.store.list_report_log_public(SERVER), [])

    def test_tier_c_never_appears(self):
        r = self.report(2300, 0)
        self.store.ingest(r, "10.0.0.1", "C")
        self.store.ingest(r, "10.0.0.2", "C")
        self.assertEqual(self.store.list_report_log_public(SERVER), [])

    def test_unpublished_tier_b_report_does_not_appear(self):
        # k_tier_b=1 here, so raise it back up for this one test by using a
        # fresh store with the ordinary default threshold instead.
        store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, k_tier_b=2, ttl=100000,
                      clock=self.clock, salt="testsalt-reportlog-public-unpub")
        net = store.networks[SERVER]
        map_hash = store.map_hashes[SERVER]
        r = reference_client.build_report(2400, 120, 0, "NETHER", net, map_hash, SERVER,
                                          cond="HOLE", now=self.clock.t)
        store.ingest(r, "discord-pub-lone", "B")  # only 1 of 2 needed -- tentative
        self.assertEqual(store.list_report_log_public(SERVER), [])

    def test_scoped_per_server(self):
        self.store.ingest(self.report(2500, 0), "discord-pub-2", "B")
        self.assertEqual(self.store.list_report_log_public("6b6t.org"), [])
