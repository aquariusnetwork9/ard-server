"""list_hazard_episodes: groups list_report_log's flat per-event rows by
cond_id -- since `conditions` has a UNIQUE constraint on (server, road_canon,
seg, along, cond), a cond_id already IS one location+condition-type's full
resend history. Same audience/gating as list_report_log (Tier B/A/M only,
admin-only) -- this is that same data regrouped, not a new privilege level."""
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


class HazardEpisodesTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(str(GEO_DIR), rand=_ZeroRand(), k_anon=2, ttl=100000,
                           clock=self.clock, salt="testsalt-episodes")
        self.net = self.store.networks[SERVER]
        self.map = self.store.map_hashes[SERVER]

    def report(self, x, z, cond="HOLE"):
        r = reference_client.build_report(x, 120, z, "NETHER", self.net, self.map, SERVER,
                                          cond=cond, now=self.clock.t)
        self.assertIsNotNone(r)
        return r

    def test_resends_collapse_into_one_episode_with_multiple_events(self):
        r = self.report(3000, 0)
        for _ in range(5):
            self.store.ingest(r, "discord-resend", "B")
        episodes = self.store.list_hazard_episodes(SERVER)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(episodes[0]["events"]), 5)
        self.assertEqual(episodes[0]["cond"], "HOLE")

    def test_different_locations_produce_separate_episodes(self):
        self.store.ingest(self.report(3100, 0), "discord-a", "B")
        self.store.ingest(self.report(3200, 0), "discord-b", "B")
        episodes = self.store.list_hazard_episodes(SERVER)
        self.assertEqual(len(episodes), 2)
        self.assertEqual({len(e["events"]) for e in episodes}, {1})

    def test_hazard_then_clear_at_same_spot_are_separate_episodes_ordered_by_time(self):
        hole = self.report(3300, 0, cond="HOLE")
        self.store.ingest(hole, "discord-raiser", "B")
        self.clock.t += 10
        clear = self.report(3300, 0, cond="CLEAR")
        self.store.ingest(clear, "discord-clearer", "B")
        episodes = self.store.list_hazard_episodes(SERVER)
        # Same (road, seg, along) -- two distinct cond_ids (HOLE and CLEAR are
        # a different UNIQUE-constraint key each), ordered so the hazard's
        # own episode reads immediately before the one that resolved it.
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0]["cond"], "HOLE")
        self.assertEqual(episodes[1]["cond"], "CLEAR")
        self.assertEqual(episodes[0]["road"], episodes[1]["road"])
        self.assertEqual(episodes[0]["seg"], episodes[1]["seg"])
        self.assertEqual(episodes[0]["along"], episodes[1]["along"])
        self.assertLess(episodes[0]["firstSeen"], episodes[1]["firstSeen"])

    def test_tier_c_anonymous_reports_never_appear(self):
        r = self.report(3400, 0)
        self.store.ingest(r, "10.0.0.1", "C")
        self.store.ingest(r, "10.0.0.2", "C")
        self.assertEqual(self.store.list_hazard_episodes(SERVER), [])

    def test_since_filters_events_but_keeps_the_episode_alive_via_later_ones(self):
        r = self.report(3500, 0)
        self.store.ingest(r, "discord-early", "B")
        self.clock.t += 100
        cutoff = self.clock.t
        self.clock.t += 100
        self.store.ingest(r, "discord-late", "B")
        episodes = self.store.list_hazard_episodes(SERVER, since=cutoff)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(episodes[0]["events"]), 1, "the pre-cutoff event is filtered out")
        self.assertEqual(episodes[0]["events"][0]["discordId"], "discord-late")

    def test_since_excludes_an_episode_with_no_events_after_the_cutoff(self):
        r = self.report(3600, 0)
        self.store.ingest(r, "discord-old", "B")
        self.clock.t += 100
        cutoff = self.clock.t
        episodes = self.store.list_hazard_episodes(SERVER, since=cutoff)
        self.assertEqual(episodes, [])

    def test_events_within_an_episode_are_ordered_oldest_first(self):
        r = self.report(3700, 0)
        for name in ("first", "second", "third"):
            self.store.ingest(r, f"discord-{name}", "B")
            self.clock.t += 10
        episodes = self.store.list_hazard_episodes(SERVER)
        ids = [e["discordId"] for e in episodes[0]["events"]]
        self.assertEqual(ids, ["discord-first", "discord-second", "discord-third"])

    def test_episodes_ordered_by_road_seg_along_then_first_seen(self):
        self.store.ingest(self.report(4900, 0), "discord-a", "B")
        self.clock.t += 10
        self.store.ingest(self.report(100, 0), "discord-b", "B")
        # Independently derive the "correct" order straight from the raw rows
        # (not from list_hazard_episodes' own sort) so this doesn't just
        # restate whatever the implementation happens to produce.
        rows = self.store.db.execute(
            "SELECT road_canon, seg, along FROM conditions WHERE server=?", (SERVER,)).fetchall()
        canon2idx = self.net["_canon2idx"]
        expected_keys = sorted((canon2idx.get(canon), seg, along) for canon, seg, along in rows)
        episodes = self.store.list_hazard_episodes(SERVER)
        self.assertEqual(len(episodes), 2)
        actual_keys = [(e["road"], e["seg"], e["along"]) for e in episodes]
        self.assertEqual(actual_keys, expected_keys)

    def test_limit_caps_the_number_of_episodes(self):
        for i in range(5):
            self.store.ingest(self.report(4000 + i * 10, 0), f"discord-lim-{i}", "B")
        episodes = self.store.list_hazard_episodes(SERVER, limit=2)
        self.assertEqual(len(episodes), 2)

    def test_scoped_per_server(self):
        self.store.ingest(self.report(4100, 0), "discord-scope", "B")
        self.assertEqual(self.store.list_hazard_episodes("6b6t.org"), [])

    def test_quashed_condition_is_skipped_without_crashing(self):
        # Simulates a moderator's quash(): the conditions row is deleted
        # outright but report_log (an audit trail) still references its
        # cond_id -- _view_by_id must return None rather than crash, and
        # list_hazard_episodes must skip it cleanly rather than surface it.
        r = self.report(4200, 0)
        self.store.ingest(r, "discord-quashed", "B")
        row = self.store.db.execute(
            "SELECT id FROM conditions WHERE server=?", (SERVER,)).fetchone()
        self.store.db.execute("DELETE FROM conditions WHERE id=?", (row[0],))
        self.store.db.commit()
        self.assertEqual(self.store.list_hazard_episodes(SERVER), [])

    def test_counts_toward_corroboration_reflected_per_event(self):
        hole = self.report(4300, 0, cond="HOLE")
        self.store.ingest(hole, "discord-raiser-2", "B")
        clear = self.report(4300, 0, cond="CLEAR")
        self.store.ingest(clear, "discord-raiser-2", "B")
        episodes = self.store.list_hazard_episodes(SERVER)
        clear_episode = next(e for e in episodes if e["cond"] == "CLEAR")
        self.assertFalse(clear_episode["events"][0]["countsTowardCorroboration"])

    def test_episode_carries_current_aggregate_state(self):
        r = self.report(4400, 0)
        self.store.ingest(r, "discord-agg-1", "B")
        self.store.ingest(r, "discord-agg-2", "B")
        episodes = self.store.list_hazard_episodes(SERVER)
        self.assertEqual(episodes[0]["distinctSources"], 2)
        self.assertIn("confidence", episodes[0])
        self.assertIn("published", episodes[0])
