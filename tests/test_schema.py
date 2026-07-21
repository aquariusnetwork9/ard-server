"""Strict schema validation — the narrow-waist guard rejects smuggled coordinates."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import validate_report, validate_moderation  # noqa: E402

GOOD = {"v": 1, "server": "2b2t.org", "map": "sha256:0123456789abcdef",
        "road": 0, "seg": 0, "along": 5, "cond": "HOLE"}


class ReportSchema(unittest.TestCase):
    def test_valid(self):
        validate_report(dict(GOOD))
        validate_report({**GOOD, "sev": 2, "ts": 1720624800})

    def test_reject_smuggled_x_z(self):
        for extra in ("x", "z", "y", "offset", "lat", "lon"):
            with self.assertRaises(ValueError, msg=f"must reject extra field {extra}"):
                validate_report({**GOOD, extra: 12345})

    def test_reject_missing_required(self):
        for f in ("v", "server", "map", "road", "seg", "along", "cond"):
            bad = dict(GOOD)
            bad.pop(f)
            with self.assertRaises(ValueError):
                validate_report(bad)

    def test_reject_bool_as_int(self):
        # bool is a subclass of int in Python; a JSON `true` must not pass as a coordinate.
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "road": True})

    def test_reject_negative_and_bad_enum(self):
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "along": -1})
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "cond": "NUKE"})

    def test_reject_bad_map_and_version(self):
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "map": "deadbeef"})
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "v": 2})

    def test_reject_bad_sev(self):
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "sev": 9})


class ObstructionSchema(unittest.TestCase):
    """laneMin/laneMax: required together, and ONLY for OBSTRUCTION_PARTIAL."""

    def test_full_obstruction_has_no_lane_fields(self):
        validate_report({**GOOD, "cond": "OBSTRUCTION_FULL"})
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "cond": "OBSTRUCTION_FULL", "laneMin": -1, "laneMax": 1})

    def test_partial_obstruction_requires_both_lane_fields(self):
        validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL", "laneMin": -1, "laneMax": 2})
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL", "laneMin": -1})
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL", "laneMax": 1})
        with self.assertRaises(ValueError):
            validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL"})

    def test_other_conds_reject_lane_fields(self):
        for cond in ("CLEAR", "HOLE", "LAVA", "COBWEB"):
            with self.assertRaises(ValueError, msg=f"{cond} must not carry a lane span"):
                validate_report({**GOOD, "cond": cond, "laneMin": -1, "laneMax": 1})

    def test_lane_ordering_and_bounds(self):
        with self.assertRaises(ValueError):  # min > max
            validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL", "laneMin": 2, "laneMax": -2})
        with self.assertRaises(ValueError):  # out of the structural bound
            validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL", "laneMin": -1, "laneMax": 999})
        with self.assertRaises(ValueError):  # bool is not an int
            validate_report({**GOOD, "cond": "OBSTRUCTION_PARTIAL", "laneMin": True, "laneMax": 1})


class ModerationSchema(unittest.TestCase):
    def test_valid(self):
        validate_moderation({**GOOD, "observedY": 113, "note": "7.5k ring is y113"})

    def test_requires_observed_y(self):
        with self.assertRaises(ValueError):
            validate_moderation(dict(GOOD))

    def test_rejects_extra(self):
        with self.assertRaises(ValueError):
            validate_moderation({**GOOD, "observedY": 113, "x": 1})


if __name__ == "__main__":
    unittest.main()
