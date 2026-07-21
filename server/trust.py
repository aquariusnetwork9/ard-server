"""
Owner-issued token registry — PROTOCOL.md §6.3.

Backs three scopes off one table: `full` (Tier A), `maintainer` (Tier M), and
`moderator`. A holder can be issued more than one — they're independent grants,
not a hierarchy. Pure stdlib, SQLite-backed, same style as Store in
highway_conditions.py.

Every grant is scoped to exactly one `server` — a token vouched for one
anarchy server's highway crew carries no authority on another, even within the
same deployment/Owner (e.g. 2b2t.org and 6b6t.org sharing one registry DB).
Trust doesn't transfer automatically; someone trusted on both gets two tokens.
The Owner secret itself stays deployment-wide (out-of-band, not a registry
entry at all) — this scoping is specifically about registry *grants*.

Only the token's hash is ever stored; the raw token is returned once, at issue
time, and never retrievable again. Revocation is keyed by the public `token_id`
(NOT the secret) — the whole point of revoking is often that you no longer
have (or trust) the holder's copy of the secret.
"""

import hashlib
import secrets
import sqlite3
import threading
import time

SCOPE_FULL = "full"
SCOPE_MAINTAINER = "maintainer"
SCOPE_MODERATOR = "moderator"
SCOPES = {SCOPE_FULL, SCOPE_MAINTAINER, SCOPE_MODERATOR}

# Admin dashboard: scope granted directly to a Discord identity (no bearer
# token involved) rather than to a token holder. SCOPE_MODERATOR is shared
# with the bearer-token registry above -- day-to-day queue/suspend work means
# the same thing whether reached via a bot's token or a human's dashboard
# session. SCOPE_ADMIN is dashboard-only and has no bearer-token equivalent:
# it can issue/revoke registry tokens and moderator grants for its own
# server(s), but minting or revoking ANOTHER admin grant stays gated behind
# the Owner secret specifically (see highway_conditions.py) -- never reachable
# from a dashboard session alone.
SCOPE_ADMIN = "admin"
DISCORD_GRANT_SCOPES = {SCOPE_MODERATOR, SCOPE_ADMIN}


class Registry:
    """Owner-managed token registry backing Tier A, Tier M, and moderator scope."""

    def __init__(self, db_path=":memory:", clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tokens(
          token_id TEXT PRIMARY KEY,
          token_hash TEXT UNIQUE NOT NULL,
          holder_label TEXT NOT NULL,
          issued_by TEXT NOT NULL,
          issued_at REAL NOT NULL,
          scope TEXT NOT NULL,
          server TEXT NOT NULL DEFAULT '',
          revoked INTEGER NOT NULL DEFAULT 0,
          revoked_at REAL
        );
        CREATE TABLE IF NOT EXISTS discord_grants(
          grant_id TEXT PRIMARY KEY,
          discord_id TEXT NOT NULL,
          server TEXT NOT NULL,
          scope TEXT NOT NULL,
          granted_by TEXT NOT NULL,
          granted_at REAL NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0,
          revoked_at REAL,
          UNIQUE(discord_id, server, scope)
        );
        """)
        self.db.commit()
        # Migration for a DB created before per-server scoping existed: back-fill
        # any pre-existing rows to '2b2t.org', the only server that could have
        # issued them (a fresh DB's CREATE TABLE above already has the column, so
        # this is a no-op there).
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tokens)").fetchall()}
        if "server" not in cols:
            self.db.execute("ALTER TABLE tokens ADD COLUMN server TEXT NOT NULL DEFAULT '2b2t.org'")
            self.db.commit()

    @staticmethod
    def hash_token(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def issue(self, holder_label, issued_by, scope, server, token=None):
        """Create a new registry entry, scoped to exactly one `server`. Returns
        (token_id, raw_token) — the raw token is the only time it's ever
        returned; only its hash is kept.

        `token`, if given, seeds the entry with a token the caller already has
        (e.g. a fleet bot's existing config) instead of minting a fresh one --
        used for CLI/deploy-time bootstrap, where the raw secret already lives
        somewhere else and only needs registering here. That path must be
        IDEMPOTENT: a persistent registry DB means the same --seed-token spec
        gets re-issued on every process restart, so re-seeding an already-known
        token updates its label/scope/server in place instead of hitting the
        token_hash UNIQUE constraint. A token the Owner has since revoked stays
        revoked — re-seeding never resurrects it."""
        if scope not in SCOPES:
            raise ValueError(f"bad scope {scope!r}, must be one of {sorted(SCOPES)}")
        if not holder_label:
            raise ValueError("holder_label required")
        if not server:
            raise ValueError("server required")
        reseeding_known_token = token is not None
        token = token or secrets.token_urlsafe(32)
        token_hash = self.hash_token(token)
        now = self.clock()
        with self._lock:
            if reseeding_known_token:
                existing = self.db.execute(
                    "SELECT token_id, revoked FROM tokens WHERE token_hash=?",
                    (token_hash,)).fetchone()
                if existing:
                    token_id, revoked = existing
                    if not revoked:
                        self.db.execute(
                            "UPDATE tokens SET holder_label=?, scope=?, server=? WHERE token_id=?",
                            (holder_label, scope, server, token_id))
                        self.db.commit()
                    return token_id, token
            token_id = secrets.token_hex(8)
            self.db.execute(
                "INSERT INTO tokens(token_id,token_hash,holder_label,issued_by,"
                "issued_at,scope,server,revoked) VALUES(?,?,?,?,?,?,?,0)",
                (token_id, token_hash, holder_label, issued_by, now, scope, server))
            self.db.commit()
        return token_id, token

    def revoke(self, token_id):
        """Revoke by public token_id. Returns True if a live entry was revoked."""
        now = self.clock()
        with self._lock:
            cur = self.db.execute(
                "UPDATE tokens SET revoked=1, revoked_at=? WHERE token_id=? AND revoked=0",
                (now, token_id))
            self.db.commit()
            return cur.rowcount > 0

    def scope_of(self, token, server):
        """Live scope for a presented raw token ON THIS SPECIFIC server, or None
        if absent/revoked/unknown/scoped to a different server."""
        if not token or not server:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT scope FROM tokens WHERE token_hash=? AND server=? AND revoked=0",
                (self.hash_token(token), server)).fetchone()
        return row[0] if row else None

    def has_scope(self, token, scope, server):
        return self.scope_of(token, server) == scope

    def list_active(self, server=None):
        """Never includes the raw token or its hash — token_id is the only handle.
        `server`, if given, restricts the listing — used by a per-server dashboard
        admin (not the Owner) so they only ever see their own server's tokens."""
        with self._lock:
            if server is None:
                rows = self.db.execute(
                    "SELECT token_id, holder_label, issued_by, issued_at, scope, server"
                    " FROM tokens WHERE revoked=0 ORDER BY issued_at").fetchall()
            else:
                rows = self.db.execute(
                    "SELECT token_id, holder_label, issued_by, issued_at, scope, server"
                    " FROM tokens WHERE revoked=0 AND server=? ORDER BY issued_at", (server,)).fetchall()
        return [{"tokenId": r[0], "holderLabel": r[1], "issuedBy": r[2],
                 "issuedAt": int(r[3]), "scope": r[4], "server": r[5]} for r in rows]

    def token_server(self, token_id):
        """The `server` an existing registry token belongs to, or None — lets a
        per-server admin's revoke request be authorized against their own scope
        without needing Owner-level (all-servers) visibility first."""
        with self._lock:
            row = self.db.execute("SELECT server FROM tokens WHERE token_id=?", (token_id,)).fetchone()
        return row[0] if row else None

    # ---- discord_grants: dashboard access granted directly to a Discord identity ----
    def grant_to_discord(self, discord_id, scope, server, granted_by):
        """Give a Discord identity a dashboard scope on one server — no bearer
        token involved; the session cookie IS the credential (see sessions.py).
        Idempotent like issue()'s reseed path: re-granting an already-revoked
        grant reactivates the same row instead of hitting the UNIQUE constraint."""
        if scope not in DISCORD_GRANT_SCOPES:
            raise ValueError(f"bad scope {scope!r}, must be one of {sorted(DISCORD_GRANT_SCOPES)}")
        if not discord_id:
            raise ValueError("discord_id required")
        if not server:
            raise ValueError("server required")
        now = self.clock()
        with self._lock:
            existing = self.db.execute(
                "SELECT grant_id FROM discord_grants WHERE discord_id=? AND server=? AND scope=?",
                (discord_id, server, scope)).fetchone()
            if existing:
                grant_id = existing[0]
                self.db.execute(
                    "UPDATE discord_grants SET revoked=0, revoked_at=NULL, granted_by=?, granted_at=?"
                    " WHERE grant_id=?", (granted_by, now, grant_id))
                self.db.commit()
                return grant_id
            grant_id = secrets.token_hex(8)
            self.db.execute(
                "INSERT INTO discord_grants(grant_id,discord_id,server,scope,granted_by,granted_at,revoked)"
                " VALUES(?,?,?,?,?,?,0)",
                (grant_id, discord_id, server, scope, granted_by, now))
            self.db.commit()
        return grant_id

    def revoke_discord_grant(self, grant_id):
        now = self.clock()
        with self._lock:
            cur = self.db.execute(
                "UPDATE discord_grants SET revoked=1, revoked_at=? WHERE grant_id=? AND revoked=0",
                (now, grant_id))
            self.db.commit()
            return cur.rowcount > 0

    def discord_grant_lookup(self, grant_id):
        """{'server':..., 'scope':...} for a live grant, or None — used to decide
        whether revoking it needs Owner (scope==admin) or just that server's
        admin (scope==moderator)."""
        with self._lock:
            row = self.db.execute(
                "SELECT server, scope FROM discord_grants WHERE grant_id=? AND revoked=0",
                (grant_id,)).fetchone()
        return {"server": row[0], "scope": row[1]} if row else None

    def discord_scopes(self, discord_id, server):
        """Active dashboard scopes ('moderator'/'admin') this Discord identity
        holds ON THIS SERVER, or an empty set."""
        if not discord_id or not server:
            return set()
        with self._lock:
            rows = self.db.execute(
                "SELECT scope FROM discord_grants WHERE discord_id=? AND server=? AND revoked=0",
                (discord_id, server)).fetchall()
        return {r[0] for r in rows}

    def admin_servers_for(self, discord_id):
        """Every server this Discord identity holds admin (not just moderator)
        scope on — used to scope a non-Owner admin's /registry list view."""
        if not discord_id:
            return set()
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT server FROM discord_grants WHERE discord_id=? AND scope=? AND revoked=0",
                (discord_id, SCOPE_ADMIN)).fetchall()
        return {r[0] for r in rows}

    def grants_for_discord(self, discord_id):
        """Every live {server, scope} grant this identity holds, across all
        servers — what /admin/login and /admin/session hand back so the
        dashboard knows what to show without a separate lookup per server."""
        with self._lock:
            rows = self.db.execute(
                "SELECT server, scope FROM discord_grants WHERE discord_id=? AND revoked=0",
                (discord_id,)).fetchall()
        return [{"server": r[0], "scope": r[1]} for r in rows]

    def list_discord_grants(self, server=None):
        with self._lock:
            if server is None:
                rows = self.db.execute(
                    "SELECT grant_id,discord_id,server,scope,granted_by,granted_at FROM discord_grants"
                    " WHERE revoked=0 ORDER BY granted_at").fetchall()
            else:
                rows = self.db.execute(
                    "SELECT grant_id,discord_id,server,scope,granted_by,granted_at FROM discord_grants"
                    " WHERE revoked=0 AND server=? ORDER BY granted_at", (server,)).fetchall()
        return [{"grantId": r[0], "discordId": r[1], "server": r[2], "scope": r[3],
                 "grantedBy": r[4], "grantedAt": int(r[5])} for r in rows]
