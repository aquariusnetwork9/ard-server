# Aquarius Road Department — Protocol (v1)

This document is the **frozen contract** every client and server implementation conforms to.
Its single most important property: **an off-highway coordinate is unrepresentable.** There is
no `(x, z)` field anywhere in the wire format — the only spatial data is a 1-D on-highway
coordinate. This holds even under full server-side logging: nothing beyond what's already
visible by walking the public road is ever exposed.

## 1. The narrow waist

The nether highways form a 1-dimensional manifold (rays + the near-spawn grid). A point *on* a
road is fully described by three integers:

| field  | meaning |
|--------|---------|
| `road` | index into the authoritative road table for the given `map` version |
| `seg`  | segment index within that road's polyline |
| `along`| quantized distance along that segment (`floor(distFromSegStart / BUCKET)`) |

Real `(x, z)` is **derived** from `(road, seg, along)` on the consumer side. The perpendicular
offset (how far off-road you were, and in which direction) is the *only* value that could
triangulate a base — the client computes it solely for the tolerance gate and then **discards
it before constructing any report object**. It is never transmitted, bucketed, or logged.

## 2. Constants (v1 defaults)

| name | value | note |
|------|-------|------|
| `ROAD_Y` | `120` | highways are **always** y120; the client gate is strict |
| `BUCKET` | `100` | blocks per `along` bucket |
| `TOLERANCE` | `3.0` | max off-road XZ distance to count as on-road (matches ElytraPilot's pass tolerance) |
| `NEAR_SPAWN_RADIUS` | `100000` | `max(\|x\|,\|z\|) <= this` → full grid; beyond → axis-only |
| `TTL` | `10800` (3h) | condition decay window (§6.1) — tripled from an original 1h default: highway traffic density varies a lot across the network, and a sparsely-traveled road shouldn't expire before someone else happens to pass through and refresh it |
| `LINK_CODE_TTL` | `600` (10 min) | seconds an unclaimed account-link code stays valid |
| `MAX_LINKED_UIDS` | `8` | max Minecraft UIDs one Discord identity may link (multiboxer allowance) |
| `K_TIER_C_NEW` | `4` | distinct Tier C (IP-hash) sources required to publish a *new* condition |
| `K_TIER_B_NEW` | `2` | distinct Tier B (Discord identity) sources required to publish a *new* condition |
| `K_CLEAR_FACTOR` | `2×` | CLEAR/downgrade reports require this multiple of the tier's normal `k` — see §6.4 |
| `MAINTAINER_REOPEN_WINDOW` | `3600` (1h) | a Tier A/M clear reopened within this window routes to `/moderation` — see §6.4 |
| `MAX_TRAVEL_SPEED` | `100` blocks/sec | above this implied speed between an identity's two claimed positions, the newer report doesn't corroborate anything — see §6.1.1 |
| `TRUST_BASELINE` / `TRUST_MIN` / `TRUST_MAX` | `1.0` / `0.2` / `1.0` | per-identity trust weight range — see §6.1.1 |
| `TRUST_PENALTY` / `TRUST_BOOST` | `0.15` / `0.05` | per travel-implausible report / per report that helps a condition publish — see §6.1.1 |

## 3. Road-set policy (spatially tiered)

A point may be reported only if its **on-road** point is on an *allowed* road:

- **Within 100k of spawn** (`max(|x|,|z|) <= 100000`): all categories — `axis`, `ring`,
  `diamond`, `grid`. The near-spawn grid (out to and including the 100k ring road) is
  contested public infrastructure.
- **Beyond 100k**: only `category == "axis"` (cardinals + diagonals), all the way to the
  world border. Keeps the manifold thin where real bases live — a ring/diamond/grid road
  that far out is close enough to base territory that automatic corroboration there isn't
  worth the exposure.

`planned` roads are never reportable. The policy is evaluated on the on-road point so it is
identical on the client and the server.

## 4. Geometry versioning

`map` is `sha256:<16 hex>` over the ordered road table's geometry (`category`, `radius`,
`segments`) — see `geometry.map_hash`. Clients snap against a specific `map` and send it; the
server rejects reports whose `map` doesn't match its authoritative geometry for that server.
The server also maps each `road` index to a **canonical, geometry-derived key**
(`geometry.canonical_road_key`) so aggregates survive a road-table reorder/rehash.

## 5. Report (client → `POST /report`)

```jsonc
{ "v": 1, "server": "2b2t.org", "map": "sha256:….", "road": 12, "seg": 3,
  "along": 417, "cond": "HOLE", "sev": 2, "ts": 1720624800 }
```

Strict schema: `schema/report.schema.json`. **Unknown fields are rejected** (this is what makes
a smuggled `"x"`/`"z"` impossible). `cond ∈ {CLEAR, HOLE, LAVA, OBSTRUCTION_FULL,
OBSTRUCTION_PARTIAL, COBWEB, WATER, GRAVEL, UNBUILT, PRESENCE}`. `ts` is rounded to 30s by the
client (k-anonymity / timing jitter). Never present: raw `x`/`z`, perpendicular offset,
distance, yaw/pitch/velocity, entity UUIDs, contributor identity. A `POST /report` body may be
one object or an array (batching).

`laneMin` / `laneMax` (signed integers, bounded to roughly the road's own width) are present
**only** when `cond == OBSTRUCTION_PARTIAL` — see §5.1. They describe a span *within* the
already-known road width, not a world coordinate, so they don't reopen the narrow waist.

### Client algorithm (identical across `plugin-aquarius` and `client-fabric`)

```
if dimension != NETHER: skip
if max(|x|,|z|) > maxReportRadius: skip           # per-contributor privacy cap
snap = nearest_allowed(x, z)                       # region-tiered; None if off-road/disallowed
if snap is None or snap.distance > TOLERANCE: skip # OFF-ROAD → no report object built
if roadPlaneY != 120: skip                         # strict; off-y may go to /moderation instead
# perpendicular offset is DISCARDED here
report = { road, seg = snap.seg, along = floor(distFromSegStart/BUCKET),
           cond = sampleAround(x,y,z), ts = roundTo30s(now) }
```

### 5.1 Obstruction detection: FULL vs PARTIAL, and why signs/item frames don't need a blacklist

Nether highways routinely have signs and item frames placed on them (waypoints, ads, decor)
all the way from spawn to the ends of every road. These have negligible collision — a bot
walking or elytra-bouncing through them normally never notices. **They must never be reported
as obstructions merely because they're present.** The rule: a sign/item frame only counts if
its placement actually delays the bot's forward progress by **3 or more seconds**.

Rather than maintaining a block/entity-type blacklist (version-fragile, and item frames are
entities, not blocks, so a blacklist would need two different mechanisms anyway), detection is
**purely behavioral, in two stages**. The 3-second sustained-stall requirement *is* the filter:
decorative signs/frames essentially never cause a real stall, so they're excluded by
construction, not by enumerating their block/entity IDs. On the rare occasion something does
cause a genuine ≥3s stall, whatever is physically present there is — by definition — worth
reporting, whether that's a sign wall, an item-frame array, cobwebs, or a player-built plug.

**Stage 1 — stall watchdog (mode-agnostic; works whether the bot is walking, Baritone-driving,
or elytra-bouncing).** Runs every tick while the base on-road gate (§ client algorithm above)
holds. This is a *new, independent* implementation — it does not hook `ElytraPilot`'s internal
bounce-stall timer (unexposed/private), but mirrors the same pattern:

```
bps        = hypot(x - lastX, z - lastZ) * 20          # blocks/sec this tick
peakBps    = max(peakBps * PEAK_DECAY, bps)             # decaying recent-peak speed
armed      = peakBps >= MIN_ARM_BPS                     # bot has demonstrably been moving
stalled    = armed and bps < STALL_FRACTION * peakBps   # speed collapsed relative to its own recent peak
```

Using *relative-to-recent-peak* rather than a fixed speed constant makes one detector work
across travel modes without per-mode tuning, and `armed` prevents a bot that's intentionally
parked (restocking, fighting, idling at a junction) — which never had a meaningful peak speed
to begin with — from ever starting the stall clock.

| constant | value | note |
|----------|-------|------|
| `PEAK_DECAY` | `0.995`/tick | ~7s half-life; peak "forgets" after the bot settles into new travel |
| `MIN_ARM_BPS` | `3.0` | peak must have exceeded this before a slowdown counts as a stall |
| `STALL_FRACTION` | `0.20` | current speed must drop below 20% of recent peak |
| `STALL_TICKS` | `60` (3.0s) | first report fires here, `sev = 1` |
| escalation | `120` (6s) → `sev=2`, `300` (15s) → `sev=3` | re-reports once per new severity tier only (edge-triggered, not spammed every tick) |

**Stage 2 — classification (only runs once Stage 1 confirms a real stall).** Compute the road's
perpendicular unit vector at the stalled segment (`perpX = -dz/len, perpZ = dx/len`, the same
idiom `HighwayBuilder` already uses for its build cross-section) and the road's width (parsed
from its `dim` metadata, e.g. `"6x4"` → 6; default 6 if absent, e.g. grid roads). Scan each
integer lane offset `w` from `-width/2` to `+width/2`: a lane counts as blocked if there's a
non-air, non-water block in its clear-height column, or an item-frame/glow-item-frame entity
sitting in it.

- **All lanes blocked → `OBSTRUCTION_FULL`** (no `laneMin`/`laneMax` — the whole road is out).
- **Some lanes blocked → `OBSTRUCTION_PARTIAL`**, with `laneMin`/`laneMax` = the min/max
  blocked lane offset found. A routing consumer reads this as "aim outside this span."
- **No lanes blocked at all → not an obstruction.** The stall had some other cause (a
  deliberate stop, an off-road-adjacent hazard the scan didn't reach) — nothing is reported.

The server **unions** corroborating `laneMin`/`laneMax` observations for the same
`(road, seg, along, OBSTRUCTION_PARTIAL)` key (widens the confirmed span, never narrows it) —
see §6.

## 6. Trust tiers, identity & aggregation

Trust is a property of the **verified account**, never of the software that submitted the
report. `plugin-aquarius` (whether run on the project's own fleet or a third party's own
instance) and `client-fabric` go through the exact same identity paths below — a proxy bot is
not inherently more trusted than a player just because it has to be logged into a real
Microsoft account; a player linking their own account via §6.2 gets that same guarantee.

One ARD deployment can serve **more than one anarchy server** — each `*.nether_highways.json`
geometry file in `geometry/` is its own independent road network (§1), and every grant below
(Tier A/M/moderator via §6.3, Tier B via §6.2) is scoped to exactly one `server`. Trust doesn't
transfer automatically between servers sharing a deployment; someone trusted on both just holds
two grants. The Owner secret is the one exception — it stays deployment-wide, since it's one
operator's bootstrap credential, not a per-community grant.

### 6.1 The tiers

- **Tier A (vouched):** holds a live token from the admin-issued registry (§6.3). Publishes at
  high confidence without waiting for corroboration. This is how the project's own fleet bots
  work today, and how any other person or group an admin chooses to trust works going forward —
  same mechanism, not a separate "fleet" concept.
- **Tier M (maintainer):** a *scoped* grant from the same admin-issued registry as Tier A
  (§6.3), for highway-maintenance groups who do the physical work of clearing obstructions.
  Can publish CLEAR/downgrade reports unilaterally on **any** obstruction-class condition —
  not just ones the holder raised — bypassing §6.4's corroboration and non-overlap rule the
  same way Tier A does. Deliberately narrower than Tier A otherwise: a Tier M holder's *new*
  condition reports are **not** auto-published — they're treated at Tier B strength (still
  need corroboration) unless the holder separately also carries a full (`scope: full`) Tier A
  grant. Rationale for the split: "I fixed this and it's now clear" is a claim the whole
  community fact-checks within minutes just by traveling the road, so it's safe to extend
  broadly to vetted groups; "there's a new hazard here" is much harder to fact-check quickly
  (an off-route claim can go unnoticed), so unilateral new-hazard publish rights stay gated at
  the normal tier the holder would otherwise sit at.
- **Tier B (verified):** authenticated via a Discord identity with at least one linked,
  ownership-verified Minecraft UID (§6.2). Tentative until `K_TIER_B_NEW` **distinct
  identities** corroborate. Distinct-source counting is **by Discord identity, not by UID** —
  see §6.2's dedup rule, this is load-bearing.
- **Tier C (anonymous):** no linked identity. IP-rate-limited. Tentative until `K_TIER_C_NEW`
  distinct sources corroborate, using a rotating salted hash of the source (never a stored raw
  IP), scoped per condition key so keys can't be cross-linked. This is the network's weakest,
  slowest-to-publish tier by design — it's the fallback for someone who doesn't want to link an
  identity, not the default path.
- **TTL/decay:** a condition expires after `TTL` with no refresh; confidence decays with age.
- **Reputation (§6.1.1):** a pragmatic first step toward the previously-planned "Phase 5,
  blind-token reputation" — not the full unlinkable-credential version yet (see §6.1.1), but a
  real, live weighting of each Tier B/C source's contribution to `k`-corroboration, plus a
  travel-plausibility check that excludes a report from corroborating anything at all when the
  same pseudonymous identity claims two positions further apart than physically reachable in the
  elapsed time between them.

`PRESENCE` (camper reports) is **opt-in, default off**, count-only, no identity, short TTL —
unaffected by the tiers above.

### 6.1.1 Reputation (pragmatic version — not yet the full blind-token vision)

Both travel-plausibility and per-identity trust weighting need the same new capability: telling
"this is the same pseudonymous identity" apart across *different* locations over time. That's
deliberately impossible for the ordinary per-report `source_key` hash (§6.1 above — scoped per
condition key precisely so it *can't* be cross-linked). This layer introduces a **second,
separate hash space** — `hash(salt | "identity" | server | source_key)`, recomputed live per
request exactly like every other use of `source_key` in this project, never persisted raw — used
only to look up a small per-identity record: a trust score and the identity's last claimed
`(road, seg, along)` + timestamp. This is an explicit, narrower weakening of the
"sources can't be correlated across different condition keys" property than existed before —
the tradeoff accepted for this pragmatic version instead of the full cryptographic one.

**Travel-plausibility.** On every Tier B/C report, the server re-derives the claimed position's
real `(x, z)` (already does this for the wire-format narrow waist, §1) and compares it against
that identity's last claimed position: implied speed beyond `MAX_TRAVEL_SPEED` (default 100
blocks/sec — well above any real travel mode, including elytra e-bounce) means the report is
excluded from corroboration for this condition key (same mechanism as the existing non-overlap
rule below) and applies a small trust penalty. A report with no prior claim on record (a fresh
identity, or nothing recent enough to matter) is never flagged — there's nothing to compare
against. Deliberately scoped to Tier B/C only: Tier A/M auto-publish and don't consult weight at
all, and Tier A's `source_key` is the caller's IP — this project's own fleet runs several
distinct bots behind one VPS IP, and travel-plausibility-checking Tier A would immediately
false-positive against that.

**Trust weighting.** Each identity has a trust score in `[TRUST_MIN, TRUST_MAX]` = `[0.2, 1.0]`,
starting at `1.0` (baseline) — a fresh, first-time contributor's report counts exactly as much
as it always did; this pragmatic version only ever *discounts* a demonstrated bad pattern, it
never boosts anyone above neutral. A travel-implausible report costs `TRUST_PENALTY` (0.15); a
report that helps corroborate a condition that reaches publication earns `TRUST_BOOST` (0.05)
back, capped at baseline — a penalized identity can work its way back through continued
legitimate contribution rather than a one-way ban. The floor (never below 0.2) exists so that
tanking one identity's trust can't be used to fully silence it. `k`-corroboration now sums each
source's weight instead of counting sources flatly; `distinctSources` in the public view stays
an honest plain headcount regardless.

**What this deliberately does not do, and why:** it does not retroactively reward every past
contributor when a condition crosses the publish threshold (only the specific report that helped
push it there earns anything) — doing that would require persisting an identity-to-specific-
report link, a materially bigger privacy cost than the one "last known point" this version
already accepts. It also does not implement the full blind-token/anonymous-credential scheme —
that would give the server true cryptographic inability to link a reputation credential's
issuance to its later use, at the cost of a real cryptographic protocol implementation. This
version is intentionally the smaller, reviewable step; the full version remains open for later.

### 6.2 Account linking (`POST /link/init` + `POST /link/complete`)

Device-code style, like `gh auth login`. Either producer (`plugin-aquarius` or `client-fabric`)
can initiate a link to reach Tier B:

1. The producer calls `POST /link/init { mcUid, server }` — **unauthenticated**, since no
   identity exists yet. `mcUid` is read from the producer's own authenticated Minecraft session
   (the game/proxy already proved this to Mojang/Microsoft; ARD doesn't re-verify ownership, it
   just binds *this* session's uid to a fresh code). `server` is whichever anarchy server the
   producer is actually connected to (§6.3's per-server scoping applies to Tier B the same way it
   applies to the admin registry — a link established on one server has no standing on another).
   The server generates a short, high-entropy code (matches the standard device-code-grant
   pattern — the authorization side mints the code, not the client, avoiding any client-side
   uniqueness/collision handling), records the pending `(code, mcUid, server)` with a
   `LINK_CODE_TTL` expiry, and returns the code. Rate-limited per source IP; the record is
   single-use.
2. The producer displays the code (chat/HUD for `client-fabric`; console/log for
   `plugin-aquarius`).
3. A human opens `website/link.html` (part of the Phase 3 public map site), enters/confirms the
   code, and clicks through Discord login. The page builds the OAuth authorize URL itself from
   `GET /link/config` (client id + redirect URI + scope — never the client secret, which stays
   server-side only) and carries the pending `linkCode` through the redirect via OAuth's `state`
   parameter, so no server-side session/cookie is needed for this either.
4. Whatever completed the Discord login calls `POST /link/complete { linkCode, discordCode }` —
   `discordCode` is Discord's one-time OAuth authorization code from the redirect; `server` is
   **not** re-supplied here, it's read back from the pending record `/link/init` created, so the
   browser-side completion step never needs to know or choose it. The server exchanges
   `discordCode` itself against `discord.com` (client_id/secret configured at deploy time, stdlib
   `urllib` — no extra dependency), resolves the Discord user id, matches `linkCode` to its
   pending record, links `mcUid` to that identity **on that record's server**, marks the code
   used, and mints a fresh Tier B bearer token scoped to `(mcUid, server)`. That token is what
   subsequent `/report` calls to that server authenticate with.

**Dedup rule (must not be skipped):** an identity may link up to `MAX_LINKED_UIDS` Minecraft
UIDs **per server** (covers legitimate multiboxers). All UIDs linked to the same Discord identity
on the same server count as **one** source for `k`-corroboration purposes, always — this falls
out for free rather than needing special-case logic, since the corroboration "source key" for a
Tier B report is the resolved `discord_id` itself (paired with `server`), not the token or the
UID — this rule must not be skipped, or "allow multiple UIDs" would let one identity satisfy
`K_TIER_B_NEW` entirely on its own. The same identity linking on a *second* server goes through
this whole flow again independently — it's a separate grant, not an extension of the first.

`/link/init` grants nothing by itself (a guessed/observed code is inert without completing
Discord auth), but it's still rate-limited and short-TTL as defense in depth, same posture as
every other unauthenticated write surface. A moderator can suspend a Discord identity on their
own server (`POST /identity/<server>/<discord_id>/suspend`, §6.5) — its tokens on that server then
simply fail to resolve and any report through them falls back to Tier C rather than being
rejected outright; the identity's standing on any other server is untouched.

#### 6.2.1 Bot-authenticated completion (`POST /link/bot-complete`)

A second way to complete step 4 above, for a first-party Discord bot (e.g. "Highway Bot," ARD's
community-server bot) instead of `website/link.html`. The website needs the full `discordCode`
OAuth exchange because a browser has no other way to prove who's looking at it. A Discord bot
resolving a slash command doesn't have that problem — Discord's own gateway/interaction signature
already tells the bot the invoking user's real `discord_id`, cryptographically guaranteed by
Discord itself. Making the bot *also* perform a redundant OAuth round-trip would add latency and
complexity for zero additional assurance.

So the bot instead authenticates **itself**: `POST /link/bot-complete { linkCode, discordId }`
with header `Authorization: <ARD_BOT_SECRET>` — a flat, deployment-wide, first-party credential
(same shape/hashing as the Owner secret, §6.3, but narrower: it unlocks only this one route, never
registry or admin-dashboard actions). The server verifies the bot secret, resolves which `server`
the `linkCode` was `/link/init`'d for (without consuming it), then completes the link exactly as
`/link/complete` would and returns `{ tokenId, token, server }` — the `server` field is what tells
the bot which per-community role (e.g. "2b2t Verified") to grant, no separate lookup needed. Same
rate limiting, same single-use/TTL/dedup rules as the OAuth path — this is an alternate proof of
Discord identity, not a weaker one; the trust boundary just shifts from "Discord vouches for this
code" to "our own bot process vouches for this code," which is exactly as strong since the bot
itself only ever learns a `discord_id` from Discord's own verified interaction data.

### 6.3 Admin-issued tokens (Tier A, Tier M & moderator)

One registry, not a flat set, backs all of these: each entry is `{ token (opaque, never logged),
holder_label, issued_by, issued_at, scope, server, revoked, revoked_at }`. `holder_label`
identifies the person or group the token was issued to (a fleet bot, a trusted contributor, a
highway-crew, a partner group, a moderator) so revoking one holder's access doesn't require
reasoning about a shared secret used elsewhere. Revocation takes effect on the **next request** —
the server checks the live registry, not a cached set.

**Every grant is scoped to exactly one `server`.** A deployment can serve more than one anarchy
server (e.g. `2b2t.org` and `6b6t.org` sharing the same registry/database/box) without trust
bleeding across them — a highway-crew vouched for one community carries no authority on the
other. Someone trusted on both just gets two tokens, one per server; the Owner secret itself
stays deployment-wide (§6.3 is specifically about registry *grants*, not the Owner bootstrap).

`scope` is what actually distinguishes these — same registry, same revocation mechanism,
different grants, and they're independent (a holder can be issued more than one, including on
different servers):

| `scope` | grants |
|---|---|
| `full` (Tier A) | publish + clear anything unilaterally |
| `maintainer` (Tier M) | clear/downgrade anything unilaterally; new reports fall through to normal Tier B/C corroboration |
| `moderator` | adjudicate the `/moderation` queue and suspend/reinstate Tier B/C identities (§6.5) — **not** report-publishing power |

Only an **Owner** can issue or revoke registry entries of any scope, including `moderator` —
the registry delegates day-to-day *report* adjudication, not the ability to hand out trust.
Owner is out-of-band, set up at deploy time (not itself a registry entry, to sidestep the
bootstrapping problem of the first grant needing something to grant it) — future additional
owners are an Owner-only action too, not built into v1.

### 6.4 Corroboration & asymmetric thresholds

A **new** condition needs `K_TIER_B_NEW` distinct Tier B identities or `K_TIER_C_NEW` distinct
Tier C sources (or a single Tier A report) to publish. A report that **downgrades or clears an
already-published condition** is held to a stricter bar — clearing and raising a new hazard are
deliberately asymmetric, not mirror-image operations:

- Requires `K_CLEAR_FACTOR ×` the normal threshold for that tier.
- The corroborating sources for a CLEAR must **not overlap** with the sources that created or
  corroborated the condition being cleared — a source can't both raise and resolve the same
  hazard.
- Tier A and Tier M can both clear unilaterally (see §6.1) — Tier A because it's fully vouched,
  Tier M because a fix claim is fast and cheap for the community to fact-check by just
  traveling the road.
- **Reopen accountability:** if a condition cleared by Tier A or Tier M is re-reported as still
  present within `MAINTAINER_REOPEN_WINDOW` of the clear, it's routed to `/moderation` for
  review rather than silently republished as a fresh hazard. A fast reopen is the practical
  signal that a clear was wrong — whether by honest mistake, something re-breaking almost
  immediately, or bad faith — and it's cheap to check since it doesn't block the reopen report
  itself from going out, just also creates a review record.

### 6.5 Moderator scope

A `scope: moderator` token is issued for one `server` (§6.3) and can, **on that server only**:

- List and resolve the `/moderation` queue (`approve`/`reject`) — off-y120 anomalies,
  `MAINTAINER_REOPEN_WINDOW` reopen flags (§6.4), and anything else routed there.
- Suspend or reinstate a **Tier B (Discord) identity** — its tokens stop resolving to that
  identity until reinstated, so any report through them falls back to Tier C rather than being
  rejected outright. Tier C has no persistent identity to suspend (that's the point of it being
  anonymous); a moderator's lever there is the existing per-IP rate limiting, not this route.
  This is deliberately the scope of what moderators handle day-to-day: self-serve bad actors, the
  exact burden this role exists to take off the Owner. Suspension is also per-server — the same
  Discord identity linked on two servers (§6.2) can be suspended on one without touching the
  other, matching a moderator's own server-scoped authority. A suspend also **retracts** that
  identity's corroborating contributions to every currently-active condition on that server —
  its `sources` rows are removed immediately rather than lingering until `TTL`, so any condition
  that was only published because of its corroboration is re-evaluated right away.
- **Quash** a specific published condition (`road`, `seg`, `along`, `cond`) outright, on that
  server only — removes the row and its corroborating sources immediately, logged to the
  moderation table (`kind: "quash"`) for the same audit trail a reopen flag already gets.

A `moderator` token explicitly does **not** grant: issuing or revoking any registry entry
(§6.3), or revoking a Tier A/M token — pulling a token the Owner personally vouched for is an
Owner-level decision, not a moderator one. A moderator with a concern about a Tier A/M holder
flags it for Owner review rather than acting unilaterally. (Worth reconsidering once there are
several moderators: e.g. requiring `k` moderators to agree before a suspension takes effect,
the same corroboration idea as §6.4, to bound what one rogue or mistaken moderator can do —
not needed for a first moderator or two, but noted here so it isn't forgotten later.)

### 6.6 Admin dashboard login (Discord session, no bearer token)

`website/admin/` is a browser dashboard for the moderation queue, registry tokens, dashboard
grants, and identity suspend/reinstate — everything §6.3–§6.5 previously required raw `curl` with
a bearer token for. It authenticates a human via Discord login rather than a bearer token,
resolved through a **new registry table, `discord_grants`**, separate from the bearer-token table
in §6.3: a `discord_id` (not a token) is granted `moderator` or `admin` scope directly, on exactly
one `server`. `moderator` here means the identical thing §6.5 already means — the two grant paths
converge on the same check, so a moderator is a moderator whether reached by a bot's bearer token
or a human's dashboard session. `admin` is dashboard-only and has no bearer-token equivalent: it
can issue/revoke registry tokens (§6.3) and grant/revoke `moderator` dashboard access, both scoped
to its own `server`.

**Login flow**, sharing the exact OAuth redirect URI account-linking (§6.2) already uses rather
than registering a second one with Discord: the dashboard sends the browser to Discord with
`state=admin:<nonce>` (the nonce, stashed in `sessionStorage` before the redirect, is a login-CSRF
guard — Discord's redirect back is checked against it before anything is trusted). Discord's
callback lands on `website/link.html` as always; the page recognizes the `admin:` prefix and calls
`POST /admin/login {discordCode}` instead of `/link/complete`. The server exchanges the code for a
`discord_id` (same `discord_exchange` used by §6.2), requires it to hold **at least one** live
`discord_grants` row (otherwise 403 — no dashboard access, full stop), and mints a session: a
random token, hashed before storage (identical handling to every other secret in this project),
set as an `HttpOnly; Secure; SameSite=Strict` cookie. `SameSite=Strict` alone closes the CSRF gap
for every state-changing dashboard route, since the browser won't attach the cookie to any
cross-site request at all. Sessions are short-lived (12h default) and individually or
per-identity revocable (`POST /admin/logout`, or a moderator/admin suspended some other way).

**The Owner secret still never has to touch the browser**, but it remains the *only* thing that
can create the very first `admin` grant (`--discord-admin DISCORD_ID:SERVER` / `ARD_DISCORD_ADMINS`
at deploy time, same bootstrap shape as `--seed-token`) and the only thing that can mint or revoke
*any subsequent* `admin` grant through the dashboard's own API. An admin session can act within
its own server's scope, but can never mint itself, or anyone else, more admins — new `admin`
grants always require the raw Owner token, both at bootstrap and afterward. Dashboard `moderator`
grants have no such restriction — any `admin` can grant/revoke `moderator` access on their own
server, delegating day-to-day queue work exactly as §6.5 already describes.

**Possible future step, not built yet:** a WebAuthn/FIDO2 passkey (platform authenticator —
Windows Hello / Touch ID / Android biometric) bound to the admin's `discord_id` at enrollment,
as a second factor alongside Discord OAuth on every login. Would need a from-scratch pure-stdlib
COSE/CBOR + ECDSA (P-256) implementation to keep this project's no-pip posture — real
cryptographic surface area worth its own careful pass rather than folding into this change.

## 7. Consumption

| route | method | auth | purpose |
|-------|--------|------|---------|
| `/health` | GET | none | liveness only, no data |
| `/geometry/<server>` | GET | **none (public, rate-limited)** | authoritative road table + `map` + `BUCKET` |
| `/report` | POST | Tier A / Tier M (clear only) / Tier B / anon (Tier C) | ingest reports — see §6 |
| `/link/init` | POST | none (rate-limited) | body `{mcUid, server}` — server mints a pending link code — §6.2 |
| `/link/complete` | POST | none (holds a Discord `discordCode` instead) | resolves a link code + Discord OAuth code into a Tier B token scoped to the `/link/init` record's server — §6.2 |
| `/link/bot-complete` | POST | `ARD_BOT_SECRET` (first-party bot credential) | resolves a link code + an already-Discord-verified `discordId` into a Tier B token; response includes `server` — §6.2.1 |
| `/link/config` | GET | none (public, rate-limited) | Discord `clientId`/`redirectUri`/`authorizeUrl` for the website's link page to build the OAuth URL — never the client secret |
| `/conditions/<server>` | GET | **none (public, rate-limited)** | published, non-expired conditions (`?road=&from=&to=`) |
| `/conditions/<server>/stream` | GET | **none (public, rate-limited)** | live SSE of updates |
| `/moderation` | POST | **none (public, own rate limit — separate from the read limit)** | submit an anomaly for approval (`schema/moderation.schema.json`) |
| `/moderation/<server>` | GET | `moderator` (or `admin`) scope **for that server**, token or dashboard session | list pending anomalies — §6.3 |
| `/moderation/<id>/<approve\|reject>` | POST | `moderator`/`admin` scope for the entry's own server, token or session | resolve an anomaly — §6.5 |
| `/moderation/quash` | POST | `moderator`/`admin` scope **for that server**, token or session | body `{server, road, seg, along, cond}` — removes a specific published condition outright — §6.5 |
| `/identity/<server>/<discord_id>/suspend` | POST | `moderator`/`admin` scope **for that server**, token or session | suspend a Tier B (Discord) identity on that server only — §6.5 |
| `/identity/<server>/<discord_id>/reinstate` | POST | `moderator`/`admin` scope **for that server**, token or session | reinstate a suspended Tier B identity on that server only — §6.5 |
| `/registry` | POST/GET/DELETE | Owner (all servers) or a dashboard `admin` session (own server(s) only) | issue/list/revoke registry tokens of any scope; issuing requires `server` in the body — §6.3/§6.6 |
| `/admin/login` | POST | none (holds a Discord `discordCode` instead) | exchanges a Discord code for a session cookie, if that identity holds any `discord_grants` — §6.6 |
| `/admin/logout` | POST | dashboard session | revokes the presented session — §6.6 |
| `/admin/session` | GET | dashboard session | `{discordId, grants}` for the current session, or 401 — §6.6 |
| `/admin/grants` | POST/GET | `admin` scope for the target server (Owner, or a dashboard `admin` session) | grant/list `moderator` dashboard access; granting `admin` scope itself requires the raw Owner token — §6.6 |
| `/admin/grants/<id>` | DELETE | `admin` scope for the grant's server; revoking an `admin`-scope grant requires the raw Owner token | revoke a dashboard grant — §6.6 |
| `/` and other static paths | GET | **none (public, rate-limited)** | the map website itself (Phase 3), including `/admin/` — plain files, no templating |

**Read policy locked in 2026-07-19: fully public, no token.** `/geometry`, `/conditions` (incl.
`/stream`) and the website's static files carry no auth check at all — this is the "Google-Maps-
style consumption" goal from the start, not a fallback. *Writes* stay exactly as tiered as §6
describes; only the read side opened up. The one adjacent route that changes shape as a result:
`/moderation` submission used to inherit its auth check from the (formerly token-gated) read gate
— now that reads carry no token, it has its **own** explicit per-IP rate limit instead, so opening
reads doesn't silently open the moderation queue to unlimited spam as a side effect.

Since `/conditions` is a fully public route, put a CDN/reverse proxy in front of it in
production — edge caching absorbs the bulk of read traffic, so the stdlib `http.server` process
itself only needs to size for the write-side tiers, not serve as the public-facing edge on its
own.

The human-facing map website (Phase 3, `website/`) is just another consumer of this table: it
fetches `/geometry` once to draw the road skeleton and `/conditions` (+ `/stream` as a live-update
nudge) to plot hazard markers, doing its own `(road,seg,along) → (x,z)` placement from data the
server already re-derives — no new wire concepts, no session, no login for viewing.
