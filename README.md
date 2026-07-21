# ard-server (private)

The actual Aquarius Road Department ingest/moderation/trust service. This repo is
**intentionally private** — unlike the producers (`plugin-aquarius`, `plugin-zenith`,
`client-fabric`) which run on end-user machines and are public in
[`Aquarius-Road-Department`](https://github.com/aquariusnetwork9/Aquarius-Road-Department), this
service only ever runs on infrastructure we control. It never needed to be public, so it isn't.

`PROTOCOL.md` here is the **full internal spec** — every trust-tier threshold, the reputation
weighting, moderator/admin scope, all of it. The public repo's `PROTOCOL.md` is a trimmed subset:
just enough wire-format/interop detail for a third-party producer to build a compliant client. If
the two ever disagree, this one is the source of truth for how the live service actually behaves.

## Layout

```
server/
  geometry.py            narrow-waist math (snap/quantize/re-derive/region policy) — also
                          mirrored in the public repo's protocol/ dir; identical file, kept in
                          both places since the service needs it locally and producers need it
                          as their client-algorithm reference
  reference_client.py     canonical client gate — same duplication rationale as above
  highway_conditions.py   ingest/aggregation/reputation/moderation service (private)
  trust.py                Owner-issued token registry — Tier A/M/moderator scopes (private)
  identity.py             Tier B Discord account linking + reputation identity (private)
  sessions.py             admin dashboard session cookies (private)

tests/                    full suite incl. everything that exercises the private modules above
tools/hc_mock.py          off-road-leak audit / demo harness (spins up the real service in-process)
schema/, geometry/        wire schema + road-network geometry — mirrors the public repo's copies
website/                  the public map + admin dashboard static files, served by the service
                          (mirrors the public repo's website/ — it's browser-delivered either way,
                          so keeping a copy here isn't a privacy concession, just what's needed
                          to actually serve it)
```

## Run

```bash
python -m unittest discover -s tests -t .

python server/highway_conditions.py --geometry ./geometry --port 8788 \
    --owner-token <OWNER_SECRET> --seed-token <FLEET_BOT_SECRET>:full:2b2t.org:fleet-bot-1
```

See `PROTOCOL.md §6`/`§7` for the full route table, env vars (`ARD_OWNER_TOKEN`,
`ARD_SEED_TOKENS`, `ARD_DISCORD_CLIENT_SECRET`, `ARD_DISCORD_ADMINS`, `ARD_BOT_SECRET`,
`ARD_TRUSTED_PROXIES`, `ARD_NTFY_URL`/`ARD_NTFY_TOKEN` for ops alerts, `ARD_MIN_DISCORD_AGE_DAYS`,
`ARD_REQUIRE_OWNERSHIP_PROOF`/`ARD_PRESENCE_CHECK` — both default true, set to `0`/`false` to
disable either without touching the systemd unit, `ARD_IDENTITY_SALT`), and deploy notes (secrets
via environment, never CLI flags, on a shared box).

**`ARD_REQUIRE_OWNERSHIP_PROOF=0`/`ARD_PRESENCE_CHECK=0`** exist specifically so an operator can
turn either off from the env file alone — e.g. disable the Mojang ownership-proof gate until the
producer plugins (`plugin-aquarius`/`plugin-zenith`/`client-fabric`, all in the public repo) carry
the client-side `session/minecraft/join` call it depends on, without editing `ExecStart=`.

**`ARD_IDENTITY_SALT`** must be set for the reputation layer (trust scores, travel-plausibility)
to survive restarts — generate it once with `openssl rand -hex 16` and never change it. Without
it the service still runs (a fresh salt is generated for that run only), but every restart
silently resets every identity's trust score back to baseline; a startup warning fires either way
so this can't go unnoticed.

## Keeping this in sync with the public repo

When `geometry.py`/`reference_client.py`/`schema/*.json`/`geometry/*.json`/`website/*` change
here, the same change needs to land in the public repo's mirrored copies (and vice versa) — they
must stay byte-identical since producers build against the public copies but the deployed
service runs these. When the wire format itself changes (`PROTOCOL.md §1-§5`), the public repo's
trimmed `PROTOCOL.md` needs the matching update.

## Standing orders that apply here too

Same as the public repo: [security-obscurity standing order] — even though this repo is private,
don't get lax about it; semver odometer versioning; never log/echo real coordinates; goldfarm/
real-client testing before treating anything as proven-stable.
