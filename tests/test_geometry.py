"""Core privacy-invariant tests: off-highway coords are unrepresentable."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import geometry  # noqa: E402
from geometry import load_network, map_hash, rederive, nearest_allowed  # noqa: E402
import reference_client  # noqa: E402

GEO = ROOT / "geometry" / "2b2t.org.nether_highways.json"
SERVER = "2b2t.org"

# A fake off-highway "base" within 20k of 0,0 (per the no-real-coords standing order):
#   not on an axis/diagonal (|x| != |z|), not on a 5k grid line, ring, or diamond.
FAKE_BASE = (12500.0, 7500.0)   # nearest road is ~2500 blocks away


class GeometryInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = load_network(GEO)
        cls.map = map_hash(cls.net)

    def report(self, x, y, z, **kw):
        return reference_client.build_report(x, y, z, kw.pop("dim", "NETHER"),
                                             self.net, self.map, SERVER, **kw)

    # --- the headline invariant -------------------------------------------------
    def test_off_road_base_yields_no_report(self):
        r = self.report(FAKE_BASE[0], 120, FAKE_BASE[1])
        self.assertIsNone(r, "an off-highway base must produce NO report at all")

    def test_report_carries_no_coordinate_field(self):
        r = self.report(1000, 120, 0)
        self.assertIsNotNone(r)
        for forbidden in ("x", "z", "dist", "distance", "offset", "yaw", "pitch"):
            self.assertNotIn(forbidden, r)
        self.assertLessEqual(set(r), {"v", "server", "map", "road", "seg", "along", "cond", "sev", "ts"})

    def test_perpendicular_offset_is_destroyed(self):
        # Three points at the same x on the z=0 axis, differing only in the perpendicular
        # (z) offset. They must produce IDENTICAL (road, seg, along) — the offset is gone.
        # x=1234 is clear of every ring/grid/diamond crossing, so all three snap to the axis
        # (at a crossing, moving in z would legitimately snap onto the crossing road instead).
        base = self.report(1234, 120, 0.0)
        reps = [self.report(1234, 120, dz) for dz in (0.0, 1.5, 2.9)]
        self.assertTrue(all(x is not None for x in reps))
        for r in reps:
            self.assertEqual((r["road"], r["seg"], r["along"]),
                             (base["road"], base["seg"], base["along"]))

    def test_rederive_stays_on_road_within_one_bucket(self):
        r = self.report(1000, 120, 2.0)
        self.assertIsNotNone(r)
        x, z = rederive(self.net, r["road"], r["seg"], r["along"])
        # Re-derived point must be ON the road and within a bucket of the true projection (1000,0).
        self.assertLessEqual(abs(x - 1000), geometry.DEFAULT_BUCKET)
        self.assertLessEqual(abs(z - 0), geometry.DEFAULT_BUCKET)

    # --- gates ------------------------------------------------------------------
    def test_y_must_be_120(self):
        self.assertIsNone(self.report(1000, 115, 0))
        self.assertIsNone(self.report(1000, 122, 0))
        self.assertIsNotNone(self.report(1000, 120, 0))

    def test_dimension_gate(self):
        self.assertIsNone(self.report(1000, 120, 0, dim="OVERWORLD"))

    def test_radius_cap(self):
        # On the axis at x=1000 but a tiny cap -> suppressed.
        self.assertIsNone(self.report(1000, 120, 0, max_report_radius=500))
        self.assertIsNotNone(self.report(1000, 120, 0, max_report_radius=5000))

    # --- spatially-tiered road-set policy --------------------------------------
    def test_beyond_100k_axis_allowed_ring_not(self):
        # On the z=0 axis at 125k -> allowed (axis goes all the way to the world border).
        self.assertIsNotNone(self.report(125000, 120, 0))
        # On the 125k ring but off any axis/diagonal -> NOT reportable beyond 100k.
        self.assertIsNone(self.report(125000, 120, 20000))

    def test_within_50k_grid_allowed(self):
        # A 5k grid line at x=10000 (within 100k) is reportable.
        self.assertIsNotNone(self.report(10000, 120, 3210))

    def test_ring_between_50k_and_100k_now_allowed(self):
        # The 55k/62.5k/100k rings sit beyond the OLD 50k threshold but within the
        # current 100k one -- off-axis points on them must be reportable.
        self.assertIsNotNone(self.report(55000, 120, 20000), "55k ring, off-axis")
        self.assertIsNotNone(self.report(62500, 120, 20000), "62.5k ring, off-axis")
        # Exactly at the 100k boundary (inclusive) -> still allowed.
        self.assertIsNotNone(self.report(100000, 120, 20000), "100k ring, off-axis, at the boundary")


if __name__ == "__main__":
    unittest.main()
