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
DEFAULT_MIN_DISCORD_AGE = 0          # seconds; 0 = no minimum (PROTOCOL.md MIN_DISCORD_ACCOUNT_AGE)

# Discord's snowflake epoch (2015-01-01T00:00:00Z, ms). A snowflake's high bits are
# its creation timestamp, so an account's age is derivable from its id alone -- no
# API call, no extra scope.
_DISCORD_EPOCH_MS = 1420070400000


def discord_account_age_seconds(discord_id, now):
    """Age of a Discord account in seconds, read straight from its snowflake id.
    None if the id isn't a parseable snowflake (in which case callers skip the
    age check rather than guessing)."""
    try:
        created_ms = (int(discord_id) >> 22) + _DISCORD_EPOCH_MS
    except (ValueError, TypeError):
        return None
    return now - created_ms / 1000.0


def _format_code(raw_hex):
    # Human-typeable, device-code style: "A1B2-C3D4".
    raw_hex = raw_hex.upper()
    return f"{raw_hex[:4]}-{raw_hex[4:]}"


class LinkStore:
    def __init__(self, db_path=":memory:", link_code_ttl=DEFAULT_LINK_CODE_TTL,
                 max_linked_uids=DEFAULT_MAX_LINKED_UIDS, clock=time.time,
                 min_discord_age=DEFAULT_MIN_DISCORD_AGE,
                 require_ownership_proof=True):
        self.link_code_ttl = link_code_ttl
        self.max_linked_uids = max_linked_uids
        self.min_discord_age = min_discord_age
        # The "real fix" for A4 (PROTOCOL.md §6.1): a self-claimed mc_uid is no
        # longer enough on its own -- complete_link also requires verify_ownership
        # to have passed first. Only ever False in tests/local dev without live
        # Mojang connectivity.
        self.require_ownership_proof = require_ownership_proof
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS pending_links(
          code TEXT PRIMARY KEY, mc_uid TEXT NOT NULL, server TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
          verify_server_id TEXT, verified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS identities(
          discord_id TEXT NOT NULL, server TEXT NOT NULL, created_at REAL NOT NULL,
          suspended INTEGER NOT NULL DEFAULT 0, suspended_at REAL,
          credit_opt_in INTEGER NOT NULL DEFAULT 0, discord_username TEXT,
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
        if "verify_server_id" not in cols:
            self.db.execute("ALTER TABLE pending_links ADD COLUMN verify_server_id TEXT")
            self.db.execute("ALTER TABLE pending_links ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
            self.db.commit()

        cols = {r[1] for r in self.db.execute("PRAGMA table_info(identities)").fetchall()}
        if "server" not in cols:
            self.db.executescript("""
            ALTER TABLE identities RENAME TO identities_old;
            CREATE TABLE identities(
              discord_id TEXT NOT NULL, server TEXT NOT NULL, created_at REAL NOT NULL,
              suspended INTEGER NOT NULL DEFAULT 0, suspended_at REAL,
              credit_opt_in INTEGER NOT NULL DEFAULT 0, discord_username TEXT,
              PRIMARY KEY(discord_id, server)
            );
            INSERT INTO identities(discord_id, server, created_at, suspended, suspended_at)
              SELECT discord_id, '2b2t.org', created_at, suspended, suspended_at FROM identities_old;
            DROP TABLE identities_old;
            """)
            self.db.commit()
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(identities)").fetchall()}
        if "credit_opt_in" not in cols:
            self.db.execute("ALTER TABLE identities ADD COLUMN credit_opt_in INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(identities)").fetchall()}
        if "discord_username" not in cols:
            # Backfilled NULL for anyone who linked before this existed -- their
            # display name is simply unknown until they touch a flow that re-resolves
            # it (any future /link/complete or /link/bot-complete for that identity).
            self.db.execute("ALTER TABLE identities ADD COLUMN discord_username TEXT")
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
        # A random nonce the producer uses for its own Mojang session/minecraft/join
        # call (see verify_ownership below) -- separate from `code`, which is the
        # human-facing device code, so the two never leak into each other's flow.
        verify_server_id = secrets.token_hex(16)
        now = self.clock()
        with self._lock:
            self.db.execute(
                "INSERT INTO pending_links(code,mc_uid,server,created_at,used,verify_server_id)"
                " VALUES(?,?,?,?,0,?)",
                (code, mc_uid, server, now, verify_server_id))
            self.db.commit()
        return code

    def verify_server_id_for(self, code):
        """The Mojang join-proof nonce for a still-pending code, or None if the
        code is unknown/already used. Read-only, doesn't touch `verified` --
        lets /link/init's response include it without changing init_link's
        existing (widely tested) return shape."""
        with self._lock:
            row = self.db.execute(
                "SELECT verify_server_id FROM pending_links WHERE code=? AND used=0", (code,)).fetchone()
        return row[0] if row else None

    # ---- step 1.5: producer proves it holds a live Mojang session for mc_uid ----
    def verify_ownership(self, code, mojang_verify):
        """Consumes nothing (the code is still needed for /link/complete) -- just
        marks the pending link ownership-verified once `mojang_verify(mc_uid,
        verify_server_id)` confirms the producer's own prior `session/minecraft/join`
        call really was made by that account. `mojang_verify` is injectable so tests
        never hit the real Mojang API (same pattern as discord_verify elsewhere in
        this project). Raises ValueError on an unknown/used/expired code or a failed
        proof."""
        now = self.clock()
        with self._lock:
            row = self.db.execute(
                "SELECT mc_uid, created_at, used, verify_server_id FROM pending_links WHERE code=?",
                (code,)).fetchone()
            if row is None:
                raise ValueError("unknown link code")
            mc_uid, created_at, used, verify_server_id = row
            if used:
                raise ValueError("link code already used")
            if now - created_at > self.link_code_ttl:
                raise ValueError("link code expired")
            if not mojang_verify(mc_uid, verify_server_id):
                raise ValueError("Mojang ownership proof failed -- the account did not "
                                  "join with the expected server id")
            self.db.execute("UPDATE pending_links SET verified=1 WHERE code=?", (code,))
            self.db.commit()

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
    def complete_link(self, code, discord_id, discord_username=None):
        """Consumes the pending code and mints a fresh Tier B bearer token for its
        mc_uid, linked to discord_id, ON THE SERVER RECORDED AT /link/init TIME
        (not re-supplied here -- the browser-side completion step never needs to
        know or choose it). Returns (token_id, token). Raises ValueError on an
        unknown/expired/already-used code, a not-yet-ownership-verified code (when
        require_ownership_proof is on), a suspended identity, or hitting
        max_linked_uids for that server.

        discord_username, when the caller has one on hand (a real OAuth exchange
        always does; the bot-mediated path only if the bot passes one along), is
        stored purely for admin-dashboard display -- it's never a security check,
        so a stale or missing name never blocks a link. Always overwritten with
        the freshest value seen, since a Discord username can change."""
        now = self.clock()
        with self._lock:
            row = self.db.execute(
                "SELECT mc_uid, server, created_at, used, verified FROM pending_links WHERE code=?",
                (code,)).fetchone()
            if row is None:
                raise ValueError("unknown link code")
            mc_uid, server, created_at, used, verified = row
            if used:
                raise ValueError("link code already used")
            if now - created_at > self.link_code_ttl:
                raise ValueError("link code expired")
            if self.require_ownership_proof and not verified:
                raise ValueError("Mojang ownership not verified yet -- call "
                                  "/link/verify-ownership first")

            if self.min_discord_age > 0:
                # Age comes from the snowflake itself; an id that doesn't parse as
                # one skips the check (only ever seen from test fakes -- real
                # Discord ids are always snowflakes) rather than hard-failing.
                age = discord_account_age_seconds(discord_id, now)
                if age is not None and age < self.min_discord_age:
                    days = self.min_discord_age // 86400
                    raise ValueError(
                        f"this Discord account is too new to link here "
                        f"(minimum account age: {days} days)")

            self.db.execute(
                "INSERT INTO identities(discord_id,server,created_at) VALUES(?,?,?)"
                " ON CONFLICT(discord_id,server) DO NOTHING", (discord_id, server, now))
            if discord_username:
                self.db.execute(
                    "UPDATE identities SET discord_username=? WHERE discord_id=? AND server=?",
                    (discord_username, discord_id, server))
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

    def mc_uid_for(self, token, server):
        """The specific mc_uid a live Tier B token is linked to on this server, or
        None. A given token is minted for exactly one (mc_uid, server) pair, unlike
        discord_identity_for's identity-level resolution -- used for per-account
        checks (e.g. presence verification) that discord_id alone can't answer,
        since one identity may have several linked UIDs."""
        if not token or not server:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT lu.mc_uid FROM linked_uids lu JOIN identities i"
                " ON i.discord_id = lu.discord_id AND i.server = lu.server"
                " WHERE lu.token_hash=? AND lu.server=? AND lu.revoked=0 AND i.suspended=0",
                (self.hash_token(token), server)).fetchone()
        return row[0] if row else None

    # ---- credit opt-in (Survey leaderboard, SS6.7) ----
    def set_credit_opt_in(self, discord_id, server, value):
        """Flips whether confirmed Tier B reports from this identity get a
        permanent, real discord_id credit record (see Store.credits in
        highway_conditions.py). Off by default -- only ever true after an
        explicit /credit on. Requires the identity to already exist (i.e. this
        discord_id has completed at least one /link on this server); returns
        False if it hasn't rather than silently creating a bare row."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE identities SET credit_opt_in=? WHERE discord_id=? AND server=?",
                (1 if value else 0, discord_id, server))
            self.db.commit()
            return cur.rowcount > 0

    def credit_opt_in_for(self, discord_id, server):
        with self._lock:
            row = self.db.execute(
                "SELECT credit_opt_in FROM identities WHERE discord_id=? AND server=?",
                (discord_id, server)).fetchone()
        return bool(row and row[0])

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

    def set_discord_username(self, discord_id, server, username):
        """Best-effort display-name backfill for an identity that already exists,
        independent of any link event -- used by the bot's one-off migration
        script to resolve names for accounts that linked before username capture
        existed (complete_link only ever refreshes the name of the identity IT'S
        currently linking, so it can't reach these). Never creates a row and never
        writes an empty name; returns whether a matching identity was found."""
        if not username:
            return False
        with self._lock:
            cur = self.db.execute(
                "UPDATE identities SET discord_username=? WHERE discord_id=? AND server=?",
                (username, discord_id, server))
            self.db.commit()
            return cur.rowcount > 0

    # ---- admin dashboard: the roster itself (SS6.2) ----
    def list_identities(self, server):
        """Every linked Discord identity on this server -- discord_id, best-known
        display name, every currently-linked (non-revoked) mc_uid, link date, and
        suspended/credit-opt-in state. Until now nothing could answer "who's
        linked" as a set -- every other read here (discord_identity_for, mc_uid_for,
        credit_opt_in_for) resolves exactly one known id/token; this is the one
        list view, so it's the only place the admin dashboard can build a roster
        panel from."""
        with self._lock:
            identities = self.db.execute(
                "SELECT discord_id, discord_username, created_at, suspended, credit_opt_in"
                " FROM identities WHERE server=? ORDER BY created_at DESC", (server,)).fetchall()
            uid_rows = self.db.execute(
                "SELECT discord_id, mc_uid FROM linked_uids WHERE server=? AND revoked=0",
                (server,)).fetchall()
        uids_by_identity = {}
        for discord_id, mc_uid in uid_rows:
            uids_by_identity.setdefault(discord_id, []).append(mc_uid)
        return [
            {
                "discordId": discord_id,
                "discordUsername": discord_username,
                "linkedUids": uids_by_identity.get(discord_id, []),
                "linkedAt": created_at,
                "suspended": bool(suspended),
                "creditOptIn": bool(credit_opt_in),
            }
            for discord_id, discord_username, created_at, suspended, credit_opt_in in identities
        ]


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

    Returns (discord_id, display_name). display_name prefers the modern "global_name"
    (the display name Discord shows everywhere post-username-migration) and falls
    back to the classic "username" field if a user has never set one -- it's a
    display convenience only, never used for corroboration or dedup (discord_id
    alone is the identity), so an unusual/missing value here can never weaken Tier
    B's guarantees.

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
    display_name = user_resp.get("global_name") or user_resp.get("username")
    return discord_id, display_name


class MojangVerifyError(Exception):
    pass


def mojang_verify_join(mc_uid, server_id, timeout=10):
    """Real Mojang session-server ownership proof, pure stdlib. The producer
    (plugin-aquarius/client-fabric) makes its OWN `session/minecraft/join` call
    directly to Mojang using its live session's access token, `selectedProfile`
    set to mc_uid, and `serverId` set to this pending link's verify_server_id --
    that call never touches ARD at all, it's between the producer and Mojang. This
    function is the other half: given only mc_uid and that same serverId, ask
    Mojang's hasJoined whether such a join really just happened.

    hasJoined is keyed by USERNAME, not uuid (a legacy Yggdrasil quirk), so this
    first resolves mc_uid's current username via the session-server profile
    endpoint, then calls hasJoined with that username + server_id. Returns True
    only if Mojang's response profile id matches mc_uid (belt-and-suspenders --
    should always hold, since the username was itself resolved from mc_uid a
    moment earlier). False (not True) on ANY negative signal, network error, or
    malformed response -- this function's job is a strict yes/no, never a crash
    that could look like a bypass."""
    try:
        profile_req = urllib.request.Request(
            f"https://sessionserver.mojang.com/session/minecraft/profile/{mc_uid}",
            headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(profile_req, timeout=timeout) as resp:
            profile = json.loads(resp.read())
        username = profile.get("name")
        if not username:
            return False

        params = urllib.parse.urlencode({"username": username, "serverId": server_id})
        has_joined_req = urllib.request.Request(
            f"https://sessionserver.mojang.com/session/minecraft/hasJoined?{params}",
            headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(has_joined_req, timeout=timeout) as resp:
            if resp.status == 204:  # no body -- Mojang's "did not join" response
                return False
            joined = json.loads(resp.read())
        return _normalize_uuid(joined.get("id")) == _normalize_uuid(mc_uid)
    except (urllib.error.URLError, ValueError, TypeError):
        return False


def _normalize_uuid(u):
    return (u or "").replace("-", "").lower()
