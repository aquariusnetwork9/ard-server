"""Optional push alerts (ntfy-compatible plain POST).

Fire-and-forget: a send never blocks the request path and a delivery failure never
surfaces past this module. Per-key throttling so a repeating condition produces one
alert per interval, not one per occurrence.
"""

import threading
import time
import urllib.request

# A real User-Agent, not urllib's default -- Cloudflare-fronted endpoints block
# default HTTP-tool signatures outright (same fix identity.py already carries for
# Discord's API).
USER_AGENT = "ARD-notify/1.0 (+https://map.aquariusconnect.org)"

DEFAULT_MIN_INTERVAL = 600  # seconds between repeat alerts for the same key


class Notifier:
    """POSTs `message` to a single configured topic URL, ntfy header style.

    url=None disables the whole thing (every send() is a cheap no-op) -- callers
    never need to guard. `transport` and `async_send` are injectable for tests.
    """

    def __init__(self, url=None, token=None, min_interval=DEFAULT_MIN_INTERVAL,
                 clock=time.time, transport=None, async_send=True):
        self.url = url
        self.token = token
        self.min_interval = min_interval
        self.clock = clock
        self.async_send = async_send
        self._transport = transport or self._post
        self._last = {}
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.url)

    def send(self, key, title, message, priority="default"):
        """Dispatch an alert unless the same key fired within min_interval.
        Returns True if dispatched (or handed to the background thread)."""
        if not self.url:
            return False
        now = self.clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and (now - last) < self.min_interval:
                return False
            self._last[key] = now
        if self.async_send:
            threading.Thread(target=self._safe_send,
                             args=(title, message, priority), daemon=True).start()
        else:
            self._safe_send(title, message, priority)
        return True

    def _safe_send(self, title, message, priority):
        try:
            self._transport(title, message, priority)
        except Exception:
            pass  # alerting must never take the service down with it

    def _post(self, title, message, priority):
        req = urllib.request.Request(
            self.url, data=message.encode(),
            headers={"Title": title, "Priority": priority, "User-Agent": USER_AGENT},
            method="POST")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=10):
            pass
