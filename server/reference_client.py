"""
Reference client gate (PROTOCOL.md section 5).

This is the canonical Python implementation of the on-road gate the Java (proxy plugin)
and JS/Java (Meteor/Rusherhack) clients each mirror. Its defining property: it returns a
report dict that contains ONLY (road, seg, along, cond, ...) — never x/z or the perpendicular
offset. The offset is computed for the tolerance check and discarded inside this function.
"""

import time

from geometry import (
    ROAD_Y, DEFAULT_BUCKET, DEFAULT_TOLERANCE, nearest_allowed, along_bucket,
)


def build_report(x, y, z, dim, network, map_hash_str, server, *,
                 bucket=DEFAULT_BUCKET, tolerance=DEFAULT_TOLERANCE,
                 max_report_radius=None, cond="CLEAR", report_clear=True, now=None):
    """Return a wire report for a position, or None if it is not reportable.

    Gates, in order: nether dimension, per-contributor radius cap, on-road within
    tolerance, strict y==120. The perpendicular offset never leaves this function.
    """
    if dim != "NETHER":
        return None
    if max_report_radius is not None and max(abs(x), abs(z)) > max_report_radius:
        return None
    snap = nearest_allowed(x, z, network, tolerance)
    if snap is None:
        return None
    road_idx, seg_idx, cx, cz, _offset = snap   # _offset (perp distance) is DISCARDED here
    if round(y) != ROAD_Y:
        return None
    if cond == "CLEAR" and not report_clear:
        return None
    seg = network["roads"][road_idx]["segments"][seg_idx]
    along = along_bucket(seg, cx, cz, bucket)
    ts = int((now if now is not None else time.time()) // 30 * 30)
    return {"v": 1, "server": server, "map": map_hash_str,
            "road": road_idx, "seg": seg_idx, "along": along, "cond": cond, "ts": ts}
