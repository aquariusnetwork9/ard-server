import fs from 'fs';
import path from 'path';
import { Client, TextChannel, NewsChannel } from 'discord.js';
import { config } from '../../config';
import { getLeaderboard } from '../../ard-client';
import {
  SERVER_ROLE, DISPATCH_CHANNEL_NAMES, TIER_THRESHOLDS,
  tierRoleName, rotatingBadgeName, Track, Cadence,
} from '../provision/structure';
import { periodKey, periodStartMs } from './periods';
import { isCurrentSession } from '../../runtime-lock';

/**
 * Two independent jobs, run together on one long-interval timer (ranks don't
 * need dispatch-queue freshness):
 *  1. Tier-role sync -- recomputes each member's lifetime Survey/Road Crew
 *     counts and grants any newly-crossed tier role. STACKING: only ever
 *     adds a role, never removes one already earned.
 *  2. Weekly/monthly rotating badge -- on a period rollover, recomputes that
 *     period's leader per track and moves the single rotating badge role to
 *     them, stripping it from whoever held it before.
 *
 * Both tracks are siloed per server, same as everywhere else in this project
 * (see structure.ts's own note on this) -- there is no cross-server ladder.
 */

const STATE_PATH = path.join(process.cwd(), 'tiers-state.json');
const SYNC_INTERVAL_MS = 5 * 60_000;

const TRACKS: Track[] = ['survey', 'crew'];
const CADENCES: Cadence[] = ['weekly', 'monthly'];

interface ServerTiersState {
  periodKeys: Record<Cadence, string>;
  // Who currently holds each cadence x track's rotating badge, or undefined.
  holders: Record<Cadence, Partial<Record<Track, string>>>;
}

type TiersState = Record<string, ServerTiersState>; // key: server

function loadState(): TiersState {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function saveState(state: TiersState): void {
  try {
    fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), 'utf8');
  } catch (err) {
    console.error('[tiers] Failed to persist tiers-state.json:', err);
  }
}

function findChannel(client: Client, name: string): TextChannel | NewsChannel | null {
  const guild = client.guilds.cache.get(config.discord.guildId);
  const channel = guild?.channels.cache.find(c => c.name === name);
  return (channel as TextChannel | NewsChannel) ?? null;
}

async function syncTierRoles(
  client: Client, server: string, track: Track, recordsCh: TextChannel | NewsChannel | null,
): Promise<void> {
  const guild = client.guilds.cache.get(config.discord.guildId);
  if (!guild) return;
  const board = await getLeaderboard(server, track);
  for (const entry of board) {
    // One fetch per credited member per sync cycle -- fine at this
    // community's scale; revisit with a bulk guild.members.fetch() if the
    // leaderboard ever grows large enough for this to matter.
    const member = await guild.members.fetch(entry.discordId).catch(() => null);
    if (!member) continue;
    for (let tierIndex = 0; tierIndex < TIER_THRESHOLDS.length; tierIndex++) {
      if (entry.count < TIER_THRESHOLDS[tierIndex]) break; // thresholds ascend -- stop at the first unmet one
      const roleName = tierRoleName(server, track, tierIndex);
      const role = guild.roles.cache.find(r => r.name === roleName);
      if (!role) continue; // /setup hasn't provisioned this role yet
      if (member.roles.cache.has(role.id)) continue; // already holds it -- stacking, never re-announce
      await member.roles.add(role).catch(() => {});
      await recordsCh?.send(`🎉 <@${entry.discordId}> just made **${roleName}** on **${server}**!`).catch(() => {});
    }
  }
}

async function rotateBadge(
  client: Client, server: string, track: Track, cadence: Cadence, serverState: ServerTiersState,
  recordsCh: TextChannel | NewsChannel | null,
): Promise<void> {
  const guild = client.guilds.cache.get(config.discord.guildId);
  if (!guild) return;
  const badgeName = rotatingBadgeName(server, track, cadence);
  const role = guild.roles.cache.find(r => r.name === badgeName);
  if (!role) return; // /setup hasn't provisioned this role yet

  const board = await getLeaderboard(server, track, periodStartMs(cadence));
  const leaderId = board[0]?.discordId;
  const prevHolderId = serverState.holders[cadence][track];

  if (prevHolderId && prevHolderId !== leaderId) {
    const prevMember = await guild.members.fetch(prevHolderId).catch(() => null);
    if (prevMember?.roles.cache.has(role.id)) await prevMember.roles.remove(role).catch(() => {});
  }
  if (leaderId && leaderId !== prevHolderId) {
    const newMember = await guild.members.fetch(leaderId).catch(() => null);
    if (newMember) {
      await newMember.roles.add(role).catch(() => {});
      await recordsCh?.send(`🏆 <@${leaderId}> is the new **${badgeName}** on **${server}**!`).catch(() => {});
    }
  }
  serverState.holders[cadence][track] = leaderId;
}

export async function syncTiersOnce(client: Client): Promise<void> {
  const state = loadState();
  const recordsCh = findChannel(client, DISPATCH_CHANNEL_NAMES.records);

  for (const server of Object.keys(SERVER_ROLE)) {
    try {
      for (const track of TRACKS) {
        await syncTierRoles(client, server, track, recordsCh);
      }

      const serverState: ServerTiersState = state[server] ?? {
        periodKeys: { weekly: '', monthly: '' },
        holders: { weekly: {}, monthly: {} },
      };
      for (const cadence of CADENCES) {
        const key = periodKey(cadence);
        if (serverState.periodKeys[cadence] === key) continue; // no rollover (or already handled this period)
        serverState.periodKeys[cadence] = key;
        for (const track of TRACKS) {
          await rotateBadge(client, server, track, cadence, serverState, recordsCh);
        }
      }
      state[server] = serverState;
    } catch (err) {
      console.error(`[tiers] Sync failed for ${server}:`, err);
    }
  }

  saveState(state);
}

let tiersTimer: ReturnType<typeof setInterval> | null = null;

export function startTiersSync(client: Client): void {
  if (tiersTimer) return;
  tiersTimer = setInterval(() => {
    isCurrentSession().then(current => {
      if (!current) return;
      syncTiersOnce(client).catch(err => console.error('[tiers] Sync failed:', err));
    });
  }, SYNC_INTERVAL_MS);
  syncTiersOnce(client).catch(err => console.error('[tiers] Initial sync failed:', err));
}
