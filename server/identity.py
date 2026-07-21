"""
Tier B account linking -- PROTOCOL.md SS6.2: Discord identity + verified Minecraft
UID(s), device-code style (like `gh auth login`).

  1. A producer (plugin-aquarius or client-fabric) generates a short code locally
     from its own authenticated Minecraft session and calls LinkStore.init_link(),
     which also records which `server` the producer is connected to.
  2. A human logs into Discord (real OAuth against discord.com -- see
     discord_exchange) and enters the code; the server resolves it via
     LinkStore.complete_link(), which mints a new Tier B bearer token for that UID
     ON THAT SAME SERVER (read back from the pending record, not re-supplied).
  3. Future /report calls authenticate with that token. LinkStore.discord_identity_for
     resolves it back to a discord_id -- the corroboration "source" for Tier B, NOT
     the mc_uid, so every UID linked to the same identity ON THE SAME SERVER counts
     as one source (SS6.2's dedup rule -- load-bearing, not optional).

Every link, identity-suspension, and corroboration weight is scoped to exactly
one `server` -- the same person verified on both 2b2t.org and 6b6t.org links (and
is suspendable) independently per server, mirroring trust.py's registry scoping.
Someone linked on both goes through the device-code flow once per server.

Pure stdlib, SQLite-backed, same style as Store/Registry.
"""

import hashlib
import secrets
import sqlite3
import threading
import time
import json
import urllib.request
import urllib.parse
import urllib.error

DEFAULT_LINK_CODE_TTL = 600          # PROTOCOL.md LINK_CODE_TTL
DEFAULT_MAX_LINKED_UIDS = 8          # PROTOCOL.md MAX_LINKED_UIDS


def _format_code(raw_hex):
    # Human-typeable, device-code style: "A1B2-C3D4".
    raw_hex = raw_hex.upper()
    return f"{raw_hex[:4]}-{raw_hex[4:]}"


class LinkStore:
    def __init__(self, db_path=":memory:", link_code_ttl=DEFAULT_LINK_CODE_TTL,
                 max_linked_uids=DEFAULT_MAX_LINKED_UIDS, clock=time.time):
        self.link_code_ttl = link_code_ttl
        self.max_linked_uids = max_linked_uids
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pending_links(
          code TEXT PRIMARY KEY, mc_uid TEXT NOT NULL, server TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS identities(
          discord_id TEXT NOT NULL, server TEXT NOT NULL, created_at REAL NOT NULL,
          suspended INTEGER NOT NULL DEFAULT 0, suspended_at REAL,
          PRIMARY KEY(discord_id, server)
        );
        CREATE TABLE IF NOT EXISTS linked_uids(
          mc_uid TEXT NOT NULL, server TEXT NOT NULL, discord_id TEXT NOT NULL,
          token_id TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
          linked_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, revoked_at REAL,
          PRIMARY KEY(mc_uid, server)
        );
        """)
        self.db.commit()
        self._migrate_to_server_scoped()

    def _migrate_to_server_scoped(self):
        # A DB created before per-server scoping existed has `identities`/
        # `linked_uids` with a bare-discord_id / bare-mc_uid primary key -- that
        # can't be widened with ALTER TABLE, so rebuild those two tables,
        # back-filling existing rows to '2b2t.org' (the only server that could
        # have created them). `pending_links` only needs a column added. No-ops
        # entirely on a fresh DB (the CREATE TABLE above already has the final
        # shape, so none of the old-shaped tables are found).
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pending_links)").fetchall()}
        if "server" not in cols:
            self.db.execute("ALTER TABLE pending_links ADD COLUMN server TEXT NOT NULL DEFAULT '2b2t.org'")
            self.db.commit()

        cols = {r[1] for r in self.db.execute("PRAGMA table_info(identities)").fetchall()}
        if "server" not in cols:
            self.db.executescript("""
            ALTER TABLE identities RENAME TO identities_old;
            CREATE TABLE identities(
              discord_id TEXT NOT NULL, server TEXT NOT NULL, created_at REAL NOT NULL,
              suspended INTEGER NOT NULL DEFAULT 0, suspended_at REAL,
              PRIMARY KEY(discord_id, server)
            );
            INSERT INTO identities(discord_id, server, created_at, suspended, suspended_at)
              SELECT discord_id, '2b2t.org', created_at, suspended, suspended_at FROM identities_old;
            DROP TABLE identities_old;
            """)
            self.db.commit()

        cols = {r[1] for r in self.db.execute("PRAGMA table_info(linked_uids)").fetchall()}
        if "server" not in cols:
            self.db.executescript("""
            ALTER TABLE linked_uids RENAME TO linked_uids_old;
            CREATE TABLE linked_uids(
              mc_uid TEXT NOT NULL, server TEXT NOT NULL, discord_id TEXT NOT NULL,
              token_id TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
              linked_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, revoked_at REAL,
              PRIMARY KEY(mc_uid, server)
            );
            INSERT INTO linked_uids(mc_uid, server, discord_id, token_id, token_hash,
                                     linked_at, revoked, revoked_at)
              SELECT mc_uid, '2b2t.org', discord_id, token_id, token_hash,
                     linked_at, revoked, revoked_at FROM linked_uids_old;
            DROP TABLE linked_uids_old;
            """)
            self.db.commit()

    @staticmethod
    def hash_token(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    # ---- step 1: producer proposes a pending link (POST /link/init) ----
    def init_link(self, mc_uid, server):
        if not mc_uid:
            raise ValueError("mc_uid required")
        if not server:
            raise ValueError("server required")
        code = _format_code(secrets.token_hex(4))
        now = self.clock()
        with self._lock:
            self.db.execute(
                "INSERT INTO pending_links(code,mc_uid,server,created_at,used) VALUES(?,?,?,?,0)",
                (code, mc_uid, server, now))
            self.db.commit()
        return code

    # ---- read-only peek: which server a pending code was init'd for ----
    def peek_pending_server(self, code):
        """Returns the `server` a still-valid (unused, unexpired) pending code was
        created for, or None if the code is unknown/used/expired. Does not consume
        the code or touch linked_uids -- a caller that also wants to complete the
        link should still call complete_link() afterward; this exists so a caller
        can learn which server a code belongs to WITHOUT resolving a discord_id
        first (e.g. to pick which server-specific role to grant on success)."""
        now = self.clock()
        with self._lock:
            row = self.db.execute(
                "SELECT server, created_at, used FROM pending_links WHERE code=?",
                (code,)).fetchone()
        if row is None:
            return None
        server, created_at, used = row
        if used or now - created_at > self.link_code_ttl:
            return None
        return server

    # ---- step 2: Discord-authenticated website/session resolves the code ----
    def complete_link(self, code, discord_id):
        """Consumes the pending code and mints a fresh Tier B bearer token for its
        mc_uid, linked to discord_id, ON THE SERVER RECORDED AT /link/init TIME
        (not re-supplied here -- the browser-side completion step never needs to
        know or choose it). Returns (token_id, token). Raises ValueError on an
        unknown/expired/already-used code, a suspended identity, or hitting
        max_linked_uids for that server."""
        now = self.clock()
        with self._lock:
            row = self.db.execute(
                "SELECT mc_uid, server, created_at, used FROM pending_links WHERE code=?",
                (code,)).fetchone()
            if row is None:
                raise ValueError("unknown link code")
            mc_uid, server, created_at, used = row
            if used:
                raise ValueError("link code already used")
            if now - created_at > self.link_code_ttl:
                raise ValueError("link code expired")

            self.db.execute(
                "INSERT INTO identities(discord_id,server,created_at) VALUES(?,?,?)"
                " ON CONFLICT(discord_id,server) DO NOTHING", (discord_id, server, now))
            suspended = self.db.execute(
                "SELECT suspended FROM identities WHERE discord_id=? AND server=?",
                (discord_id, server)).fetchone()[0]
            if suspended:
                raise ValueError("this Discord identity is suspended on this server")

            active = self.db.execute(
                "SELECT COUNT(*) FROM linked_uids WHERE discord_id=? AND server=? AND revoked=0",
                (discord_id, server)).fetchone()[0]
            already_this_identity = self.db.execute(
                "SELECT 1 FROM linked_uids WHERE mc_uid=? AND server=? AND discord_id=? AND revoked=0",
                (mc_uid, server, discord_id)).fetchone()
            if active >= self.max_linked_uids and not already_this_identity:
                raise ValueError(f"identity already has {self.max_linked_uids} linked UIDs on "
                                  f"{server} (the max)")

            # (mc_uid, server) is the primary key -- re-linking (same identity,
            # fresh token, or a different identity entirely, e.g. someone lost
            # access to their old Discord account) just overwrites the one row for
            # that (uid, server) pair rather than keeping revoked history around.
            # The mc_uid itself was already proven (it came from a live
            # authenticated MC session at /link/init time), so re-keying doesn't
            # weaken that guarantee.
            token_id = secrets.token_hex(8)
            token = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO linked_uids(mc_uid,server,discord_id,token_id,token_hash,linked_at,revoked)"
                " VALUES(?,?,?,?,?,?,0)"
                " ON CONFLICT(mc_uid,server) DO UPDATE SET"
                " discord_id=excluded.discord_id, token_id=excluded.token_id,"
                " token_hash=excluded.token_hash, linked_at=excluded.linked_at, revoked=0, revoked_at=NULL",
                (mc_uid, server, discord_id, token_id, self.hash_token(token), now))
            self.db.execute("UPDATE pending_links SET used=1 WHERE code=?", (code,))
            self.db.commit()
        return token_id, token

    # ---- step 3: resolve a presented Tier B token at report-ingest time ----
    def discord_identity_for(self, token, server):
        """The discord_id behind a live (non-revoked, non-suspended-identity) Tier B
        token ON THIS SERVER, or None. This -- not the mc_uid or the token -- is
        the corroboration "source" for Tier B, so every UID linked to one identity
        ON THE SAME SERVER counts once."""
        if not token or not server:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT lu.discord_id FROM linked_uids lu JOIN identities i"
                " ON i.discord_id = lu.discord_id AND i.server = lu.server"
                " WHERE lu.token_hash=? AND lu.server=? AND lu.revoked=0 AND i.suspended=0",
                (self.hash_token(token), server)).fetchone()
        return row[0] if row else None

    # ---- moderator actions (SS6.5) ----
    def suspend(self, discord_id, server):
        now = self.clock()
        with self._lock:
            cur = self.db.execute(
                "UPDATE identities SET suspended=1, suspended_at=?"
                " WHERE discord_id=? AND server=? AND suspended=0",
                (now, discord_id, server))
            self.db.commit()
            return cur.rowcount > 0

    def reinstate(self, discord_id, server):
        with self._lock:
            cur = self.db.execute(
                "UPDATE identities SET suspended=0, suspended_at=NULL"
                " WHERE discord_id=? AND server=? AND suspended=1",
                (discord_id, server))
            self.db.commit()
            return cur.rowcount > 0


# --------------------------------------------------------------------------- Discord OAuth

class DiscordOAuthError(Exception):
    pass


# Discord's own API docs (developers.discord.com/docs/reference#user-agent) say
# requests with a default HTTP-tool signature -- "Python-urllib", "node-fetch",
# bare "curl", etc, i.e. exactly what urllib.request sends with no headers set --
# get blocked outright (Cloudflare error 1010) rather than just discouraged. A
# real production symptom of this omission: /link and /admin login both failed
# live with "token exchange failed: 403 ... error code: 1010" until this was set.
_USER_AGENT = "AquariusRoadDepartment (https://github.com/aquariusnetwork9/Aquarius-Road-Department)"


def discord_exchange(client_id, client_secret, redirect_uri, discord_code, timeout=10):
    """Real Discord OAuth2 authorization-code exchange, pure stdlib (urllib). Trades
    a one-time `discord_code` (from Discord's redirect after the user logs in) for
    that user's Discord ID via /oauth2/token then /users/@me. No dependency beyond
    the stdlib, matching the rest of this service.

    Until Phase 3 (the dedicated map website) exists, the ingest server itself is
    the thing calling this -- a minimal static "log in with Discord" link pointing
    here, plus this exchange, is enough for /link/complete to work without a
    separate website stack.
    """
    token_body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "grant_type": "authorization_code", "code": discord_code,
        "redirect_uri": redirect_uri,
    }).encode()
    token_req = urllib.request.Request(
        "https://discord.com/api/oauth2/token", data=token_body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(token_req, timeout=timeout) as resp:
            token_resp = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise DiscordOAuthError(f"token exchange failed: {e.code} {e.read()[:200]}") from e
    access_token = token_resp.get("access_token")
    if not access_token:
        raise DiscordOAuthError("no access_token in Discord's response")

    user_req = urllib.request.Request(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(user_req, timeout=timeout) as resp:
            user_resp = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise DiscordOAuthError(f"fetching user profile failed: {e.code} {e.read()[:200]}") from e
    discord_id = user_resp.get("id")
    if not discord_id:
        raise DiscordOAuthError("no id in Discord's user profile response")
    return discord_id
