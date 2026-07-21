"""
Admin dashboard sessions — cookie-based auth for a Discord identity that holds
a discord_grants scope (trust.py: 'moderator' or 'admin'), separate from the
bearer-token registry used by bots/producers. A session credential is a
random token handled exactly like every other secret in this project: only
its hash is stored, the raw value is set once as an HttpOnly cookie and never
retrievable again.

Deliberately short-lived (SESSION_TTL) — the dashboard is a human clicking
around, not a long-lived bot connection. Sessions are individually and bulk
(per-identity) revocable, so an admin can be logged out everywhere without
needing to know which browser/device is affected.

Pure stdlib, SQLite-backed, same style as Store/Registry/LinkStore.
"""

import hashlib
import secrets
import sqlite3
import threading
import time

DEFAULT_SESSION_TTL = 12 * 3600  # 12h -- a human dashboard session, not a bot token


class SessionStore:
    def __init__(self, db_path=":memory:", session_ttl=DEFAULT_SESSION_TTL, clock=time.time):
        self.session_ttl = session_ttl
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          session_hash TEXT PRIMARY KEY,
          discord_id TEXT NOT NULL,
          created_at REAL NOT NULL,
          expires_at REAL NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0
        );
        """)
        self.db.commit()

    @staticmethod
    def hash_session(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def create(self, discord_id):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self.db.execute(
                "INSERT INTO sessions(session_hash,discord_id,created_at,expires_at,revoked)"
                " VALUES(?,?,?,?,0)",
                (self.hash_session(token), discord_id, now, now + self.session_ttl))
            self.db.commit()
        return token

    def discord_id_for(self, token):
        """The discord_id behind a live (non-revoked, non-expired) session
        token, or None."""
        if not token:
            return None
        now = self.clock()
        with self._lock:
            row = self.db.execute(
                "SELECT discord_id FROM sessions WHERE session_hash=? AND revoked=0 AND expires_at>=?",
                (self.hash_session(token), now)).fetchone()
        return row[0] if row else None

    def revoke(self, token):
        with self._lock:
            cur = self.db.execute(
                "UPDATE sessions SET revoked=1 WHERE session_hash=?", (self.hash_session(token),))
            self.db.commit()
            return cur.rowcount > 0

    def revoke_all_for(self, discord_id):
        """Kill every live session for this identity, independent of which
        device/browser it's on."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE sessions SET revoked=1 WHERE discord_id=? AND revoked=0", (discord_id,))
            self.db.commit()
            return cur.rowcount
