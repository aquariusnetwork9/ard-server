#!/usr/bin/env python3
"""
Mock contributor + wire-payload audit harness for the Highway Conditions service.

Runs the real ingest service in-process and drives it over HTTP the way a bot/addon would:

  1. Simulate a trajectory: a leg ALONG a spawn highway (on-road) and a detour to a fake
     off-highway "base". Report only what the client gate allows.
  2. AUDIT every wire payload actually sent: assert none carry x/z/offset — the off-highway
     leg must produce ZERO reports.
  3. Fuzz the ingest with malformed / coordinate-smuggling payloads and show they're rejected.
  4. Read back the published conditions through the gated API.

No real coordinates are used anywhere (fake base within 20k of 0,0), per the standing rule.

Usage:  python tools/hc_mock.py
"""

import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from highway_conditions import Store, Auth, App, Server  # noqa: E402
import reference_client  # noqa: E402
import trust  # noqa: E402

SERVER = "2b2t.org"
FLEET = "MOCK-FLEET-TOKEN"
FORBIDDEN = {"x", "z", "y", "dist", "distance", "offset", "lat", "lon", "yaw", "pitch"}

_sent_payloads = []  # every object we actually POST to /report (for the audit)


def _req(method, url, token=None, body=None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = token
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    store = Store(str(ROOT / "geometry"), k_anon=2, ttl=3600, salt="mock")
    registry = trust.Registry()
    registry.issue("mock-fleet-bot", "mock", trust.SCOPE_FULL, SERVER, token=FLEET)
    auth = Auth(registry)
    app = App(store, auth)
    srv = Server(("127.0.0.1", 0), app)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    net = store.networks[SERVER]
    mh = store.map_hashes[SERVER]

    def build(x, y, z, cond="CLEAR"):
        return reference_client.build_report(x, y, z, "NETHER", net, mh, SERVER, cond=cond)

    print(f"== highway-conditions mock ==  server on {base}  map={mh}\n")

    # ---- 1. trajectory ------------------------------------------------------------------
    # On-road leg: travel out the z=0 (south/north) axis; drop a couple of hazards.
    # Step matches BUCKET (100) so consecutive samples never land in the same
    # along-bucket as a hazard -- otherwise a "CLEAR" 50 blocks past a hazard would
    # share its bucket and immediately resolve it (PROTOCOL.md SS6.4 reconciliation,
    # working as intended -- this demo just needs samples that don't collide).
    hazards = {1500: "HOLE", 2200: "LAVA", 2600: "OBSTRUCTION_FULL"}
    on_road = off_road = 0
    for x in range(0, 3001, 100):
        cond = hazards.get(x, "CLEAR")
        rep = build(x, 120, 0, cond)
        if rep is None:
            off_road += 1
            continue
        _sent_payloads.append(rep)
        _req("POST", base + "/report", token=FLEET, body=rep)
        on_road += 1

    # Off-road leg: a fake base + wiggles around it, all at y120. The gate must suppress ALL.
    suppressed = 0
    for (bx, bz) in [(12500, 7500), (12480, 7520), (12550, 7450), (12500, 7500)]:
        rep = build(bx, 120, bz, "CLEAR")
        if rep is None:
            suppressed += 1
        else:
            _sent_payloads.append(rep)  # would be a leak — the audit below would catch it
            _req("POST", base + "/report", token=FLEET, body=rep)

    # Wrong-Y leg: on the axis but at y=118 (roof-adjacent). Suppressed from auto-report.
    wrongy = 1 if build(1000, 118, 0) is None else 0

    print(f"trajectory: {on_road} on-road reports sent, "
          f"{off_road} positions off the axis skipped mid-travel")
    print(f"off-road base leg: {suppressed}/4 positions SUPPRESSED (0 reports built)")
    print(f"wrong-Y (y118) on-axis: {'suppressed' if wrongy else 'LEAKED'}\n")

    # ---- 2. wire-payload audit ------------------------------------------------------------
    leaks = [p for p in _sent_payloads if FORBIDDEN & set(p)]
    print(f"AUDIT: {len(_sent_payloads)} wire payloads inspected; "
          f"{len(leaks)} carried a coordinate/offset field.")
    assert not leaks, f"COORDINATE LEAK in wire payload: {leaks[:1]}"
    assert suppressed == 4, "off-road base positions were NOT fully suppressed"
    print("AUDIT PASS: no off-highway coordinate could enter the network.\n")

    # ---- 3. fuzz ------------------------------------------------------------------------
    good = build(500, 120, 0, "HOLE")
    fuzz = [
        {**good, "x": -1_200_000},            # smuggled base coordinate
        {**good, "cond": "NUKE"},             # bad enum
        {"garbage": True},                    # unknown shape
        {**good, "road": True},               # bool-as-int
    ]
    code, body = _req("POST", base + "/report", token=FLEET, body=fuzz)
    print(f"fuzz batch of {len(fuzz)}: accepted={body['accepted']} "
          f"rejected={len(body['rejected'])}")
    for rej in body["rejected"]:
        print(f"   rejected[{rej['i']}]: {rej['reason']}")
    assert body["accepted"] == 0, "a malformed/smuggling payload was accepted!"
    print()

    # ---- 4. public read -------------------------------------------------------------------
    code, body = _req("GET", base + f"/conditions/{SERVER}")  # no token -- reads are public (SS7)
    print(f"read without any token -> HTTP {code} (must be 200; reads are public)")
    assert code == 200
    conds = body["conditions"]
    print(f"{len(conds)} published conditions:")
    for c in conds:
        if c["cond"] != "CLEAR":
            print(f"   {c['cond']:<12} road={c['road']} seg={c['seg']} along={c['along']} "
                  f"@(x={c['x']}, z={c['z']}) conf={c['confidence']} tier={c['tier']}")
    print("\nOK - mock run complete.")
    srv.shutdown()


if __name__ == "__main__":
    main()
