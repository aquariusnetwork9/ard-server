/* Aquarius Road Department — public map (Phase 3).
 *
 * This is just another PROTOCOL.md consumer: it fetches the public road
 * geometry (/geometry/<server>) and published conditions (/conditions/<server>,
 * plus /stream for a live nudge) exactly like any producer/consumer would, over
 * the same open, unauthenticated read routes. No server-side rendering, no
 * session, no login -- reads are public by design (PROTOCOL.md SS7).
 */
(function () {
  "use strict";

  // world (x,z) -> Leaflet latlng. -z is "up" (north), x is "right" (east),
  // matching the usual top-down Minecraft map convention. CRS.Simple, no scaling
  // needed -- highway coordinates are already a flat plane.
  function w2ll(x, z) { return [-z, x]; }
  // ...and the inverse, for turning a cursor position back into world (x,z).
  function ll2w(latlng) { return [latlng.lng, -latlng.lat]; }

  // Roads already have a sensible, hand-curated name in the geometry data
  // itself (e.g. "7.5k ringroad (7x4, y113)", "farlands ringroad (3x3)",
  // "Cardinals (6x4) + Diagonals (7x4) dug") -- strip a trailing "(dig
  // dimensions[, note])" suffix and normalize "ring road"/"ringroad" to "RR"
  // rather than re-deriving a label from the raw radius, which reads badly
  // for the handful of rings that aren't a round number of blocks (the
  // farlands ring is ~1,568,852 out -- "1568.9k RR" is much worse than the
  // name's own "farlands").
  function roadLabel(name) {
    var base = name.replace(/\s*\([^)]*\)\s*$/, "").trim();
    return base.replace(/ring\s?road/i, "RR").replace(/\s+/g, " ").trim();
  }

  // The "axis" category combines all 8 cardinal/diagonal rays (each running
  // BOTH directions through spawn) into just 2 road entries (dug/paved), so
  // their own name ("Cardinals (6x4) + Diagonals (7x4) dug") is really just
  // dig-width metadata, not something to show as a road name -- a compass
  // direction, taken straight from which side of spawn is actually on screen,
  // is what's actually useful while traveling one of these.
  function compassLabel(x, z) {
    var ns = z < 0 ? "N" : z > 0 ? "S" : "";
    var ew = x > 0 ? "E" : x < 0 ? "W" : "";
    return ns + ew;
  }

  // Liang-Barsky segment/rectangle clip -- given a straight world-space
  // segment and the world-space rectangle currently visible on screen,
  // returns the midpoint of whichever portion of the segment is actually
  // in view, or null if none of it is. This is what lets a label "follow"
  // the viewport along a long road (a 30,000,000-block dug highway ray, an
  // 8-segment ring) the way Google Maps repositions a road's name as you pan,
  // rather than pinning it to one fixed point that scrolls out of view.
  function clipMidpoint(x1, z1, x2, z2, b) {
    var dx = x2 - x1, dz = z2 - z1;
    var t0 = 0, t1 = 1;
    var p = [-dx, dx, -dz, dz];
    var q = [x1 - b.xmin, b.xmax - x1, z1 - b.zmin, b.zmax - z1];
    for (var i = 0; i < 4; i++) {
      if (p[i] === 0) {
        if (q[i] < 0) return null; // parallel to this edge and outside it
      } else {
        var r = q[i] / p[i];
        if (p[i] < 0) { if (r > t1) return null; if (r > t0) t0 = r; }
        else { if (r < t0) return null; if (r < t1) t1 = r; }
      }
    }
    if (t0 > t1) return null;
    var tm = (t0 + t1) / 2;
    return [x1 + tm * dx, z1 + tm * dz];
  }

  var COND_STYLE = {
    HOLE:                { color: "#e0503a", label: "hole" },
    LAVA:                { color: "#ff7a1a", label: "lava" },
    OBSTRUCTION_FULL:    { color: "#e0503a", label: "obstruction (full)" },
    OBSTRUCTION_PARTIAL: { color: "#e0a92f", label: "obstruction (partial)" },
    COBWEB:              { color: "#9aa0ab", label: "cobweb" },
    WATER:               { color: "#4aa3ff", label: "water" },
    GRAVEL:              { color: "#a67c52", label: "gravel" },
    UNBUILT:             { color: "#b25cff", label: "unbuilt gap" },
    PRESENCE:            { color: "#ff5ca8", label: "presence (camper)" },
    CLEAR:                { color: "#3ecf6e", label: "clear" },
  };
  var DEFAULT_STYLE = { color: "#888", label: "unknown" };

  var mapEl = document.getElementById("map");
  var serverSelect = document.getElementById("server-select");
  var connDot = document.getElementById("conn-dot");
  var statusLine = document.getElementById("status-line");
  var legendItems = document.getElementById("legend-items");

  var coordTooltip = document.getElementById("coord-tooltip");

  // minZoom is deliberately very permissive -- the road network now reaches
  // all the way out to the world border ring (~3.75M blocks), and with labels
  // on all of it (see updateRoadLabels below) there's a real reason to let
  // someone scroll all the way out to see it, not just the near-spawn cluster.
  var map = L.map(mapEl, { crs: L.CRS.Simple, minZoom: -14, maxZoom: 6, zoomControl: true })
    .setView([0, 0], -1);

  var roadLayer = L.layerGroup().addTo(map);
  var labelLayer = L.layerGroup().addTo(map);
  var hazardLayer = L.layerGroup().addTo(map);

  // A tooltip that follows the cursor directly, not a fixed topbar readout --
  // positioned in plain viewport pixels (clientX/clientY) rather than through
  // Leaflet's own projection, since it only ever needs to track the mouse.
  map.on("mousemove", function (e) {
    var w = ll2w(e.latlng);
    coordTooltip.textContent = "x " + Math.round(w[0]) + "  z " + Math.round(w[1]);
    coordTooltip.style.left = e.originalEvent.clientX + "px";
    coordTooltip.style.top = e.originalEvent.clientY + "px";
    coordTooltip.classList.remove("hidden");
  });
  map.on("mouseout", function () { coordTooltip.classList.add("hidden"); });

  var currentServer = null;
  var eventSource = null;
  var refreshTimer = null;
  var refreshPending = false;

  function buildLegend() {
    legendItems.innerHTML = "";
    Object.keys(COND_STYLE).forEach(function (cond) {
      if (cond === "CLEAR") return; // CLEAR never appears as a standing marker
      var s = COND_STYLE[cond];
      var row = document.createElement("div");
      row.className = "legend-row";
      row.innerHTML = '<span class="legend-swatch" style="background:' + s.color + '"></span>' + s.label;
      legendItems.appendChild(row);
    });
  }

  function setConn(state, text) {
    connDot.className = "dot " + state;
    if (text) statusLine.textContent = text;
  }

  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(url + " -> " + r.status);
      return r.json();
    });
  }

  function agoString(unixSeconds) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - unixSeconds));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    return Math.round(s / 3600) + "h ago";
  }

  function popupHTML(v) {
    var s = COND_STYLE[v.cond] || DEFAULT_STYLE;
    var lines = [
      "<b>" + s.label + "</b> (tier " + v.tier + ")",
      "reports: " + v.reports + " · distinct sources: " + v.distinctSources,
      "confidence: " + v.confidence,
    ];
    if (v.laneMin !== null && v.laneMin !== undefined) {
      lines.push("lane span: " + v.laneMin + " .. " + v.laneMax);
    }
    lines.push("last seen " + agoString(v.lastSeen));
    return '<div class="hz-popup">' + lines.join("<br>") + "</div>";
  }

  // Every road currently loaded, as {text, segments} -- populated by
  // drawRoads(), consumed by updateRoadLabels() on every pan/zoom.
  var roadsForLabels = [];

  function worldBounds() {
    var b = map.getBounds();
    var nw = ll2w(b.getNorthWest()), se = ll2w(b.getSouthEast());
    return {
      xmin: Math.min(nw[0], se[0]), xmax: Math.max(nw[0], se[0]),
      zmin: Math.min(nw[1], se[1]), zmax: Math.max(nw[1], se[1]),
    };
  }

  // One label per road, wherever it's currently on screen -- the first
  // segment (in the road's own order) whose visible portion is non-empty.
  // A ring/diamond loop's label jumps to whichever edge is in view as you
  // pan around it; a long ray highway's label simply appears once you've
  // scrolled far enough out for any part of it to be visible, same as it
  // would for a fully in-view road, and follows as you keep panning along
  // it. Bounding it to one label per road (rather than one per visible
  // segment) also keeps the 74-segment near-spawn grid from turning into 74
  // overlapping copies of the same label.
  function updateRoadLabels() {
    labelLayer.clearLayers();
    var b = worldBounds();
    roadsForLabels.forEach(function (road) {
      for (var i = 0; i < road.segments.length; i++) {
        var seg = road.segments[i];
        var mid = clipMidpoint(seg[0], seg[1], seg[2], seg[3], b);
        if (mid) {
          var text = road.category === "axis" ? compassLabel(mid[0], mid[1]) : road.text;
          if (!text) continue; // exactly at spawn -- no direction to show yet
          L.marker(w2ll(mid[0], mid[1]), {
            icon: L.divIcon({ className: "road-label-icon", html: '<div class="road-label">' + text + "</div>" }),
            interactive: false,
            keyboard: false,
          }).addTo(labelLayer);
          break;
        }
      }
    });
  }

  var labelUpdateQueued = false;
  function scheduleLabelUpdate() {
    if (labelUpdateQueued) return;
    labelUpdateQueued = true;
    requestAnimationFrame(function () { labelUpdateQueued = false; updateRoadLabels(); });
  }
  map.on("move zoom", scheduleLabelUpdate);

  function drawRoads(geo) {
    roadLayer.clearLayers();
    roadsForLabels = [];
    geo.roads.forEach(function (road) {
      var latlngs = road.segments.map(function (seg) {
        return [w2ll(seg[0], seg[1]), w2ll(seg[2], seg[3])];
      });
      latlngs.forEach(function (pair) {
        L.polyline(pair, { color: "#4a5262", weight: 2, opacity: 0.6 }).addTo(roadLayer);
      });
      roadsForLabels.push({ text: roadLabel(road.name), category: road.category, segments: road.segments });
    });
    var allPts = geo.roads.flatMap(function (r) {
      return r.segments.flatMap(function (seg) {
        return [w2ll(seg[0], seg[1]), w2ll(seg[2], seg[3])];
      });
    });
    if (allPts.length) {
      map.fitBounds(allPts, { padding: [40, 40] });
      // The full bounds reach all the way to the world border, so a true tight
      // fit would zoom the *default* view out to a scale where the near-spawn
      // area everyone actually cares about is a speck -- pull back one step
      // further than that tight fit (matching the ask: "zoom out a little
      // further"), but floor it so it doesn't chase the world-border points
      // all the way out. minZoom itself stays permissive for anyone who wants
      // to manually scroll out further than this default.
      map.setZoom(Math.max(map.getZoom() - 1, -5));
    }
    updateRoadLabels();
  }

  function drawHazards(rows) {
    hazardLayer.clearLayers();
    rows.forEach(function (v) {
      if (v.x === null || v.z === null) return; // off the known road table -- nothing to plot
      var s = COND_STYLE[v.cond] || DEFAULT_STYLE;
      // NOTE: `sev` is validated on ingest but Store doesn't persist/aggregate it
      // yet (no column on `conditions`), so there's no real severity signal to
      // size markers by here -- constant radius until that's added server-side.
      L.circleMarker(w2ll(v.x, v.z), {
        radius: 7, color: s.color, weight: 2,
        fillColor: s.color, fillOpacity: 0.55,
      }).bindPopup(popupHTML(v)).addTo(hazardLayer);
    });
  }

  function refreshConditions() {
    if (!currentServer || refreshPending) return;
    refreshPending = true;
    fetchJSON("/conditions/" + encodeURIComponent(currentServer))
      .then(function (data) {
        drawHazards(data.conditions);
        setConn("live", data.conditions.length + " active condition(s) · updated " + new Date().toLocaleTimeString());
      })
      .catch(function () {
        setConn("stale", "couldn't refresh conditions, retrying…");
      })
      .finally(function () { refreshPending = false; });
  }

  function connectStream(server) {
    if (eventSource) eventSource.close();
    eventSource = new EventSource("/conditions/" + encodeURIComponent(server) + "/stream");
    eventSource.onopen = function () { setConn("live"); };
    // The stream is a low-latency "something changed" nudge, not an incremental
    // patch feed -- CLEAR/reopen reconciliation only happens at query() time, so
    // the simplest correct thing is to just re-fetch the authoritative list.
    eventSource.onmessage = function () { refreshConditions(); };
    eventSource.onerror = function () { setConn("stale", "live stream reconnecting…"); };
  }

  function loadServer(server) {
    currentServer = server;
    if (refreshTimer) clearInterval(refreshTimer);
    setConn("stale", "loading " + server + "…");
    fetchJSON("/geometry/" + encodeURIComponent(server))
      .then(function (geo) {
        drawRoads(geo);
        refreshConditions();
        connectStream(server);
        refreshTimer = setInterval(refreshConditions, 20000);
      })
      .catch(function () { setConn("down", "couldn't load geometry for " + server); });
  }

  function init() {
    buildLegend();
    fetchJSON("/health")
      .then(function (h) {
        var servers = h.servers || [];
        serverSelect.innerHTML = "";
        servers.forEach(function (s) {
          var opt = document.createElement("option");
          opt.value = s; opt.textContent = s;
          serverSelect.appendChild(opt);
        });
        if (!servers.length) { setConn("down", "no servers configured"); return; }
        serverSelect.addEventListener("change", function () { loadServer(serverSelect.value); });
        loadServer(servers[0]);
      })
      .catch(function () { setConn("down", "couldn't reach the service"); });
  }

  init();
})();
