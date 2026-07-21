import fs from 'fs';
import path from 'path';
import { Client, TextChannel, NewsChannel, EmbedBuilder } from 'discord.js';
import { config } from '../../config';
import { getAllConditions, listDispatch, roadName, ConditionEntry } from '../../ard-client';
import { SERVER_ROLE, DISPATCH_CHANNEL_NAMES, radarChannelName } from '../provision/structure';
import { isCurrentSession } from '../../runtime-lock';

/**
 * Situational-awareness feed, distinct from the dispatch queue (poller.ts):
 * shows EVERY hazard report worth a human's attention, not just the promoted
 * subset. One rolling embed per server (edited in place, only re-rendered on
 * real change) plus a `#records` one-liner on genuine per-condition
 * transitions (new / confirmed / aged-out) -- never a fresh message per
 * condition, which would flood the channel given how often bots resend.
 *
 * Reads /conditions/<server>/all (bot/moderator-only -- see ard-client.ts and
 * PROTOCOL.md SS6.7), the one surface that includes unpublished rows, so a
 * lone not-yet-corroborated report can still be logged (per the agreed
 * "log even a single Tier C report, only DISPLAY once 2+ C-tier or any
 * B/A/M" design) even though the public /conditions route can't see it.
 */

const STATE_PATH = path.join(process.cwd(), 'radar-state.json');
const POLL_INTERVAL_MS = 60_000;

type ConditionState = 'reported' | 'confirmed';

interface ServerRadarState {
  messageId: string | null;
  // key: `${road}:${seg}:${along}:${cond}`
  keys: Record<string, ConditionState>;
  // Stringified snapshot of the last-rendered embed content -- lets pollOnce skip
  // editing the message at all on a quiet cycle where nothing actually changed.
  snapshot: string;
}

type RadarState = Record<string, ServerRadarState>; // key: server

function loadState(): RadarState {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function saveState(state: RadarState): void {
  try {
    fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), 'utf8');
  } catch (err) {
    console.error('[radar] Failed to persist radar-state.json:', err);
  }
}

function findChannel(client: Client, name: string): TextChannel | NewsChannel | null {
  const guild = client.guilds.cache.get(config.discord.guildId);
  const channel = guild?.channels.cache.find(c => c.name === name);
  return (channel as TextChannel | NewsChannel) ?? null;
}

function conditionKey(c: ConditionEntry): string {
  return `${c.road}:${c.seg}:${c.along}:${c.cond}`;
}

const HAZARD_SEVERITY: Record<string, number> = {
  OBSTRUCTION_FULL: 0, LAVA: 1, HOLE: 2, OBSTRUCTION_PARTIAL: 3,
  COBWEB: 4, WATER: 5, GRAVEL: 6, UNBUILT: 7,
};

/** 2+ Tier C sources, or any single B/A/M source -- the agreed floor for
 *  showing an unconfirmed condition in the LIVE embed. Below this, a
 *  condition still gets logged to #records (see pollOnce) but stays out of
 *  the rolling summary -- a single anonymous ping is too weak a signal to
 *  put in front of everyone, but is still worth a permanent audit line. */
function meetsDisplayFloor(c: ConditionEntry): boolean {
  return c.tier !== 'C' || c.distinctSources >= 2;
}

function isHazard(cond: string): boolean {
  return cond !== 'CLEAR' && cond !== 'PRESENCE';
}

async function describeCondition(server: string, c: ConditionEntry, dispatched: Set<string>): Promise<string> {
  const spatialKey = `${c.road}:${c.seg}:${c.along}`;
  if (dispatched.has(spatialKey)) {
    const road = await roadName(server, c.road);
    return `${c.cond} @ **${road}** seg ${c.seg} -- → see #${DISPATCH_CHANNEL_NAMES.open.split('・')[1]}`;
  }
  const road = await roadName(server, c.road);
  const confirmedTag = c.published ? '' : ` (${c.tier}-tier, ${c.distinctSources} report${c.distinctSources === 1 ? '' : 's'}, unconfirmed)`;
  return `${c.cond} @ **${road}** seg ${c.seg}${confirmedTag}`;
}

function radarEmbed(server: string, blockages: string[], unconfirmed: string[], activity: string[]): EmbedBuilder {
  const embed = new EmbedBuilder()
    .setTitle(`${server} -- road radar`)
    .setColor(blockages.length > 0 ? 0xe74c3c : unconfirmed.length > 0 ? 0xf1c40f : 0x2ecc71)
    .setFooter({ text: `updated ${new Date().toLocaleTimeString()}` });
  embed.addFields(
    { name: `🔴 Blockages (${blockages.length})`, value: blockages.length ? blockages.slice(0, 12).join('\n') : 'none' },
    { name: `🟡 Unconfirmed (${unconfirmed.length})`, value: unconfirmed.length ? unconfirmed.slice(0, 12).join('\n') : 'none' },
    { name: `👤 Activity (${activity.length})`, value: activity.length ? activity.slice(0, 8).join('\n') : 'none' },
  );
  if (blockages.length > 12 || unconfirmed.length > 12 || activity.length > 8) {
    embed.setDescription('(list truncated to the busiest entries per section)');
  }
  return embed;
}

export async function pollOnce(client: Client): Promise<void> {
  const state = loadState();

  for (const server of Object.keys(SERVER_ROLE)) {
    const channel = findChannel(client, radarChannelName(server));
    const recordsCh = findChannel(client, DISPATCH_CHANNEL_NAMES.records);
    if (!channel) {
      console.error(`[radar] #${radarChannelName(server)} is missing -- run /setup first.`);
      continue;
    }

    let conditions: ConditionEntry[];
    let dispatchQueue: Awaited<ReturnType<typeof listDispatch>>;
    try {
      [conditions, dispatchQueue] = await Promise.all([getAllConditions(server), listDispatch(server)]);
    } catch (err) {
      console.error(`[radar] Failed to poll ${server}:`, err);
      continue;
    }
    const dispatched = new Set(dispatchQueue.map(e => `${e.road}:${e.seg}:${e.along}`));

    const serverState: ServerRadarState = state[server] ?? { messageId: null, keys: {}, snapshot: '' };
    const seenKeys = new Set<string>();
    const blockageLines: string[] = [];
    const unconfirmedLines: string[] = [];
    const activityLines: string[] = [];

    for (const c of conditions) {
      if (c.cond === 'CLEAR') continue;
      const key = conditionKey(c);

      if (c.cond === 'PRESENCE') {
        activityLines.push(await describeCondition(server, c, dispatched));
        continue;
      }
      if (!isHazard(c.cond)) continue;
      seenKeys.add(key);

      const newState: ConditionState = c.published ? 'confirmed' : 'reported';
      const prevState = serverState.keys[key];
      if (recordsCh) {
        if (!prevState) {
          const road = await roadName(server, c.road);
          const label = newState === 'confirmed' ? '✅ confirmed on first report' : '🆕 reported';
          await recordsCh.send(`${label}: **${c.cond}** @ ${server} -- **${road}** seg ${c.seg} (${c.tier}-tier)`).catch(() => {});
        } else if (prevState === 'reported' && newState === 'confirmed') {
          const road = await roadName(server, c.road);
          await recordsCh.send(`✅ confirmed: **${c.cond}** @ ${server} -- **${road}** seg ${c.seg}`).catch(() => {});
        }
      }
      serverState.keys[key] = newState;

      if (newState === 'confirmed') {
        blockageLines.push(await describeCondition(server, c, dispatched));
      } else if (meetsDisplayFloor(c)) {
        unconfirmedLines.push(await describeCondition(server, c, dispatched));
      }
    }

    // Anything tracked but no longer returned by /all either resolved (a fresh
    // CLEAR superseded it) or aged out (ttl elapsed with no fresh reports) --
    // the query itself can't tell us which, so we infer from whichever state
    // this key was in the moment it disappeared: 'confirmed' reads as
    // resolved (a genuinely positive outcome), 'reported' reads as aged out,
    // worded neutrally -- it was never disproven, just never corroborated.
    for (const [key, prevState] of Object.entries(serverState.keys)) {
      if (seenKeys.has(key)) continue;
      if (recordsCh) {
        const [, , , cond] = key.split(':');
        const label = prevState === 'confirmed'
          ? `✅ resolved: **${cond}** on **${server}**`
          : `⌛ aged out, unconfirmed: **${cond}** on **${server}** -- no one corroborated it, could still be accurate`;
        await recordsCh.send(label).catch(() => {});
      }
      delete serverState.keys[key];
    }

    blockageLines.sort((a, b) => {
      const sevA = HAZARD_SEVERITY[a.split(' @')[0]] ?? 99;
      const sevB = HAZARD_SEVERITY[b.split(' @')[0]] ?? 99;
      return sevA - sevB;
    });

    const snapshot = JSON.stringify({ blockageLines, unconfirmedLines, activityLines });
    if (serverState.snapshot !== snapshot) {
      const embed = radarEmbed(server, blockageLines, unconfirmedLines, activityLines);
      const existing = serverState.messageId
        ? await channel.messages.fetch(serverState.messageId).catch(() => null)
        : null;
      if (existing) {
        await existing.edit({ embeds: [embed] }).catch(() => {});
      } else {
        const sent = await channel.send({ embeds: [embed] });
        serverState.messageId = sent.id;
      }
      serverState.snapshot = snapshot;
    }
    state[server] = serverState;
  }

  saveState(state);
}

let radarTimer: ReturnType<typeof setInterval> | null = null;

export function startRadarPolling(client: Client): void {
  if (radarTimer) return;
  radarTimer = setInterval(() => {
    isCurrentSession().then(current => {
      if (!current) return;
      pollOnce(client).catch(err => console.error('[radar] Poll failed:', err));
    });
  }, POLL_INTERVAL_MS);
  pollOnce(client).catch(err => console.error('[radar] Initial poll failed:', err));
}
