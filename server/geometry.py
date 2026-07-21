"""
Highway geometry — the narrow-waist coordinate math.

This module is the single place that ever converts between real (x, z) and the
1-D on-highway coordinate (road, seg, along). The wire protocol carries ONLY the
latter, so a base coordinate is unrepresentable. The two directions:

  * client side (mock/fuzz + the Java/JS ports mirror this):
        nearest_allowed(x, z) -> snap onto an allowed road, then quantize to
        (road_idx, seg_idx, along). The perpendicular offset (distance off the
        road) is used ONLY for the tolerance gate and then discarded.

  * server side:
        validate_spatial(...) re-derives a representative (x, z) from
        (road_idx, seg_idx, along) using the server's own authoritative geometry.
        It never trusts a client-supplied (x, z) — the schema has no such field.

Geometry source: the bundled 2b2t nether-highway map (geometry/<server>.nether_highways.json),
the same data behind AquariusProxy's HighwayNetwork and ABM's highway grid map.
"""

import hashlib
import json
import math
from pathlib import Path

# ---- protocol constants (mirror PROTOCOL.md; the Java/JS clients use the same) -------------
ROAD_Y = 120                 # highways are ALWAYS y120; the client gate is strict on this
DEFAULT_BUCKET = 100         # quantization of `along` (blocks per segment bucket)
DEFAULT_TOLERANCE = 3.0      # max off-road XZ distance to count as "on the road" (client gate)
NEAR_SPAWN_RADIUS = 100_000  # max(|x|,|z|) <= this -> full grid allowed; beyond -> axis only


def load_network(path):
    """Load a <server>.nether_highways.json into a plain dict of roads."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    roads = []
    for r in data.get("roads", []):
        roads.append({
            "name": r.get("name", ""),
            "category": r.get("category", ""),
            "surface": r.get("surface", ""),
            "dim": r.get("dim"),
            "radius": r.get("radius"),
            "yLevel": r.get("yLevel"),
            "segments": [tuple(int(v) for v in s) for s in r.get("segments", [])],
        })
    return {
        "netherWorldBorder": data.get("netherWorldBorder", 30_000_000),
        "defaultYLevel": data.get("defaultYLevel", ROAD_Y),
        "roads": roads,
    }


def map_hash(network):
    """Stable version tag for a road table. Sensitive to geometry, not to name/surface."""
    canon = [
        {"category": r["category"], "radius": r["radius"],
         "segments": [list(s) for s in r["segments"]]}
        for r in network["roads"]
    ]
    blob = json.dumps(canon, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()[:16]


def _closest_point(px, pz, seg):
    """Closest point on a segment to (px,pz), clamped to endpoints. Returns (cx, cz)."""
    x1, z1, x2, z2 = seg
    dx = x2 - x1
    dz = z2 - z1
    len2 = dx * dx + dz * dz
    if len2 == 0:
        return float(x1), float(z1)
    t = ((px - x1) * dx + (pz - z1) * dz) / len2
    t = max(0.0, min(1.0, t))
    return x1 + t * dx, z1 + t * dz


def _seg_len(seg):
    return math.hypot(seg[2] - seg[0], seg[3] - seg[1])


def region_allows(category, x, z):
    """Spatially-tiered road-set policy: full grid within 100k of spawn, axis-only beyond.

    Evaluated on the ON-ROAD point so it is identical client-side and server-side
    (the server only ever has the re-derived on-road point).
    """
    if max(abs(x), abs(z)) <= NEAR_SPAWN_RADIUS:
        return True
    return category == "axis"


def nearest_allowed(x, z, network, tolerance=DEFAULT_TOLERANCE):
    """Snap (x,z) onto the nearest ALLOWED, usable road within tolerance.

    Returns (road_idx, seg_idx, cx, cz, distance) or None if off-road / disallowed.
    This is the client-side gate; callers additionally require nether + y==120.
    """
    best = None
    for ri, road in enumerate(network["roads"]):
        if road["surface"] == "planned":
            continue
        for si, seg in enumerate(road["segments"]):
            cx, cz = _closest_point(x, z, seg)
            if not region_allows(road["category"], cx, cz):
                continue
            d = math.hypot(x - cx, z - cz)
            if best is None or d < best[4]:
                best = (ri, si, cx, cz, d)
    if best is None or best[4] > tolerance:
        return None
    return best


def along_bucket(seg, cx, cz, bucket=DEFAULT_BUCKET):
    """Quantized distance from the segment start to the on-road point."""
    d = math.hypot(cx - seg[0], cz - seg[1])
    return int(d // bucket)


def max_along(seg, bucket=DEFAULT_BUCKET):
    seglen = _seg_len(seg)
    return int(math.ceil(seglen / bucket)) if seglen > 0 else 1


def rederive(network, road_idx, seg_idx, along, bucket=DEFAULT_BUCKET):
    """Server-side ONLY: representative (x,z) for a bucket. Always ON the road."""
    seg = network["roads"][road_idx]["segments"][seg_idx]
    x1, z1, x2, z2 = seg
    seglen = _seg_len(seg)
    if seglen == 0:
        return float(x1), float(z1)
    d = min(along * bucket + bucket / 2.0, seglen)
    t = d / seglen
    return x1 + t * (x2 - x1), z1 + t * (z2 - z1)


def road_width(road, default=6):
    """Parse a road's `dim` metadata (e.g. "6x4" -> 6). Falls back to `default` when the
    map didn't record one (e.g. the grid roads) or it doesn't parse. Used only to bound
    obstruction lane offsets against the road's real width (defense-in-depth layer 2) --
    the client-side scan (Java) uses the identical parse so both sides agree.
    """
    dim = road.get("dim")
    if not dim or "x" not in dim:
        return default
    try:
        return int(dim.split("x")[0])
    except ValueError:
        return default


def canonical_road_key(road):
    """Geometry-derived stable key so aggregates survive a road-table reorder/rehash."""
    segs = tuple(tuple(s) for s in road["segments"])
    h = hashlib.sha256(repr((road["category"], road["radius"], segs)).encode()).hexdigest()[:12]
    return f'{road["category"]}:{h}'


def validate_spatial(network, road_idx, seg_idx, along, bucket=DEFAULT_BUCKET):
    """Range-check a report's spatial triple against authoritative geometry.

    Returns (cx, cz, road) on success; raises ValueError otherwise. This is the
    server's geometric re-derivation layer (defense-in-depth layer 2).
    """
    roads = network["roads"]
    if not isinstance(road_idx, int) or not (0 <= road_idx < len(roads)):
        raise ValueError("road index out of range")
    road = roads[road_idx]
    if road["surface"] == "planned":
        raise ValueError("planned road is not reportable")
    segs = road["segments"]
    if not isinstance(seg_idx, int) or not (0 <= seg_idx < len(segs)):
        raise ValueError("seg index out of range")
    seg = segs[seg_idx]
    if not isinstance(along, int) or not (0 <= along < max_along(seg, bucket)):
        raise ValueError("along index out of range")
    cx, cz = rederive(network, road_idx, seg_idx, along, bucket)
    if not region_allows(road["category"], cx, cz):
        raise ValueError("region policy: non-axis road beyond near-spawn")
    return cx, cz, road
