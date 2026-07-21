import { config } from './config';

export class ArdLinkError extends Error {}

export interface CompleteLinkResult {
  tokenId: string;
  token: string;
  server: string;
}

/**
 * Completes a pending /link/init code via ARD's bot-authenticated path
 * (PROTOCOL.md SS6.1) -- proves the bot's own identity with ARD_BOT_SECRET
 * instead of a Discord OAuth code, since discordId is already Discord-verified
 * (it comes straight off the slash-command interaction).
 */
export async function completeLink(linkCode: string, discordId: string): Promise<CompleteLinkResult> {
  const resp = await fetch(`${config.ard.baseUrl}/link/bot-complete`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: config.ard.botSecret,
    },
    body: JSON.stringify({ linkCode, discordId }),
  });
  const body: any = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new ArdLinkError(body?.error ?? `ARD returned ${resp.status}`);
  }
  return body as CompleteLinkResult;
}

export class ArdDispatchError extends Error {}

export interface DispatchEntry {
  id: number;
  road: number | null;
  seg: number;
  along: number;
  trigger: 'reopen' | 'conflict' | 'low_trust' | 'manual';
  priority: number;
  status: 'queued' | 'claimed';
  claimedBy: string | null;
  created: number;
  claimedAt: number | null;
}

async function dispatchRequest(path: string, method: string, discordId?: string): Promise<any> {
  const resp = await fetch(`${config.ard.baseUrl}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Authorization: config.ard.botSecret,
    },
    ...(method === 'GET' ? {} : { body: JSON.stringify(discordId ? { discordId } : {}) }),
  });
  const body: any = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new ArdDispatchError(body?.error ?? `ARD returned ${resp.status}`);
  }
  return body;
}

/** GET /dispatch/<server> -- the bot credential alone is enough to list (no
 *  discordId needed, this is just polling to render the Discord queue view,
 *  not an action taken on anyone's behalf -- PROTOCOL.md SS6.7). */
export async function listDispatch(server: string): Promise<DispatchEntry[]> {
  const body = await dispatchRequest(`/dispatch/${server}`, 'GET');
  return (body.queue ?? []) as DispatchEntry[];
}

/** POST /dispatch/<id>/claim, vouching for discordId (SS6.7) -- the caller is
 *  responsible for having already checked this Discord member actually holds
 *  a qualifying role (see structure.ts's DISPATCH_ACCESS_ROLES / Dispatcher);
 *  ARD trusts the bot's word for it entirely and checks nothing itself. */
export async function claimDispatch(id: number, discordId: string): Promise<void> {
  await dispatchRequest(`/dispatch/${id}/claim`, 'POST', discordId);
}

/** POST /dispatch/<id>/complete, vouching for discordId. ARD force-completes
 *  (regardless of who claimed it) if discordId itself holds a discord_grants
 *  moderator/admin scope on the entry's server -- otherwise it must match the
 *  claim's own discordId. */
export async function completeDispatch(id: number, discordId: string): Promise<void> {
  await dispatchRequest(`/dispatch/${id}/complete`, 'POST', discordId);
}

export interface GeoRoad {
  i: number;
  name: string;
}

let geometryCache: { server: string; roads: GeoRoad[]; fetchedAt: number } | null = null;
const GEOMETRY_CACHE_TTL_MS = 10 * 60 * 1000;

/** GET /geometry/<server> -- fully public (PROTOCOL.md SS7), just the road
 *  table, cached for a while since it only ever changes on a geometry
 *  redeploy. Used purely to render a human-readable road name instead of a
 *  bare index in dispatch embeds/log lines. */
export async function roadName(server: string, roadIdx: number | null): Promise<string> {
  if (roadIdx === null) return 'unknown road';
  const now = Date.now();
  if (!geometryCache || geometryCache.server !== server || now - geometryCache.fetchedAt > GEOMETRY_CACHE_TTL_MS) {
    const resp = await fetch(`${config.ard.baseUrl}/geometry/${server}`);
    if (!resp.ok) return `road #${roadIdx}`;
    const body: any = await resp.json().catch(() => null);
    if (!body?.roads) return `road #${roadIdx}`;
    geometryCache = {
      server,
      roads: body.roads.map((r: any) => ({ i: r.i, name: r.name })),
      fetchedAt: now,
    };
  }
  return geometryCache.roads.find(r => r.i === roadIdx)?.name ?? `road #${roadIdx}`;
}

export class ArdCreditsError extends Error {}

/** Bot-authenticated GET/POST with an arbitrary JSON body -- like
 *  dispatchRequest above, but not limited to the `{discordId}` shape, since
 *  credit-opt-in needs `{discordId, server, optIn}` and the leaderboard/radar
 *  reads are GETs with query params instead of a body at all. */
async function ardRequest(path: string, method: string, body?: unknown): Promise<any> {
  const resp = await fetch(`${config.ard.baseUrl}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Authorization: config.ard.botSecret,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const parsed: any = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new ArdCreditsError(parsed?.error ?? `ARD returned ${resp.status}`);
  }
  return parsed;
}

/** POST /link/credit-opt-in -- flips whether an already-linked Discord identity's
 *  confirmed Tier B reports earn a permanent Survey-leaderboard credit (see
 *  identity.py's credit_opt_in, PROTOCOL.md SS6.7). Off by default; callers
 *  (the /credit command) are responsible for disclosing what opting in means
 *  before calling this. */
export async function setCreditOptIn(discordId: string, server: string, optIn: boolean): Promise<void> {
  await ardRequest('/link/credit-opt-in', 'POST', { discordId, server, optIn });
}

export interface LeaderboardEntry {
  discordId: string;
  count: number;
}

/** GET /credits/<server>/leaderboard -- `kind` picks Survey (confirmed reports)
 *  vs Road Crew (completed repairs); `sinceMs`, if given, scopes to a single
 *  weekly/monthly race instead of the lifetime total. */
export async function getLeaderboard(
  server: string, kind: 'survey' | 'crew', sinceMs?: number
): Promise<LeaderboardEntry[]> {
  const qs = new URLSearchParams({ kind });
  if (sinceMs !== undefined) qs.set('since', String(sinceMs / 1000));
  const body = await ardRequest(`/credits/${server}/leaderboard?${qs}`, 'GET');
  return (body.leaderboard ?? []) as LeaderboardEntry[];
}

export interface ConditionEntry {
  road: number | null;
  seg: number;
  along: number;
  cond: string;
  tier: 'A' | 'M' | 'B' | 'C';
  reports: number;
  distinctSources: number;
  confidence: number;
  published: boolean;
  firstSeen: number;
  lastSeen: number;
}

/** GET /conditions/<server>/all -- the bot/moderator-only radar feed, the one
 *  place unpublished (not-yet-corroborated) conditions are readable at all; the
 *  public /conditions/<server> never includes them (PROTOCOL.md SS6.7). */
export async function getAllConditions(server: string): Promise<ConditionEntry[]> {
  const body = await ardRequest(`/conditions/${server}/all`, 'GET');
  return (body.conditions ?? []) as ConditionEntry[];
}
