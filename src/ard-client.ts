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
