"""Third-party presence oracle: is a given Minecraft UID actually online on the
server it's reporting about? Optional and per-server pluggable -- as of writing,
only 2b2t.org has a usable third-party source (the 2b2t.vc bot-network API, which
watches the live tablist 24/7 and exposes it with no auth required).

An oracle's `is_present(mc_uid)` returns True/False when it has an answer, or None
when it can't say (the third party is unreachable, or hasn't been asked yet) --
callers must treat None as "unknown," never as "absent." A third party's outage
must never itself become a lever to suppress real reporting.
"""

import json
import threading
import time
import urllib.request

USER_AGENT = "ARD-presence/1.0 (+https://map.aquariusconnect.org)"


def _normalize_uuid(u):
    return (u or "").replace("-", "").lower()


class TwoBTwoTVCPresence:
    """Backed by api.2b2t.vc's GET /tablist/info (no auth). Cached briefly so a
    burst of Tier B reports doesn't hammer a third party's API on every ingest
    call -- one fetch serves every check within CACHE_TTL seconds."""

    BASE_URL = "https://api.2b2t.vc"
    CACHE_TTL = 20  # seconds

    def __init__(self, clock=time.time, fetch=None, timeout=5):
        self.clock = clock
        self.timeout = timeout
        self._fetch = fetch or self._http_fetch
        self._lock = threading.Lock()
        self._cached_uuids = None
        self._cached_at = None

    def is_present(self, mc_uid):
        uuids = self._tablist_uuids()
        if uuids is None:
            return None
        return _normalize_uuid(mc_uid) in uuids

    def _tablist_uuids(self):
        with self._lock:
            now = self.clock()
            if self._cached_uuids is not None and (now - self._cached_at) < self.CACHE_TTL:
                return self._cached_uuids
            try:
                players = self._fetch()
                uuids = {_normalize_uuid(p["uuid"]) for p in players}
            except Exception:
                return None  # a fetch/parse failure must never crash ingest
            self._cached_uuids = uuids
            self._cached_at = now
            return uuids

    def _http_fetch(self):
        req = urllib.request.Request(self.BASE_URL + "/tablist/info",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data.get("players", [])
