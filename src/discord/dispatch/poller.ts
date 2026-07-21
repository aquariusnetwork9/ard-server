import fs from 'fs';
import path from 'path';
import {
  Client, TextChannel, NewsChannel, EmbedBuilder, ActionRowBuilder, ButtonBuilder,
  ButtonStyle, ButtonInteraction, GuildMember,
} from 'discord.js';
import { config } from '../../config';
import { listDispatch, claimDispatch, completeDispatch, roadName, DispatchEntry, ArdDispatchError } from '../../ard-client';
import { SERVER_ROLE, DISPATCH_CHANNEL_NAMES, DISPATCH_ACCESS_ROLES } from '../provision/structure';
import { isCurrentSession } from '../../runtime-lock';

/**
 * Polls ard-server's dispatch queue (PROTOCOL.md SS6.7) for every server this
 * bot knows about and renders it into #open/#closed/#records. There is no
 * push/SSE surface for the dispatch queue (unlike /conditions/<server>/stream)
 * -- polling is simple, sufficient for a human-paced volunteer queue, and
 * keeps this a server-API-light feature (the only ard-server change needed
 * was the auth path in ard-client.ts's dispatchRequest).
 *
 * State is a flat JSON file, not a database -- this bot already has no
 * database anywhere else (runtime-lock.ts's own file is the same idiom), and
 * the only thing being tracked is "which Discord message represents which
 * dispatch id," recoverable from ard-server's own queue if this file is ever
 * lost (a restart would just re-post everything currently open as "new").
 */

const STATE_PATH = path.join(process.cwd(), 'dispatch-state.json');
const POLL_INTERVAL_MS = 60_000;

interface TrackedEntry {
  server: string;
  messageId: string;
  lastStatus: DispatchEntry['status'];
  lastClaimedBy: string | null;
}

type DispatchState = Record<string, TrackedEntry>; // key: `${server}:${id}`

function loadState(): DispatchState {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function saveState(state: DispatchState): void {
  // Best-effort: a failed write here just means the next poll might re-post
  // something that was already posted, not a correctness problem for the
  // underlying queue itself (ard-server's own state is the source of truth).
  try {
    fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), 'utf8');
  } catch (err) {
    console.error('[dispatch] Failed to persist dispatch-state.json:', err);
  }
}

function findChannel(client: Client, name: string): TextChannel | NewsChannel | null {
  const guild = client.guilds.cache.get(config.discord.guildId);
  if (!guild) return null;
  const channel = guild.channels.cache.find(c => c.name === name);
  return (channel as TextChannel | NewsChannel) ?? null;
}

/** Renders a claimedBy actor id (ard-server PROTOCOL.md SS6.7: "tok:<id>" for
 *  a fleet bot holder, "discord:<id>" for a bot-vouched Discord identity)
 *  into something readable in an embed/log line. */
function actorLabel(claimedBy: string | null): string {
  if (!claimedBy) return 'nobody';
  if (claimedBy.startsWith('discord:')) return `<@${claimedBy.slice('discord:'.length)}>`;
  if (claimedBy.startsWith('tok:')) return `fleet bot \`${claimedBy.slice('tok:'.length)}\``;
  return claimedBy;
}

const TRIGGER_LABEL: Record<DispatchEntry['trigger'], string> = {
  reopen: '🔁 Reopen -- a cleared hazard came back',
  conflict: '⚠️ Conflict -- a fast CLEAR over a fresh hazard',
  low_trust: '🔍 Low-trust -- only ever confirmed by anonymous reports',
  manual: '📌 Manually queued',
};

async function describeTarget(server: string, entry: DispatchEntry): Promise<string> {
  const road = await roadName(server, entry.road);
  return `${server} -- **${road}**, segment ${entry.seg}`;
}

function openEmbed(server: string, entry: DispatchEntry, target: string): EmbedBuilder {
  const embed = new EmbedBuilder()
    .setTitle(target)
    .setDescription(TRIGGER_LABEL[entry.trigger])
    .setFooter({ text: `#${entry.id} -- priority ${entry.priority.toFixed(2)}` })
    .setColor(entry.status === 'claimed' ? 0xf1c40f : 0x2ecc71);
  if (entry.status === 'claimed') {
    embed.addFields({ name: 'Claimed by', value: actorLabel(entry.claimedBy) });
  }
  return embed;
}

function openComponents(entry: DispatchEntry): ActionRowBuilder<ButtonBuilder>[] {
  const row = new ActionRowBuilder<ButtonBuilder>();
  if (entry.status === 'queued') {
    row.addComponents(
      new ButtonBuilder().setCustomId(`dispatch:claim:${entry.id}`)
        .setLabel('Claim').setStyle(ButtonStyle.Primary),
    );
  } else {
    row.addComponents(
      new ButtonBuilder().setCustomId(`dispatch:complete:${entry.id}`)
        .setLabel('Mark complete').setStyle(ButtonStyle.Success),
    );
  }
  return [row];
}

export async function pollOnce(client: Client): Promise<void> {
  const openCh = findChannel(client, DISPATCH_CHANNEL_NAMES.open);
  const closedCh = findChannel(client, DISPATCH_CHANNEL_NAMES.closed);
  const recordsCh = findChannel(client, DISPATCH_CHANNEL_NAMES.records);
  if (!openCh || !closedCh || !recordsCh) {
    console.error('[dispatch] Dispatch Center channels are missing -- run /setup first.');
    return;
  }

  const state = loadState();
  const seenKeys = new Set<string>();

  for (const server of Object.keys(SERVER_ROLE)) {
    let queue: DispatchEntry[];
    try {
      queue = await listDispatch(server);
    } catch (err) {
      console.error(`[dispatch] Failed to poll ${server}:`, err);
      continue;
    }

    for (const entry of queue) {
      const key = `${server}:${entry.id}`;
      seenKeys.add(key);
      const tracked = state[key];
      const target = await describeTarget(server, entry);

      if (!tracked) {
        const msg = await openCh.send({ embeds: [openEmbed(server, entry, target)], components: openComponents(entry) });
        state[key] = { server, messageId: msg.id, lastStatus: entry.status, lastClaimedBy: entry.claimedBy };
        await recordsCh.send(`🆕 **${target}** queued (${entry.trigger})`);
        continue;
      }

      if (tracked.lastStatus !== entry.status || tracked.lastClaimedBy !== entry.claimedBy) {
        const msg = await openCh.messages.fetch(tracked.messageId).catch(() => null);
        if (msg) await msg.edit({ embeds: [openEmbed(server, entry, target)], components: openComponents(entry) });
        if (entry.status === 'claimed' && tracked.lastStatus === 'queued') {
          await recordsCh.send(`✋ **${target}** claimed by ${actorLabel(entry.claimedBy)}`);
        } else if (entry.status === 'queued' && tracked.lastStatus === 'claimed') {
          await recordsCh.send(`↩️ **${target}**: claim by ${actorLabel(tracked.lastClaimedBy)} expired -- back in the queue`);
        }
        tracked.lastStatus = entry.status;
        tracked.lastClaimedBy = entry.claimedBy;
      }
    }
  }

  // Anything still tracked but no longer in any server's open queue resolved
  // (done, via a fresh Tier A/M report or an explicit complete) or expired
  // (never claimed, aged out) -- list_dispatch only ever returns open rows,
  // so "disappeared" is the only signal available; lastStatus at the moment
  // it disappeared is what distinguishes the two.
  for (const [key, tracked] of Object.entries(state)) {
    if (seenKeys.has(key)) continue;
    const msg = await openCh.messages.fetch(tracked.messageId).catch(() => null);
    if (msg) await msg.delete().catch(() => {});
    const [server] = key.split(':');
    if (tracked.lastStatus === 'claimed') {
      await closedCh.send(`✅ Resolved on **${server}** -- completed by ${actorLabel(tracked.lastClaimedBy)}.`);
      await recordsCh.send(`✅ dispatch #${key.split(':')[1]} on **${server}** completed by ${actorLabel(tracked.lastClaimedBy)}`);
    } else {
      await recordsCh.send(`⌛ dispatch #${key.split(':')[1]} on **${server}** expired unclaimed`);
    }
    delete state[key];
  }

  saveState(state);
}

let pollTimer: ReturnType<typeof setInterval> | null = null;

export function startDispatchPolling(client: Client): void {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    isCurrentSession().then(current => {
      if (!current) return; // stale zombie process -- a newer one is polling
      pollOnce(client).catch(err => console.error('[dispatch] Poll failed:', err));
    });
  }, POLL_INTERVAL_MS);
  // Kick off an immediate first poll rather than waiting a full interval.
  pollOnce(client).catch(err => console.error('[dispatch] Initial poll failed:', err));
}

/** Whether `member` currently holds a role that grants dispatch access
 *  (PROTOCOL.md SS6.7 / structure.ts's DISPATCH_ACCESS_ROLES) -- the ONLY
 *  place this check happens; ard-server trusts the bot's word entirely. */
function hasDispatchAccess(member: GuildMember): boolean {
  return member.roles.cache.some(r => (DISPATCH_ACCESS_ROLES as readonly string[]).includes(r.name));
}

export async function handleDispatchButton(interaction: ButtonInteraction): Promise<void> {
  const [, action, idStr] = interaction.customId.split(':');
  const id = Number(idStr);
  const member = interaction.member;
  if (!(member instanceof GuildMember) || !hasDispatchAccess(member)) {
    await interaction.reply({
      content: 'You need a Dispatcher, Highway Supervisor, or Highway Inspector role to do that.',
      ephemeral: true,
    });
    return;
  }
  await interaction.deferReply({ ephemeral: true });
  try {
    if (action === 'claim') {
      await claimDispatch(id, interaction.user.id);
      await interaction.editReply('Claimed. Head out and resolve it, then hit "Mark complete" when you have.');
    } else if (action === 'complete') {
      await completeDispatch(id, interaction.user.id);
      await interaction.editReply('Marked complete -- thanks!');
    }
  } catch (err) {
    const message = err instanceof ArdDispatchError ? err.message : 'Could not reach the ARD server.';
    await interaction.editReply(`Failed: ${message}`);
    return;
  }
  // The next poll (within a minute) picks up the state change and updates
  // the embed/posts to Records -- not done inline here to keep exactly one
  // code path responsible for rendering queue state, avoiding two writers
  // racing to edit the same message.
}
