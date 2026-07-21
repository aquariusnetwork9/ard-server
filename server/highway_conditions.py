#!/usr/bin/env python3
"""
Highway Conditions Network — ingest / aggregation service.

Pure Python stdlib (no pip), in the style of Aquarius Bot Manager's manager.py.

Security posture (see PROTOCOL.md):
  * The wire format has NO (x,z) field. Reports carry only (road, seg, along).
  * Strict schema validation rejects unknown fields — a smuggled "x"/"z" is refused.
  * (x,z) is re-derived server-side from the authoritative geometry, never trusted
    from the client.
  * Reads are PUBLIC (PROTOCOL.md SS7, locked in 2026-07-19): /geometry, /conditions
    and its /stream carry no auth check at all, only per-IP rate limiting -- this is
    the "Google-Maps-style consumption" goal, not an oversight. Writes stay tiered
    (Tier A/M/B/C) exactly as before; only the read side opened up.

Run:  python highway_conditions.py --geometry ../geometry --port 8788 \
          --owner-token OWNERSECRET --seed-token FLEETBOTSECRET:full:2b2t.org:fleet-bot-1
"""

import argparse
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import geometry
import identity
import sessions as sessions_mod
import trust
from geometry import (
    DEFAULT_BUCKET, load_network, map_hash, canonical_road_key, rederive,
    road_width, validate_spatial,
)

VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()

# --------------------------------------------------------------------------- strict validation

_COND_ENUM = {"CLEAR", "HOLE", "LAVA", "OBSTRUCTION_FULL", "OBSTRUCTION_PARTIAL",
              "COBWEB", "WATER", "GRAVEL", "UNBUILT", "PRESENCE"}
# Conds a CLEAR report can resolve. PRESENCE (camper reports) is its own opt-in,
# count-only mechanism (PROTOCOL.md SS6.1) -- a CLEAR never touches it.
_HAZARD_CONDS = _COND_ENUM - {"CLEAR", "PRESENCE"}
_PARTIAL_OBSTRUCTION = "OBSTRUCTION_PARTIAL"
_LANE_FIELDS = {"laneMin", "laneMax"}
_REPORT_ALLOWED = {"v", "server", "map", "road", "seg", "along", "cond", "sev", "ts"} | _LANE_FIELDS
_REPORT_REQUIRED = {"v", "server", "map", "road", "seg", "along", "cond"}
_MOD_ALLOWED = {"v", "server", "map", "road", "seg", "along", "cond", "observedY", "note"}
_MOD_REQUIRED = {"v", "server", "map", "road", "seg", "along", "cond", "observedY"}
_MAP_RE = re.compile(r"^sha256:[0-9a-f]{16}$")
_LANE_BOUND = 32


def _is_int(v):
    # bool is a subclass of int in Python; a JSON `true` must NOT pass as an integer.
    return isinstance(v, int) and not isinstance(v, bool)


def _check_nonneg_int(obj, field):
    v = obj[field]
    if not _is_int(v) or v < 0:
        raise ValueError(f"bad {field}")


def validate_report(obj):
    """Strict structural validation. Rejects unknown fields (the narrow-waist guard)."""
    if not isinstance(obj, dict):
        raise ValueError("report must be an object")
    keys = set(obj.keys())
    extra = keys - _REPORT_ALLOWED
    if extra:
        raise ValueError(f"unknown field(s): {sorted(extra)}")
    missing = _REPORT_REQUIRED - keys
    if missing:
        raise ValueError(f"missing field(s): {sorted(missing)}")
    if obj["v"] != 1:
        raise ValueError("bad version")
    if not isinstance(obj["server"], str) or not (1 <= len(obj["server"]) <= 64):
        raise ValueError("bad server")
    if not isinstance(obj["map"], str) or not _MAP_RE.match(obj["map"]):
        raise ValueError("bad map")
    for f in ("road", "seg", "along"):
        _check_nonneg_int(obj, f)
    if obj["cond"] not in _COND_ENUM:
        raise ValueError("bad cond")
    if "sev" in obj:
        v = obj["sev"]
        if not _is_int(v) or not (0 <= v <= 3):
            raise ValueError("bad sev")
    if "ts" in obj:
        _check_nonneg_int(obj, "ts")
    _validate_lane_fields(obj)
    return obj


def _validate_lane_fields(obj):
    """laneMin/laneMax are required together, and ONLY when cond==OBSTRUCTION_PARTIAL —
    a lane span makes no sense for any other condition (OBSTRUCTION_FULL has no open lane
    to describe; every other cond isn't lane-shaped at all)."""
    present = _LANE_FIELDS & set(obj.keys())
    if obj["cond"] != _PARTIAL_OBSTRUCTION:
        if present:
            raise ValueError(f"laneMin/laneMax only allowed for {_PARTIAL_OBSTRUCTION}")
        return
    if present != _LANE_FIELDS:
        raise ValueError("OBSTRUCTION_PARTIAL requires both laneMin and laneMax")
    lo, hi = obj["laneMin"], obj["laneMax"]
    if not _is_int(lo) or not _is_int(hi):
        raise ValueError("laneMin/laneMax must be integers")
    if not (-_LANE_BOUND <= lo <= hi <= _LANE_BOUND):
        raise ValueError("laneMin/laneMax out of range or laneMin > laneMax")


def validate_moderation(obj):
    if not isinstance(obj, dict):
        raise ValueError("moderation must be an object")
    keys = set(obj.keys())
    extra = keys - _MOD_ALLOWED
    if extra:
        raise ValueError(f"unknown field(s): {sorted(extra)}")
    missing = _MOD_REQUIRED - keys
    if missing:
        raise ValueError(f"missing field(s): {sorted(missing)}")
    if obj["v"] != 1:
        raise ValueError("bad version")
    if not isinstance(obj["server"], str) or not (1 <= len(obj["server"]) <= 64):
        raise ValueError("bad server")
    if not isinstance(obj["map"], str) or not _MAP_RE.match(obj["map"]):
        raise ValueError("bad map")
    for f in ("road", "seg", "along"):
        _check_nonneg_int(obj, f)
    if obj["cond"] not in _COND_ENUM:
        raise ValueError("bad cond")
    y = obj["observedY"]
    if not _is_int(y) or not (-64 <= y <= 320):
        raise ValueError("bad observedY")
    if "note" in obj and (not isinstance(obj["note"], str) or len(obj["note"]) > 280):
        raise ValueError("bad note")
    return obj


# --------------------------------------------------------------------------- store

# Write-tier precedence for merging corroborating reports of the same condition key.
# Tier A (fully vouched) and Tier M (maintainer, clear-only — see App._report) both
# auto-publish; Tier B (Discord-verified) and Tier C (anonymous) need corroboration,
# B at a lower threshold than C (see PROTOCOL.md §6.1).
_TIER_RANK = {"C": 0, "B": 1, "M": 2, "A": 3}
_AUTO_PUBLISH_TIERS = {"A", "M"}
_CORROBORATED_TIERS = {"B", "C"}

# --- Reputation layer (PROTOCOL.md §6.1.1) ------------------------------------------
# Per-identity trust score + travel-plausibility check, layered onto the corroboration
# counting above. Uses a second hash space from _source_hash below -- scoped per-identity
# (server, source_key) rather than per-condition-key -- recomputed live per request, never
# persisted raw, same handling as every other use of source_key in this file. Applies only
# to Tier B/C (_CORROBORATED_TIERS): Tier A/M bypass corroboration/weighting entirely and
# use the caller's IP as source_key, which isn't a stable per-identity signal for this
# project's own bot fleet (several bots legitimately share one VPS IP).
TRUST_BASELINE = 1.0    # starting trust for a first-seen identity
TRUST_MIN = 0.2         # floor
TRUST_MAX = 1.0         # ceiling
TRUST_PENALTY = 0.15    # per travel-implausible report (see _check_travel_plausible)
TRUST_BOOST = 0.05      # per report that helps a condition reach publish
MAX_TRAVEL_SPEED_DEFAULT = 100.0  # blocks/sec -- above real travel modes (vanilla
                        # sprint ~5.6, elytra e-bounce peaks ~40 measured)


class Store:
    """Geometry + SQLite aggregation. No HTTP; unit-testable in isolation."""

    def __init__(self, geometry_dir, db_path=":memory:", bucket=DEFAULT_BUCKET,
                 k_anon=2, k_tier_b=2, ttl=3600, clear_factor=2, reopen_window=3600,
                 salt=None, clock=time.time, max_travel_speed=MAX_TRAVEL_SPEED_DEFAULT):
        self.bucket = bucket
        self.k_anon = k_anon      # Tier C (anonymous, IP-hash) corroboration threshold
        self.k_tier_b = k_tier_b  # Tier B (Discord-verified identity) threshold -- lower than
        # k_anon (PROTOCOL.md K_TIER_B_NEW)
        self.ttl = ttl
        # PROTOCOL.md SS6.4: a CLEAR needs K_CLEAR_FACTOR x the normal threshold, and
        # a hazard reopening within reopen_window of a published clear gets flagged
        # to /moderation instead of silently re-publishing -- see ingest()/_active_clear.
        self.clear_factor = clear_factor
        self.reopen_window = reopen_window
        self.max_travel_speed = max_travel_speed  # see the reputation-layer note above _TIER_RANK
        self.clock = clock
        self.salt = salt or secrets.token_hex(16)
        self.networks = {}
        self.map_hashes = {}
        self._lock = threading.RLock()
        self._subscribers = []  # (server, queue.Queue)
        self._load_geometry(geometry_dir)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    # ---- geometry ----
    def _load_geometry(self, geometry_dir):
        p = Path(geometry_dir)
        suffix = ".nether_highways.json"
        files = [p] if p.is_file() else sorted(p.glob("*" + suffix))
        if not files:
            raise SystemExit(f"no geometry files (*{suffix}) in {geometry_dir}")
        for f in files:
            server = f.name[:-len(suffix)] if f.name.endswith(suffix) else f.stem
            net = load_network(f)
            net["_canon"] = [canonical_road_key(r) for r in net["roads"]]
            net["_canon2idx"] = {}
            for i, c in enumerate(net["_canon"]):
                net["_canon2idx"].setdefault(c, i)
            self.networks[server] = net
            self.map_hashes[server] = map_hash(net)

    def geometry_view(self, server):
        net = self.networks[server]
        return {
            "server": server,
            "map": self.map_hashes[server],
            "bucket": self.bucket,
            "roadY": geometry.ROAD_Y,
            "nearSpawnRadius": geometry.NEAR_SPAWN_RADIUS,
            "tolerance": geometry.DEFAULT_TOLERANCE,
            "roads": [
                {"i": i, "name": r["name"], "category": r["category"], "dim": r["dim"],
                 "surface": r["surface"], "radius": r["radius"], "segments": [list(s) for s in r["segments"]]}
                for i, r in enumerate(net["roads"])
            ],
        }

    # ---- db ----
    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS conditions(
          id INTEGER PRIMARY KEY,
          server TEXT, road_canon TEXT, seg INTEGER, along INTEGER, cond TEXT,
          tier TEXT, reports INTEGER, first_seen REAL, last_seen REAL,
          lane_min INTEGER, lane_max INTEGER,
          UNIQUE(server, road_canon, seg, along, cond)
        );
        CREATE TABLE IF NOT EXISTS sources(
          cond_id INTEGER, src_hash TEXT, seen REAL,
          UNIQUE(cond_id, src_hash)
        );
        CREATE TABLE IF NOT EXISTS moderation(
          id INTEGER PRIMARY KEY, server TEXT, kind TEXT NOT NULL DEFAULT 'off_y120',
          payload TEXT, observed_y INTEGER,
          status TEXT, created REAL
        );
        CREATE TABLE IF NOT EXISTS identities(
          identity_hash TEXT PRIMARY KEY,
          trust REAL NOT NULL,
          last_road INTEGER, last_seg INTEGER, last_along INTEGER,
          last_x REAL, last_z REAL, last_seen REAL
        );
        """)
        # sources.weight didn't exist before the reputation layer -- ALTER, not
        # CREATE, since a live deployment's DB already has this table without it.
        self._ensure_column("sources", "weight", "REAL NOT NULL DEFAULT 1.0")
        self.db.commit()

    def _ensure_column(self, table, column, coldef):
        cols = [r[1] for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")

    def _source_hash(self, server, canon, seg, along, cond, source_key):
        # Privacy: rotating salt + per-condition-key scoping. Never stores the raw
        # key (an IP for Tier C, a discord_id for Tier B), and the same source cannot
        # be correlated across different condition keys.
        key = f"{self.salt}|{server}|{canon}|{seg}|{along}|{cond}|{source_key or ''}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _distinct_sources(self, cond_id, now):
        cur = self.db.execute(
            "SELECT COUNT(*) FROM sources WHERE cond_id=? AND seen>=?",
            (cond_id, now - self.ttl))
        return cur.fetchone()[0]

    # ---- CLEAR <-> hazard reconciliation (PROTOCOL.md SS6.4) ----
    def _hazard_rows(self, server, canon, seg, along, now):
        """Non-expired hazard-type rows (not CLEAR/PRESENCE) at this spatial key --
        the set a CLEAR at the same key is resolving."""
        placeholders = ",".join("?" * len(_HAZARD_CONDS))
        return self.db.execute(
            f"SELECT id, cond, tier, last_seen FROM conditions"
            f" WHERE server=? AND road_canon=? AND seg=? AND along=? AND cond IN ({placeholders})"
            f" AND last_seen>=?",
            (server, canon, seg, along, *sorted(_HAZARD_CONDS), now - self.ttl)).fetchall()

    def _reported_a_hazard_here(self, server, canon, seg, along, source_key, now):
        """Did this exact source_key (an IP for Tier C, a discord_id for Tier B)
        already report one of the active hazards at this spot? PROTOCOL.md SS6.4's
        non-overlap rule (a source can't both raise and resolve the same hazard) --
        checked the ONLY privacy-safe way available: the stored source hash is
        deliberately cond-scoped (SS6.1 -- "the same source cannot be correlated
        across different condition keys"), so a stored HOLE hash and a stored CLEAR
        hash for the same source never match each other by design, and never will.
        Instead, recompute what THIS request's source's hash would have been under
        each active hazard's cond and check it against that hazard's own stored
        sources -- using the raw source_key only for this one live request, exactly
        as an IP is already used for rate-limiting, never persisting a new
        cross-cond-linkable value anywhere."""
        for hz_id, hz_cond, _hz_tier, _hz_last_seen in self._hazard_rows(server, canon, seg, along, now):
            candidate = self._source_hash(server, canon, seg, along, hz_cond, source_key)
            if self.db.execute("SELECT 1 FROM sources WHERE cond_id=? AND src_hash=?",
                                (hz_id, candidate)).fetchone():
                return True
        return False

    def _corroboration_threshold(self, tier):
        """Base k for a corroborated tier -- Tier B (Discord-verified) is lower than
        Tier C (bare IP) (K_TIER_B_NEW vs K_TIER_C_NEW, PROTOCOL.md §2)."""
        return self.k_tier_b if tier == "B" else self.k_anon

    def _corroboration_weight(self, cond_id, now):
        """Sum of sources.weight for non-expired sources -- what the publish/confidence
        decision actually uses. See _distinct_sources for the separate raw COUNT kept
        for the public-facing 'distinctSources' field (deliberately not the same number
        once reputation weighting has discounted anyone -- distinctSources is meant to
        stay a plain, honest headcount)."""
        cur = self.db.execute(
            "SELECT COALESCE(SUM(weight),0) FROM sources WHERE cond_id=? AND seen>=?",
            (cond_id, now - self.ttl))
        return cur.fetchone()[0]

    # ---- reputation layer (see the module-level note above _TIER_RANK) ----
    def _identity_hash(self, server, source_key):
        # Deliberately a DIFFERENT hash space from _source_hash (no cond/seg/along in
        # the input, and a distinct literal tag) -- this hash must be the same across
        # different locations for the same identity (that's the whole point), so it
        # must never collide with or be derivable from any per-condition-key source
        # hash, and vice versa.
        key = f"{self.salt}|identity|{server}|{source_key or ''}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _get_trust(self, identity_hash):
        row = self.db.execute(
            "SELECT trust FROM identities WHERE identity_hash=?", (identity_hash,)).fetchone()
        return row[0] if row else TRUST_BASELINE

    def _adjust_trust(self, identity_hash, delta, now):
        trust = max(TRUST_MIN, min(TRUST_MAX, self._get_trust(identity_hash) + delta))
        self.db.execute(
            "INSERT INTO identities(identity_hash, trust, last_seen) VALUES(?,?,?)"
            " ON CONFLICT(identity_hash) DO UPDATE SET trust=excluded.trust",
            (identity_hash, trust, now))

    def _check_travel_plausible(self, identity_hash, road_idx, seg, along, cx, cz, now):
        """True if this identity's last known claimed position (if any) is reachable
        from here within the elapsed time at a physically real travel speed. Always
        updates the stored last-known position as a side effect, regardless of the
        verdict -- if it only updated on a plausible verdict, a burst of implausible
        reports would keep comparing every later, genuinely legitimate report against
        an increasingly stale reference point, cascading false positives instead of
        containing the one bad report."""
        row = self.db.execute(
            "SELECT last_x, last_z, last_seen FROM identities WHERE identity_hash=?",
            (identity_hash,)).fetchone()
        plausible = True
        if row is not None and row[0] is not None:
            last_x, last_z, last_seen = row
            elapsed = now - last_seen
            dist = ((cx - last_x) ** 2 + (cz - last_z) ** 2) ** 0.5
            if elapsed <= 0:
                plausible = dist == 0
            else:
                plausible = (dist / elapsed) <= self.max_travel_speed
        self.db.execute(
            "INSERT INTO identities(identity_hash, trust, last_road, last_seg, last_along,"
            " last_x, last_z, last_seen) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(identity_hash) DO UPDATE SET"
            " last_road=excluded.last_road, last_seg=excluded.last_seg,"
            " last_along=excluded.last_along, last_x=excluded.last_x,"
            " last_z=excluded.last_z, last_seen=excluded.last_seen",
            (identity_hash, TRUST_BASELINE, road_idx, seg, along, cx, cz, now))
        return plausible

    def _active_clear(self, server, canon, seg, along, now):
        """The last_seen of a currently-PUBLISHED CLEAR at this spatial key, or None.
        Used both to suppress resolved hazards at query time and to flag a hazard
        reopening shortly after a clear (see ingest())."""
        row = self.db.execute(
            "SELECT id, tier, last_seen FROM conditions"
            " WHERE server=? AND road_canon=? AND seg=? AND along=? AND cond='CLEAR' AND last_seen>=?",
            (server, canon, seg, along, now - self.ttl)).fetchone()
        if row is None:
            return None
        cond_id, tier, last_seen = row
        if tier in _AUTO_PUBLISH_TIERS:
            return last_seen
        weight = self._corroboration_weight(cond_id, now)
        return last_seen if weight >= self._corroboration_threshold(tier) * self.clear_factor else None

    # ---- ingest ----
    def ingest(self, report, source_key, tier):
        """report must already have passed validate_report(). tier is 'A', 'M', 'B',
        or 'C' (see App._report for how a presented token maps to a tier). source_key
        is whatever identifies the reporter for corroboration purposes -- the raw
        client IP for Tier C, a resolved discord_id for Tier B, irrelevant (but still
        passed through) for auto-publishing A/M.

        Returns a view dict."""
        server = report["server"]
        net = self.networks.get(server)
        if net is None:
            raise ValueError("unknown server")
        if report["map"] != self.map_hashes[server]:
            raise ValueError("stale or unknown map version")
        cx, cz, road = validate_spatial(net, report["road"], report["seg"], report["along"], self.bucket)
        canon = net["_canon"][report["road"]]
        seg, along, cond = report["seg"], report["along"], report["cond"]
        lane_min = lane_max = None
        if cond == _PARTIAL_OBSTRUCTION:
            lane_min, lane_max = report["laneMin"], report["laneMax"]
            half = max(1, road_width(road) // 2 + 1)
            if not (-half <= lane_min <= lane_max <= half):
                raise ValueError("lane span out of range for this road's width")
        now = self.clock()
        # A hazard reopening shortly after a published clear is flagged for moderator
        # review rather than silently republished -- a fast reopen is the practical
        # signal that the clear was wrong, whether by mistake or bad faith.
        reopened = False
        if cond in _HAZARD_CONDS:
            clear_ts = self._active_clear(server, canon, seg, along, now)
            if clear_ts is not None and (now - clear_ts) <= self.reopen_window:
                reopened = True
        # Non-overlap (SS6.4): a source that raised a hazard here can't also count
        # toward clearing it. See _reported_a_hazard_here for why this can't be
        # enforced by comparing stored hashes -- it has to be checked live, per
        # request, against this specific source_key.
        counts_toward_corroboration = not (
            cond == "CLEAR" and self._reported_a_hazard_here(server, canon, seg, along, source_key, now))
        src = self._source_hash(server, canon, seg, along, cond, source_key)
        # Reputation layer: only meaningful for tiers that actually go through
        # corroboration counting -- see the module-level note above _TIER_RANK for why
        # A/M are excluded (shared-IP fleet bots would otherwise false-positive on
        # travel-plausibility against each other).
        identity_hash = None
        weight = 1.0
        with self._lock:
            if tier in _CORROBORATED_TIERS:
                identity_hash = self._identity_hash(server, source_key)
                if not self._check_travel_plausible(identity_hash, report["road"], seg, along, cx, cz, now):
                    counts_toward_corroboration = False
                    self._adjust_trust(identity_hash, -TRUST_PENALTY, now)
                weight = self._get_trust(identity_hash)
            row = self.db.execute(
                "SELECT id, tier, lane_min, lane_max FROM conditions"
                " WHERE server=? AND road_canon=? AND seg=? AND along=? AND cond=?",
                (server, canon, seg, along, cond)).fetchone()
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO conditions(server,road_canon,seg,along,cond,tier,reports,"
                    "first_seen,last_seen,lane_min,lane_max) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
                    (server, canon, seg, along, cond, tier, now, now, lane_min, lane_max))
                cond_id = cur.lastrowid
            else:
                cond_id, cur_tier, prev_min, prev_max = row
                new_tier = max(cur_tier, tier, key=_TIER_RANK.get)
                # Union corroborating lane spans: widen, never narrow, as more reporters confirm.
                new_min = lane_min if prev_min is None else (lane_min if lane_min is None else min(prev_min, lane_min))
                new_max = lane_max if prev_max is None else (lane_max if lane_max is None else max(prev_max, lane_max))
                self.db.execute(
                    "UPDATE conditions SET reports=reports+1, last_seen=?, tier=?, lane_min=?, lane_max=?"
                    " WHERE id=?",
                    (now, new_tier, new_min, new_max, cond_id))
            if counts_toward_corroboration:
                self.db.execute(
                    "INSERT OR REPLACE INTO sources(cond_id, src_hash, seen, weight) VALUES(?,?,?,?)",
                    (cond_id, src, now, weight))
            self.db.commit()
            view = self._view_by_id(net, cond_id, now)
            # Positive reinforcement: this report helped corroborate a condition that IS
            # (now) published -- nudge this identity's trust back toward baseline. This
            # is a pragmatic approximation, not a full retroactive reward for every past
            # contributor to this condition (which would need persisting identity-to-
            # specific-report links, a materially bigger privacy cost than tracking one
            # "last known point" per identity) -- see the module-level note.
            if identity_hash is not None and counts_toward_corroboration and view["published"]:
                self._adjust_trust(identity_hash, TRUST_BOOST, now)
                self.db.commit()
        if reopened:
            self.add_moderation(report, kind="reopen")
        self._broadcast(server, view)
        return view

    def _view_by_id(self, net, cond_id, now):
        r = self.db.execute(
            "SELECT id,server,road_canon,seg,along,cond,tier,reports,first_seen,last_seen,lane_min,lane_max"
            " FROM conditions WHERE id=?", (cond_id,)).fetchone()
        return self._row_view(net, r, now)

    def _row_view(self, net, r, now):
        (cond_id, server, canon, seg, along, cond, tier, reports, first_seen, last_seen,
         lane_min, lane_max) = r
        # A CLEAR needs K_CLEAR_FACTOR x the normal threshold (PROTOCOL.md SS6.4). The
        # non-overlap half of SS6.4 (a source can't both raise and resolve the same
        # hazard) is enforced at ingest time instead -- see _reported_a_hazard_here --
        # by simply never recording that source against the CLEAR in the first place,
        # so it's already excluded from the weight sum by the time a view is built.
        base_k = self._corroboration_threshold(tier)
        k_req = base_k * self.clear_factor if cond == "CLEAR" else base_k
        # distinctSources stays a plain headcount for display; the publish/confidence
        # decision uses the reputation-weighted sum instead (equal to the headcount
        # unless a source has been discounted -- see the note above _TIER_RANK).
        ds = self._distinct_sources(cond_id, now)
        weight = self._corroboration_weight(cond_id, now)
        published = (tier in _AUTO_PUBLISH_TIERS) or (weight >= k_req)
        age = now - last_seen
        recency = max(0.0, 1.0 - age / self.ttl)
        strength = 1.0 if tier in _AUTO_PUBLISH_TIERS else min(1.0, weight / max(1, k_req))
        conf = round(strength * recency, 3)
        road_idx = net["_canon2idx"].get(canon)
        x = z = None
        if road_idx is not None and seg < len(net["roads"][road_idx]["segments"]):
            x, z = rederive(net, road_idx, seg, along, self.bucket)
        return {
            "server": server, "road": road_idx, "seg": seg, "along": along, "cond": cond,
            "x": None if x is None else round(x, 1), "z": None if z is None else round(z, 1),
            "laneMin": lane_min, "laneMax": lane_max,
            "tier": tier, "reports": reports, "distinctSources": ds, "confidence": conf,
            "published": published, "firstSeen": int(first_seen), "lastSeen": int(last_seen),
        }

    def query(self, server, road_idx=None, frm=None, to=None, include_unpublished=False):
        net = self.networks.get(server)
        if net is None:
            raise KeyError(server)
        now = self.clock()
        canon_filter = None
        if road_idx is not None:
            if not (0 <= road_idx < len(net["roads"])):
                raise ValueError("road out of range")
            canon_filter = net["_canon"][road_idx]
        with self._lock:
            q = ("SELECT id,server,road_canon,seg,along,cond,tier,reports,first_seen,last_seen,"
                 "lane_min,lane_max FROM conditions WHERE server=? AND last_seen>=?")
            args = [server, now - self.ttl]
            if canon_filter is not None:
                q += " AND road_canon=?"
                args.append(canon_filter)
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            canon = r[2]
            v = self._row_view(net, r, now)
            if frm is not None and v["along"] < frm:
                continue
            if to is not None and v["along"] > to:
                continue
            if not include_unpublished and not v["published"]:
                continue
            if v["cond"] in _HAZARD_CONDS:
                clear_ts = self._active_clear(server, canon, v["seg"], v["along"], now)
                if clear_ts is not None and clear_ts > v["lastSeen"]:
                    continue  # resolved: a newer published CLEAR supersedes this hazard
            out.append(v)
        out.sort(key=lambda v: (v["road"] if v["road"] is not None else -1, v["seg"], v["along"]))
        return out

    # ---- moderation ----
    def add_moderation(self, payload, kind="off_y120"):
        """kind: 'off_y120' for a client-submitted anomaly (has observedY), or
        'reopen' for a server-generated reopen-accountability flag (SS6.4) --
        those don't carry observedY, it's just absent from the payload."""
        now = self.clock()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO moderation(server,kind,payload,observed_y,status,created) VALUES(?,?,?,?,?,?)",
                (payload["server"], kind, json.dumps(payload, separators=(",", ":")),
                 payload.get("observedY"), "pending", now))
            self.db.commit()
            return cur.lastrowid

    def list_moderation(self, status="pending", server=None):
        with self._lock:
            if server is None:
                rows = self.db.execute(
                    "SELECT id,server,kind,payload,observed_y,status,created FROM moderation"
                    " WHERE status=? ORDER BY id", (status,)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT id,server,kind,payload,observed_y,status,created FROM moderation"
                    " WHERE status=? AND server=? ORDER BY id", (status, server)).fetchall()
        return [{"id": r[0], "server": r[1], "kind": r[2], "payload": json.loads(r[3]),
                 "observedY": r[4], "status": r[5], "created": int(r[6])} for r in rows]

    def moderation_server(self, mid):
        """The `server` a pending moderation entry belongs to, or None if unknown
        -- used to authorize a resolve action against a moderator's OWN server
        scope before the action is allowed to touch it."""
        with self._lock:
            row = self.db.execute("SELECT server FROM moderation WHERE id=?", (mid,)).fetchone()
        return row[0] if row else None

    def resolve_moderation(self, mid, action):
        with self._lock:
            cur = self.db.execute("UPDATE moderation SET status=? WHERE id=? AND status='pending'",
                                  (action, mid))
            self.db.commit()
            return cur.rowcount > 0

    def quash(self, server, road_idx, seg, along, cond):
        """Moderator override: removes one specific condition (and its corroborating
        sources) outright, regardless of its current published/confidence state.
        Returns True if a row existed and was removed. Logs an 'approved' moderation
        record (kind='quash') for the same audit trail 'reopen' already gets."""
        net = self.networks.get(server)
        if net is None:
            raise KeyError(server)
        if not (0 <= road_idx < len(net["roads"])):
            raise ValueError("road out of range")
        canon = net["_canon"][road_idx]
        now = self.clock()
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM conditions WHERE server=? AND road_canon=? AND seg=? AND along=? AND cond=?",
                (server, canon, seg, along, cond)).fetchone()
            if row is None:
                return False
            cond_id = row[0]
            self.db.execute("DELETE FROM sources WHERE cond_id=?", (cond_id,))
            self.db.execute("DELETE FROM conditions WHERE id=?", (cond_id,))
            self.db.execute(
                "INSERT INTO moderation(server,kind,payload,observed_y,status,created) VALUES(?,?,?,?,?,?)",
                (server, "quash",
                 json.dumps({"road": road_idx, "seg": seg, "along": along, "cond": cond},
                            separators=(",", ":")),
                 None, "approved", now))
            self.db.commit()
        return True

    def retract_identity(self, server, source_key, now=None):
        """Removes every currently-active `sources` row this source_key would
        correspond to on `server`, across every non-expired condition -- used when
        an identity is suspended, so its past corroborations stop counting
        immediately rather than lingering until TTL. The per-condition source hash
        can't be looked up directly (same reason _reported_a_hazard_here recomputes
        live instead of storing a cross-linkable value), so this recomputes the
        candidate hash for each active condition and deletes matching rows -- the
        raw source_key is used only for this one live call, never persisted.
        Returns the number of sources rows removed."""
        net = self.networks.get(server)
        if net is None:
            return 0
        now = self.clock() if now is None else now
        removed = 0
        with self._lock:
            rows = self.db.execute(
                "SELECT id, road_canon, seg, along, cond FROM conditions WHERE server=? AND last_seen>=?",
                (server, now - self.ttl)).fetchall()
            for cond_id, canon, seg, along, cond in rows:
                candidate = self._source_hash(server, canon, seg, along, cond, source_key)
                cur = self.db.execute(
                    "DELETE FROM sources WHERE cond_id=? AND src_hash=?", (cond_id, candidate))
                removed += cur.rowcount
            self.db.commit()
        return removed

    # ---- SSE ----
    def subscribe(self, server):
        q = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append((server, q))
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers = [(s, qq) for (s, qq) in self._subscribers if qq is not q]

    def _broadcast(self, server, view):
        with self._lock:
            subs = [qq for (s, qq) in self._subscribers if s == server]
        for qq in subs:
            try:
                qq.put_nowait(view)
            except queue.Full:
                pass


# --------------------------------------------------------------------------- client IP resolution

# A reverse proxy in front of this service (e.g. a Cloudflare tunnel) terminates the
# real client connection itself, so the socket peer this process sees is just the
# proxy's own local hop -- not useful as a rate-limit or corroboration key on its own.
# DEFAULT_TRUSTED_PROXIES is the baseline set of peers allowed to supply the real
# client address via header instead (loopback, since a local reverse proxy is the
# expected topology); --trusted-proxy/ARD_TRUSTED_PROXIES extends it for other
# topologies. A peer NOT in this set never gets its header taken at face value.
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")


def _parse_networks(specs):
    return [ipaddress.ip_network(s, strict=False) for s in specs]


def _normalize_ip(raw):
    """Collapses an IPv6 address to its /64 network address (the smallest block a
    single allocation is typically assigned) so per-address bucketing stays
    meaningful for v6 callers; IPv4 and unparseable values pass through unchanged."""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{addr}/64", strict=False).network_address)
    return str(addr)


def _forwarded_ip(headers):
    """The address a trusted proxy says the request actually came from, or None.
    CF-Connecting-IP (set by Cloudflare's own edge) takes priority; X-Forwarded-For's
    last entry is the fallback -- the entry the nearest hop itself appended."""
    cf = headers.get("CF-Connecting-IP", "").strip()
    if cf:
        return cf
    parts = [p.strip() for p in headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    return parts[-1] if parts else None


# --------------------------------------------------------------------------- auth / rate limit

class Auth:
    """Write privilege comes from two places: the Owner-issued registry (trust.py,
    Tier A/M/moderator -- SS6.3) and the self-service link store (identity.py, Tier
    B -- SS6.2). Both are scoped per-server -- a token/identity trusted on one
    anarchy server carries no authority on another sharing this deployment. The
    Owner secret is still a flat, deployment-wide hash set: Owner is deliberately
    out-of-band / not itself a registry entry (avoids the bootstrapping problem of
    the first grant needing something to grant it), and stays global on purpose --
    one operator running the whole deployment, not one per server. Reads carry no
    auth check at all -- SS7 locks that in as a deliberate decision (public,
    Google-Maps-style consumption), not an oversight; see RateLimiter for the
    abuse control that replaces it on the read side."""

    def __init__(self, registry, links=None, owner_hashes=None, sessions=None, bot_hashes=None):
        self.registry = registry
        self.links = links
        self.owner = set(owner_hashes or [])
        # Admin-dashboard sessions (sessions.py) -- a human logged in with
        # Discord, resolved to a trust.py discord_grants scope. Separate from
        # the bearer-token registry above; either credential works wherever a
        # write route accepts one (see Handler._caller_scopes).
        self.sessions = sessions
        # First-party bot credential (e.g. the Discord "Highway Bot") -- same
        # flat, deployment-wide hash-set shape as owner, but a narrower trust
        # class: it only unlocks /link/bot-complete, never registry/admin
        # actions. Exists because a bot resolving a slash command already has
        # a Discord-verified user id from Discord's own gateway/interaction
        # signature -- this proves the REQUEST came from the legitimate bot
        # service, standing in for what discord_exchange's OAuth round-trip
        # proves for the website flow.
        self.bot = set(bot_hashes or [])

    @staticmethod
    def hash_token(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def _match(self, token, pool):
        if not token:
            return False
        h = self.hash_token(token)
        return any(hmac.compare_digest(h, p) for p in pool)

    def write_scope(self, token, server):
        """The presented token's live registry scope ('full'/'maintainer'/'moderator')
        ON THIS SERVER, or None if it's absent, revoked, scoped to a different
        server, or unrecognized. Doesn't cover Tier B -- see tier_b_identity, a
        separate lookup since Tier B isn't a "scope" on this registry at all."""
        return self.registry.scope_of(token, server)

    def tier_b_identity(self, token, server):
        """The discord_id behind a live, linked Tier B token ON THIS SERVER, or None."""
        return self.links.discord_identity_for(token, server) if self.links else None

    def is_moderator(self, token, server):
        return self.registry.has_scope(token, trust.SCOPE_MODERATOR, server)

    def is_owner(self, token):
        return self._match(token, self.owner)

    def is_bot(self, token):
        return self._match(token, self.bot)


class RateLimiter:
    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, limit, window, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if t > now - window]
            if len(bucket) >= limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True


class App:
    def __init__(self, store, auth, limiter=None,
                 anon_write_limit=60, anon_write_window=60,
                 read_limit=120, read_window=60, discord_verify=None,
                 discord_client_id=None, discord_redirect_uri=None,
                 trusted_proxies=None):
        self.store = store
        self.auth = auth
        self.limiter = limiter or RateLimiter()
        self.anon_write_limit = anon_write_limit
        self.anon_write_window = anon_write_window
        self.read_limit = read_limit
        self.read_window = read_window
        # discord_code -> discord_id. None until configured (no client id/secret
        # supplied) -- /link/complete reports that clearly rather than crashing.
        # Injectable so tests never make a real network call to discord.com.
        self.discord_verify = discord_verify
        # client_id/redirect_uri are NOT secrets (they're meant to be public in an
        # OAuth authorize URL) -- kept here so /link/config can hand them to the
        # website without hardcoding deployment-specific values into committed JS.
        self.discord_client_id = discord_client_id
        self.discord_redirect_uri = discord_redirect_uri
        # None -> default (loopback only); explicit (including []) is used as-is,
        # so a caller can widen or fully disable header-trust deliberately.
        self.trusted_proxies = _parse_networks(
            DEFAULT_TRUSTED_PROXIES if trusted_proxies is None else trusted_proxies)

    def trusts_peer(self, peer):
        """Whether `peer` (the raw socket address a request arrived from) is allowed
        to have its CF-Connecting-IP/X-Forwarded-For header taken at face value."""
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self.trusted_proxies)


# --------------------------------------------------------------------------- HTTP

_CONDITIONS_RE = re.compile(r"^/conditions/([^/]+)(/stream)?$")
_GEOMETRY_RE = re.compile(r"^/geometry/([^/]+)$")
_MOD_LIST_RE = re.compile(r"^/moderation/([^/]+)$")
_MOD_RESOLVE_RE = re.compile(r"^/moderation/(\d+)/(approve|reject)$")
_REGISTRY_ID_RE = re.compile(r"^/registry/([0-9a-f]{16})$")
_IDENTITY_ACTION_RE = re.compile(r"^/identity/([^/]+)/([^/]+)/(suspend|reinstate)$")
_ADMIN_GRANT_ID_RE = re.compile(r"^/admin/grants/([0-9a-f]{16})$")
_SESSION_COOKIE_NAME = "ard_session"

# Phase 3: the public map website. Static files only (no templating/pip) -- the
# page fetches /geometry and /conditions itself, same as any other consumer.
_WEBSITE_ROOT = (Path(__file__).resolve().parent.parent / "website").resolve()
_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HighwayConditions/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---- helpers ----
    @property
    def app(self):
        return self.server.app

    def _client_ip(self):
        peer = self.client_address[0] if self.client_address else "?"
        if self.app.trusts_peer(peer):
            forwarded = _forwarded_ip(self.headers)
            if forwarded:
                return _normalize_ip(forwarded)
        return _normalize_ip(peer)

    def _token(self):
        return self.headers.get("Authorization", "").strip() or None

    def _session_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith(_SESSION_COOKIE_NAME + "="):
                return part[len(_SESSION_COOKIE_NAME) + 1:] or None
        return None

    def _session_discord_id(self):
        sessions = self.app.auth.sessions
        return sessions.discord_id_for(self._session_token()) if sessions else None

    def _set_session_cookie(self, token, max_age):
        # HttpOnly (never readable from JS -- an XSS on the dashboard can't
        # exfiltrate the session), Secure (only ever sent over the HTTPS the
        # production deployment is exclusively reached through), SameSite=Strict
        # (the browser won't attach this cookie to any cross-site request at
        # all, which is what actually matters here -- every admin route is a
        # JSON POST/DELETE a top-level cross-site navigation can't forge, so
        # Strict alone closes the CSRF gap without needing a separate token).
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE_NAME}={token}; Max-Age={max_age}; "
            f"HttpOnly; Secure; SameSite=Strict; Path=/")

    def _clear_session_cookie(self):
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE_NAME}=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit=1 << 20):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return b""
        if n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def log_message(self, fmt, *args):
        # Never log query strings / bodies. Method + path only.
        pass

    # ---- routing ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True, "version": VERSION,
                                    "servers": sorted(self.app.store.networks.keys())})
        m = _GEOMETRY_RE.match(path)
        if m:
            return self._geometry(m.group(1))
        m = _CONDITIONS_RE.match(path)
        if m:
            server, stream = m.group(1), m.group(2)
            return self._conditions_stream(server) if stream else self._conditions(server)
        m = _MOD_LIST_RE.match(path)
        if m:
            return self._moderation_list(m.group(1))
        if path == "/registry":
            return self._registry_list()
        if path == "/link/config":
            return self._link_config()
        if path == "/admin/session":
            return self._admin_session()
        if path == "/admin/grants":
            return self._admin_grants_list()
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/report":
            return self._report()
        if path == "/moderation":
            return self._moderation_submit()
        if path == "/moderation/quash":
            return self._moderation_quash()
        m = _MOD_RESOLVE_RE.match(path)
        if m:
            return self._moderation_resolve(int(m.group(1)), m.group(2))
        if path == "/registry":
            return self._registry_issue()
        if path == "/link/init":
            return self._link_init()
        if path == "/link/complete":
            return self._link_complete()
        if path == "/link/bot-complete":
            return self._link_bot_complete()
        m = _IDENTITY_ACTION_RE.match(path)
        if m:
            return self._identity_action(m.group(1), m.group(2), m.group(3))
        if path == "/admin/login":
            return self._admin_login()
        if path == "/admin/logout":
            return self._admin_logout()
        if path == "/admin/grants":
            return self._admin_grants_issue()
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = _REGISTRY_ID_RE.match(path)
        if m:
            return self._registry_revoke(m.group(1))
        m = _ADMIN_GRANT_ID_RE.match(path)
        if m:
            return self._admin_grants_revoke(m.group(1))
        return self._json(404, {"error": "not found"})

    # ---- read gate ----
    def _rate_limit_read(self):
        # Reads are public (SS7) -- no auth check, just per-IP throttling so one
        # client can't hammer the map data endpoints.
        if not self.app.limiter.allow("read:" + self._client_ip(),
                                      self.app.read_limit, self.app.read_window):
            self._json(429, {"error": "rate limited"})
            return False
        return True

    def _rate_limit_modsubmit(self):
        # /moderation submission used to inherit its gate from _require_read (a
        # token was needed to reach it at all). Now that reads carry no auth, this
        # needs its own explicit throttle -- same posture as anonymous /report
        # writes -- so a public moderation queue can't be spammed for free.
        if not self.app.limiter.allow("modsubmit:" + self._client_ip(),
                                      self.app.anon_write_limit, self.app.anon_write_window):
            self._json(429, {"error": "rate limited"})
            return False
        return True

    def _caller_scopes(self, server):
        """Every dashboard-relevant scope ('moderator'/'admin') this request
        holds for `server`, merging the bearer-token registry (bots/scripts,
        trust.py Registry) and an admin-dashboard session (a human logged in
        with Discord, trust.py discord_grants) -- either credential works
        identically wherever a route below checks scope. The Owner secret is
        checked separately (is_owner) since it's global, not a per-server grant."""
        scopes = set()
        token = self._token()
        if self.app.auth.is_moderator(token, server):
            scopes.add(trust.SCOPE_MODERATOR)
        discord_id = self._session_discord_id()
        if discord_id:
            scopes |= self.app.auth.registry.discord_scopes(discord_id, server)
        return scopes

    def _require_moderator_for(self, server):
        if self.app.auth.is_owner(self._token()):
            return True
        scopes = self._caller_scopes(server)
        if not (trust.SCOPE_MODERATOR in scopes or trust.SCOPE_ADMIN in scopes):
            self._json(403, {"error": "moderator scope required for this server"})
            return False
        return True

    def _require_admin_for(self, server):
        if self.app.auth.is_owner(self._token()):
            return True
        if trust.SCOPE_ADMIN in self._caller_scopes(server):
            return True
        self._json(403, {"error": "admin scope required for this server"})
        return False

    def _require_owner(self):
        if not self.app.auth.is_owner(self._token()):
            self._json(403, {"error": "owner token required"})
            return False
        return True

    # ---- handlers ----
    def _geometry(self, server):
        if not self._rate_limit_read():
            return
        if server not in self.app.store.networks:
            return self._json(404, {"error": "unknown server"})
        self._json(200, self.app.store.geometry_view(server))

    def _conditions(self, server):
        if not self._rate_limit_read():
            return
        if server not in self.app.store.networks:
            return self._json(404, {"error": "unknown server"})
        qs = parse_qs(urlparse(self.path).query)

        def _int(name):
            return int(qs[name][0]) if name in qs else None
        try:
            road = _int("road")
            frm = _int("from")
            to = _int("to")
        except ValueError:
            return self._json(400, {"error": "bad query param"})
        try:
            rows = self.app.store.query(server, road_idx=road, frm=frm, to=to)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"server": server, "conditions": rows})

    def _conditions_stream(self, server):
        if not self._rate_limit_read():
            return
        if server not in self.app.store.networks:
            return self._json(404, {"error": "unknown server"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.app.store.subscribe(server)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    view = q.get(timeout=15)
                    payload = json.dumps(view).encode("utf-8")
                    self.wfile.write(b"data: " + payload + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.store.unsubscribe(q)

    def _static(self, path):
        # The public map website (Phase 3) -- plain static files, no auth (same
        # public posture as the data routes it calls into), just rate-limited like
        # every other GET here. `/` -> index.html; anything else maps 1:1 onto
        # website/, resolved-and-contained so a path like /../../server can't escape
        # the website root.
        if not self._rate_limit_read():
            return
        rel = path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        candidate = (_WEBSITE_ROOT / rel).resolve()
        try:
            candidate.relative_to(_WEBSITE_ROOT)
        except ValueError:
            return self._json(404, {"error": "not found"})
        if not candidate.is_file():
            return self._json(404, {"error": "not found"})
        body = candidate.read_bytes()
        ctype = _STATIC_CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Cloudflare (and browsers) cache common static extensions like .js/.css
        # by default even with NO Cache-Control header at all from the origin --
        # a real deploy went stale in production because of exactly this (the
        # edge kept serving pre-deploy app.js). This site's whole file set is a
        # few KB and changes with every deploy, so there is no upside to caching
        # it anywhere -- opt out explicitly rather than relying on a default.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _report(self):
        # scope: 'full' -> Tier A (unilateral, any cond). 'maintainer' -> Tier M ONLY
        # for cond==CLEAR (unilateral); everything else from that token falls through
        # to Tier B (if it resolves to a linked Discord identity) or Tier C (anon) --
        # see PROTOCOL.md SS6.1, a maintainer grant deliberately doesn't include
        # new-hazard publish rights. Trust is server-scoped (SS6.3/SS6.2), and a
        # single batch can legitimately mix servers (one token might hold Tier A on
        # 2b2t.org but nothing on 6b6t.org), so scope/tier has to be resolved PER
        # ITEM from that item's own `server` field -- never once for the whole batch.
        token = self._token()
        try:
            raw = self._read_body()
            data = json.loads(raw) if raw else None
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        items = data if isinstance(data, list) else [data]
        if not items or len(items) > 512:
            return self._json(400, {"error": "empty or oversized batch"})
        accepted, rejected, tiers_used = 0, [], set()
        for i, obj in enumerate(items):
            try:
                validate_report(obj)
                server = obj["server"]
                scope = self.app.auth.write_scope(token, server)
                if scope == trust.SCOPE_FULL:
                    tier, source_key = "A", self._client_ip()
                elif scope == trust.SCOPE_MAINTAINER and obj["cond"] == "CLEAR":
                    tier, source_key = "M", self._client_ip()
                else:
                    discord_id = self.app.auth.tier_b_identity(token, server)
                    if discord_id is not None:
                        # Tier B's corroboration "source" is the discord_id, NOT the
                        # IP or mc_uid -- every UID linked to one identity on this
                        # server counts as one source (SS6.2's dedup rule; falls out
                        # for free from hashing discord_id).
                        tier, source_key = "B", discord_id
                    else:
                        tier, source_key = "C", self._client_ip()
                if tier != "A":
                    if not self.app.limiter.allow("write:" + self._client_ip(),
                                                  self.app.anon_write_limit, self.app.anon_write_window):
                        rejected.append({"i": i, "reason": "rate limited"})
                        continue
                self.app.store.ingest(obj, source_key, tier)
                tiers_used.add(tier)
                accepted += 1
            except ValueError as e:
                rejected.append({"i": i, "reason": str(e)})
        self._json(200, {"accepted": accepted, "rejected": rejected,
                          "tiers": sorted(tiers_used)})

    def _moderation_submit(self):
        if not self._rate_limit_modsubmit():
            return
        try:
            raw = self._read_body()
            obj = json.loads(raw) if raw else None
            validate_moderation(obj)
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        if obj["server"] not in self.app.store.networks:
            return self._json(404, {"error": "unknown server"})
        mid = self.app.store.add_moderation(obj)
        self._json(200, {"queued": mid})

    def _moderation_list(self, server):
        if not self._require_moderator_for(server):
            return
        self._json(200, {"pending": self.app.store.list_moderation("pending", server=server)})

    def _moderation_resolve(self, mid, action):
        server = self.app.store.moderation_server(mid)
        if server is None:
            return self._json(404, {"resolved": False})
        if not self._require_moderator_for(server):
            return
        ok = self.app.store.resolve_moderation(mid, "approved" if action == "approve" else "rejected")
        self._json(200 if ok else 404, {"resolved": ok})

    def _moderation_quash(self):
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        server = body.get("server")
        if server not in self.app.store.networks:
            return self._json(400, {"error": "unknown server"})
        if not self._require_moderator_for(server):
            return
        road, seg, along, cond = body.get("road"), body.get("seg"), body.get("along"), body.get("cond")
        if not (_is_int(road) and _is_int(seg) and _is_int(along) and isinstance(cond, str)):
            return self._json(400, {"error": "road/seg/along must be integers, cond a string"})
        try:
            ok = self.app.store.quash(server, road, seg, along, cond)
        except (KeyError, ValueError) as e:
            return self._json(400, {"error": str(e)})
        self._json(200 if ok else 404, {"quashed": ok})

    # ---- registry: Owner (all servers) or a per-server dashboard admin ----
    def _registry_issue(self):
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        holder_label = body.get("holderLabel")
        scope = body.get("scope")
        server = body.get("server")
        issued_by = body.get("issuedBy") or "owner"
        if server not in self.app.store.networks:
            return self._json(400, {"error": "unknown server"})
        if not self._require_admin_for(server):
            return
        try:
            token_id, token = self.app.auth.registry.issue(holder_label, issued_by, scope, server)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"tokenId": token_id, "token": token,
                          "note": "store this now -- it is never shown again"})

    def _registry_list(self):
        # Owner sees every server's tokens; a per-server dashboard admin only
        # ever sees the servers they were actually granted admin scope on.
        if self.app.auth.is_owner(self._token()):
            return self._json(200, {"active": self.app.auth.registry.list_active()})
        discord_id = self._session_discord_id()
        servers = self.app.auth.registry.admin_servers_for(discord_id) if discord_id else set()
        if not servers:
            return self._json(403, {"error": "admin scope required"})
        active = [t for t in self.app.auth.registry.list_active() if t["server"] in servers]
        self._json(200, {"active": active})

    def _registry_revoke(self, token_id):
        server = self.app.auth.registry.token_server(token_id)
        if server is None:
            return self._json(404, {"revoked": False})
        if not self._require_admin_for(server):
            return
        ok = self.app.auth.registry.revoke(token_id)
        self._json(200 if ok else 404, {"revoked": ok})

    # ---- account linking (Tier B, PROTOCOL.md SS6.2) ----
    def _link_config(self):
        # Public, no auth -- client_id/redirect_uri are meant to appear in a
        # browser-facing OAuth authorize URL anyway. Lets website/link.html build
        # that URL without hardcoding a deployment's client_id into committed JS.
        if not self._rate_limit_read():
            return
        if not (self.app.discord_client_id and self.app.discord_redirect_uri):
            return self._json(200, {"configured": False})
        self._json(200, {
            "configured": True,
            "clientId": self.app.discord_client_id,
            "redirectUri": self.app.discord_redirect_uri,
            "authorizeUrl": "https://discord.com/oauth2/authorize",
            "scope": "identify",
        })

    def _link_init(self):
        if self.app.auth.links is None:
            return self._json(503, {"error": "account linking not configured"})
        # Unauthenticated (no identity exists yet) -- same rate-limit posture as
        # every other unauthenticated write surface.
        if not self.app.limiter.allow("link:" + self._client_ip(),
                                      self.app.anon_write_limit, self.app.anon_write_window):
            return self._json(429, {"error": "rate limited"})
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        server = body.get("server")
        if server not in self.app.store.networks:
            return self._json(400, {"error": "unknown server"})
        try:
            code = self.app.auth.links.init_link(body.get("mcUid"), server)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"code": code})

    def _link_complete(self):
        # Called under a Discord-authenticated session (today: the ingest server's
        # own minimal OAuth completion page, until Phase 3's dedicated website
        # exists). discordCode is Discord's one-time authorization code from the
        # OAuth redirect; the server exchanges it itself (self.app.discord_verify).
        if self.app.auth.links is None:
            return self._json(503, {"error": "account linking not configured"})
        if self.app.discord_verify is None:
            return self._json(503, {"error": "Discord OAuth not configured"})
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        link_code, discord_code = body.get("linkCode"), body.get("discordCode")
        if not link_code or not discord_code:
            return self._json(400, {"error": "linkCode and discordCode required"})
        try:
            discord_id = self.app.discord_verify(discord_code)
        except identity.DiscordOAuthError as e:
            return self._json(400, {"error": f"Discord verification failed: {e}"})
        try:
            token_id, token = self.app.auth.links.complete_link(link_code, discord_id)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"tokenId": token_id, "token": token,
                          "note": "store this now -- it is never shown again"})

    def _link_bot_complete(self):
        # Alternate completion path for a first-party Discord bot (e.g. "Highway
        # Bot"): the bot already knows the caller's real discord_id with certainty
        # from Discord's own gateway/interaction signature, so there's nothing an
        # OAuth code exchange would add -- it authenticates itself instead with the
        # ARD_BOT_SECRET credential (Auth.is_bot, same hashed-flat-set shape as
        # Owner) and supplies the discord_id directly. Returns which `server` the
        # code was for so the bot knows which "<server> Verified" role to grant --
        # no guessing, no second lookup.
        if self.app.auth.links is None:
            return self._json(503, {"error": "account linking not configured"})
        if not self.app.auth.is_bot(self._token()):
            return self._json(401, {"error": "bot credential required"})
        if not self.app.limiter.allow("botlink:" + self._client_ip(),
                                      self.app.anon_write_limit, self.app.anon_write_window):
            return self._json(429, {"error": "rate limited"})
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        link_code, discord_id = body.get("linkCode"), body.get("discordId")
        if not link_code or not discord_id:
            return self._json(400, {"error": "linkCode and discordId required"})
        server = self.app.auth.links.peek_pending_server(link_code)
        if server is None:
            return self._json(400, {"error": "unknown or expired link code"})
        try:
            token_id, token = self.app.auth.links.complete_link(link_code, discord_id)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"tokenId": token_id, "token": token, "server": server,
                          "note": "store this now -- it is never shown again"})

    def _identity_action(self, server, discord_id, action):
        if not self._require_moderator_for(server):
            return
        if self.app.auth.links is None:
            return self._json(503, {"error": "account linking not configured"})
        if action == "suspend":
            ok = self.app.auth.links.suspend(discord_id, server)
            retracted = self.app.store.retract_identity(server, discord_id) if ok else 0
            self._json(200 if ok else 404, {"suspended": ok, "retracted": retracted})
        else:
            ok = self.app.auth.links.reinstate(discord_id, server)
            self._json(200 if ok else 404, {"reinstated": ok})

    # ---- admin dashboard: Discord-login sessions (trust.py discord_grants) ----
    def _admin_login(self):
        # Completes the SAME Discord OAuth redirect the account-linking flow
        # uses (website/link.html) -- see link.js's "admin:" state branch --
        # rather than needing a second redirect URI registered with Discord.
        if self.app.auth.sessions is None:
            return self._json(503, {"error": "admin sessions not configured"})
        if self.app.discord_verify is None:
            return self._json(503, {"error": "Discord OAuth not configured"})
        if not self.app.limiter.allow("adminlogin:" + self._client_ip(),
                                      self.app.anon_write_limit, self.app.anon_write_window):
            return self._json(429, {"error": "rate limited"})
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        discord_code = body.get("discordCode")
        if not discord_code:
            return self._json(400, {"error": "discordCode required"})
        try:
            discord_id = self.app.discord_verify(discord_code)
        except identity.DiscordOAuthError as e:
            return self._json(400, {"error": f"Discord verification failed: {e}"})
        grants = self.app.auth.registry.grants_for_discord(discord_id)
        if not grants:
            return self._json(403, {"error": "this Discord account has no dashboard access"})
        token = self.app.auth.sessions.create(discord_id)
        body_bytes = json.dumps({"discordId": discord_id, "grants": grants}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self._set_session_cookie(token, self.app.auth.sessions.session_ttl)
        self.end_headers()
        self.wfile.write(body_bytes)

    def _admin_logout(self):
        if self.app.auth.sessions is not None:
            tok = self._session_token()
            if tok:
                self.app.auth.sessions.revoke(tok)
        body_bytes = json.dumps({"loggedOut": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body_bytes)

    def _admin_session(self):
        discord_id = self._session_discord_id()
        if discord_id is None:
            return self._json(401, {"error": "not logged in"})
        self._json(200, {"discordId": discord_id,
                          "grants": self.app.auth.registry.grants_for_discord(discord_id)})

    def _admin_grants_list(self):
        server = parse_qs(urlparse(self.path).query).get("server", [None])[0]
        if not server or server not in self.app.store.networks:
            return self._json(400, {"error": "unknown server"})
        if not self._require_admin_for(server):
            return
        self._json(200, {"grants": self.app.auth.registry.list_discord_grants(server=server)})

    def _admin_grants_issue(self):
        try:
            raw = self._read_body()
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        discord_id, server, scope = body.get("discordId"), body.get("server"), body.get("scope")
        if server not in self.app.store.networks:
            return self._json(400, {"error": "unknown server"})
        if scope == trust.SCOPE_ADMIN:
            # Minting a new admin is the one dashboard-adjacent action that stays
            # gated behind the cold Owner secret -- never reachable from a session
            # alone, no matter which server that session has admin scope on.
            if not self.app.auth.is_owner(self._token()):
                return self._json(403, {"error": "owner token required to grant admin scope"})
        elif not self._require_admin_for(server):
            return
        granted_by = "owner" if self.app.auth.is_owner(self._token()) else f"admin:{self._session_discord_id()}"
        try:
            grant_id = self.app.auth.registry.grant_to_discord(discord_id, scope, server, granted_by)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self._json(200, {"grantId": grant_id})

    def _admin_grants_revoke(self, grant_id):
        grant = self.app.auth.registry.discord_grant_lookup(grant_id)
        if grant is None:
            return self._json(404, {"revoked": False})
        if grant["scope"] == trust.SCOPE_ADMIN:
            if not self.app.auth.is_owner(self._token()):
                return self._json(403, {"error": "owner token required to revoke admin scope"})
        elif not self._require_admin_for(grant["server"]):
            return
        ok = self.app.auth.registry.revoke_discord_grant(grant_id)
        self._json(200 if ok else 404, {"revoked": ok})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, app):
        super().__init__(addr, Handler)
        self.app = app


# --------------------------------------------------------------------------- entrypoint

def _collect_hashes(raw_tokens, hash_tokens, from_file_key, cfg):
    out = set()
    for t in raw_tokens or []:
        out.add(Auth.hash_token(t))
    for h in hash_tokens or []:
        out.add(h)
    for h in (cfg.get(from_file_key) or []):
        out.add(h)
    return out


def _seed_registry(registry, seed_specs):
    """Parse --seed-token TOKEN:SCOPE:SERVER:LABEL entries and register them with
    their existing raw token (deploy-time bootstrap -- e.g. a fleet bot's own
    config already has a token; this just makes the registry aware of it)."""
    for spec in seed_specs or []:
        parts = spec.split(":", 3)
        if len(parts) != 4:
            raise SystemExit(f"--seed-token must be TOKEN:SCOPE:SERVER:LABEL, got {spec!r}")
        token, scope, server, label = parts
        registry.issue(label, "cli-seed", scope, server, token=token)


def _seed_discord_admins(registry, admin_specs):
    """Parse --discord-admin DISCORD_ID:SERVER entries -- the only way a first
    admin dashboard grant ever gets created (same bootstrapping shape as
    --seed-token/--owner-token: something out-of-band has to seed the very
    first grant). Idempotent via grant_to_discord's own reseed logic, so this
    is safe to run on every restart of a persistent deployment."""
    for spec in admin_specs or []:
        parts = spec.split(":", 1)
        if len(parts) != 2:
            raise SystemExit(f"--discord-admin must be DISCORD_ID:SERVER, got {spec!r}")
        discord_id, server = parts
        registry.grant_to_discord(discord_id, trust.SCOPE_ADMIN, server, "cli-seed")


def apply_env_secrets(args, environ=None):
    """CLI flags are convenient for local/dev runs but land in `ps aux` for anyone
    else on the box -- a real concern once this runs as a systemd unit on a
    shared, multi-tenant VPS alongside other processes. These env vars are the
    supported alternative for systemd EnvironmentFile-style secret injection;
    the production ExecStart= should carry no secret flags at all. CLI flags
    still work and are ADDITIVE with the environment, never overridden by it --
    dev convenience and production hardening don't need to be mutually exclusive.
    Also folds in a couple of non-secret deploy-time settings (trusted proxies)
    for the same reason: one place to layer environment config in before
    build_app runs."""
    environ = os.environ if environ is None else environ
    owner_env = environ.get("ARD_OWNER_TOKEN")
    if owner_env:
        args.owner_token = (args.owner_token or []) + [owner_env]
    seed_entries = [s.strip() for s in environ.get("ARD_SEED_TOKENS", "").split(",") if s.strip()]
    if seed_entries:
        args.seed_token = (args.seed_token or []) + seed_entries
    if not args.discord_client_secret:
        args.discord_client_secret = environ.get("ARD_DISCORD_CLIENT_SECRET") or None
    admin_entries = [s.strip() for s in environ.get("ARD_DISCORD_ADMINS", "").split(",") if s.strip()]
    if admin_entries:
        args.discord_admin = (getattr(args, "discord_admin", None) or []) + admin_entries
    bot_env = environ.get("ARD_BOT_SECRET")
    if bot_env:
        args.bot_token = (getattr(args, "bot_token", None) or []) + [bot_env]
    proxy_entries = [s.strip() for s in environ.get("ARD_TRUSTED_PROXIES", "").split(",") if s.strip()]
    if proxy_entries:
        args.trusted_proxy = (getattr(args, "trusted_proxy", None) or []) + proxy_entries
    return args


def build_app(args):
    store = Store(args.geometry, db_path=args.db, bucket=args.bucket,
                  k_anon=args.k_anon, k_tier_b=args.k_tier_b, ttl=args.ttl,
                  clear_factor=args.clear_factor, reopen_window=args.reopen_window,
                  max_travel_speed=args.max_travel_speed)
    registry = trust.Registry(db_path=args.registry_db)
    _seed_registry(registry, args.seed_token)
    _seed_discord_admins(registry, args.discord_admin)
    links = identity.LinkStore(db_path=args.identity_db, link_code_ttl=args.link_code_ttl,
                                max_linked_uids=args.max_linked_uids)
    session_store = sessions_mod.SessionStore(db_path=args.session_db, session_ttl=args.session_ttl)
    cfg = {}
    if args.tokens_file and Path(args.tokens_file).exists():
        cfg = json.loads(Path(args.tokens_file).read_text())
    owner = _collect_hashes(args.owner_token, args.owner_hash, "owner", cfg)
    bot = _collect_hashes(getattr(args, "bot_token", None), getattr(args, "bot_hash", None), "bot", cfg)
    auth = Auth(registry, links=links, owner_hashes=owner, sessions=session_store, bot_hashes=bot)
    discord_verify = None
    if args.discord_client_id and args.discord_client_secret and args.discord_redirect_uri:
        discord_verify = functools.partial(
            identity.discord_exchange, args.discord_client_id,
            args.discord_client_secret, args.discord_redirect_uri)
    trusted_proxies = list(DEFAULT_TRUSTED_PROXIES) + list(getattr(args, "trusted_proxy", None) or [])
    return App(store, auth, discord_verify=discord_verify,
               discord_client_id=args.discord_client_id,
               discord_redirect_uri=args.discord_redirect_uri,
               trusted_proxies=trusted_proxies)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Highway Conditions ingest/aggregation service")
    ap.add_argument("--geometry", default=str(Path(__file__).resolve().parent.parent / "geometry"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--bucket", type=int, default=DEFAULT_BUCKET)
    ap.add_argument("--k-anon", type=int, default=2,
                     help="Tier C (anonymous, IP-hash) corroboration threshold (K_TIER_C_NEW)")
    ap.add_argument("--k-tier-b", type=int, default=2,
                     help="Tier B (Discord-verified identity) corroboration threshold "
                          "(K_TIER_B_NEW) -- lower than --k-anon")
    ap.add_argument("--ttl", type=int, default=10800,
                     help="condition decay window in seconds (default 3h -- tripled from the "
                          "original 1h since traffic density varies a lot across the highway "
                          "network; a sparsely-traveled road shouldn't expire before someone "
                          "else happens to pass through and refresh it)")
    ap.add_argument("--clear-factor", type=int, default=2,
                     help="a CLEAR needs this many times the normal corroboration threshold "
                          "(PROTOCOL.md K_CLEAR_FACTOR, SS6.4)")
    ap.add_argument("--reopen-window", type=int, default=3600,
                     help="a hazard reopening within this many seconds of a published clear "
                          "is flagged to /moderation instead of silently republished "
                          "(PROTOCOL.md MAINTAINER_REOPEN_WINDOW, SS6.4)")
    ap.add_argument("--max-travel-speed", type=float, default=MAX_TRAVEL_SPEED_DEFAULT,
                     help="blocks/sec -- a Tier B/C identity claiming two positions further "
                          "apart than this implies within the elapsed time between them is "
                          "flagged as travel-implausible (excluded from corroboration, small "
                          "reputation penalty); default is well above any real travel mode")
    ap.add_argument("--registry-db", default=":memory:",
                     help="SQLite path for the Owner-issued token registry (PROTOCOL.md SS6.3)")
    ap.add_argument("--seed-token", action="append",
                     help="bootstrap a registry entry with an existing raw token: "
                          "TOKEN:SCOPE:SERVER:LABEL, SCOPE in full|maintainer|moderator -- or "
                          "set ARD_SEED_TOKENS to a comma-separated list of the same")
    ap.add_argument("--owner-token", action="append",
                     help="raw Owner token (hashed at load) -- or set ARD_OWNER_TOKEN so it "
                          "never appears in `ps aux`")
    ap.add_argument("--owner-hash", action="append", help="sha256 of an Owner token")
    ap.add_argument("--tokens-file", help='JSON {"owner":[<sha256>...], "bot":[<sha256>...]}')
    ap.add_argument("--identity-db", default=":memory:",
                     help="SQLite path for the Tier B link store (PROTOCOL.md SS6.2)")
    ap.add_argument("--link-code-ttl", type=int, default=identity.DEFAULT_LINK_CODE_TTL,
                     help="seconds an unclaimed /link/init code stays valid (LINK_CODE_TTL)")
    ap.add_argument("--max-linked-uids", type=int, default=identity.DEFAULT_MAX_LINKED_UIDS,
                     help="max Minecraft UIDs one Discord identity may link (MAX_LINKED_UIDS)")
    ap.add_argument("--discord-client-id", help="Discord OAuth app client ID")
    ap.add_argument("--discord-client-secret", help="Discord OAuth app client secret -- keep "
                                                      "this out of shell history/version control; "
                                                      "or set ARD_DISCORD_CLIENT_SECRET")
    ap.add_argument("--discord-redirect-uri", help="must exactly match a redirect URI "
                                                     "registered on the Discord app")
    ap.add_argument("--session-db", default=":memory:",
                     help="SQLite path for admin dashboard login sessions (sessions.py)")
    ap.add_argument("--session-ttl", type=int, default=sessions_mod.DEFAULT_SESSION_TTL,
                     help="seconds an admin dashboard login session stays valid (default 12h)")
    ap.add_argument("--discord-admin", action="append",
                     help="bootstrap a Discord identity with admin dashboard scope: "
                          "DISCORD_ID:SERVER -- or set ARD_DISCORD_ADMINS to a "
                          "comma-separated list of the same. This is the only way a "
                          "first admin grant is ever created (same bootstrap shape "
                          "as --owner-token)")
    ap.add_argument("--bot-token", action="append",
                     help="raw first-party bot credential (hashed at load), unlocks only "
                          "POST /link/bot-complete -- or set ARD_BOT_SECRET so it never "
                          "appears in `ps aux`")
    ap.add_argument("--bot-hash", action="append", help="sha256 of a bot credential")
    ap.add_argument("--trusted-proxy", action="append",
                     help="CIDR or IP allowed to supply the real client address via "
                          "CF-Connecting-IP/X-Forwarded-For, in addition to loopback "
                          "(always trusted) -- or set ARD_TRUSTED_PROXIES to a "
                          "comma-separated list of the same")
    args = apply_env_secrets(ap.parse_args(argv))

    app = build_app(args)
    srv = Server((args.host, args.port), app)
    print(f"highway-conditions {VERSION} on http://{args.host}:{args.port} "
          f"servers={sorted(app.store.networks)} k={args.k_anon} ttl={args.ttl}s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
