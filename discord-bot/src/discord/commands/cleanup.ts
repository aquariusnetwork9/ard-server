import {
  SlashCommandBuilder,
  ChatInputCommandInteraction,
  ChannelType,
  CategoryChannel,
  TextChannel,
  NewsChannel,
  Role,
  AutoModerationRule,
} from 'discord.js';
import { requireAdmin } from './_utils';

// A managed text channel could be a plain GuildText channel or have been
// converted to an Announcement (GuildAnnouncement) channel by an admin
// through Discord's UI -- both need to be considered when hunting for
// name-collisions, or a duplicate straddling the two types would be missed.
const TEXT_LIKE_TYPES = [ChannelType.GuildText, ChannelType.GuildAnnouncement];

export const data = new SlashCommandBuilder()
  .setName('cleanup')
  .setDescription('Find and remove duplicate roles/categories/channels/AutoMod rules (keeps the oldest of each)')
  .addBooleanOption(o => o
    .setName('confirm')
    .setDescription('Actually delete. Without this, shows a dry-run preview only.')
    .setRequired(false));

let running = false;

interface Dupe { kind: 'category' | 'channel' | 'role' | 'automod'; name: string; id: string; }

// Discord snowflakes are monotonically increasing by creation time -- sorting
// by the raw ID as a BigInt is a reliable "oldest first" order for anything
// with a snowflake ID, without needing each object type to separately expose
// a usable createdTimestamp.
function oldestFirst<T extends { id: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1));
}

function findDupes<T extends { id: string; name: string }>(items: T[]): { keep: T[]; dupes: T[] } {
  const byName = new Map<string, T[]>();
  for (const item of items) {
    const list = byName.get(item.name) ?? [];
    list.push(item);
    byName.set(item.name, list);
  }
  const keep: T[] = [];
  const dupes: T[] = [];
  for (const list of byName.values()) {
    const ordered = oldestFirst(list);
    keep.push(ordered[0]);
    dupes.push(...ordered.slice(1));
  }
  return { keep, dupes };
}

export async function execute(interaction: ChatInputCommandInteraction): Promise<void> {
  if (await requireAdmin(interaction)) return;
  const guild = interaction.guild;
  if (!guild) {
    await interaction.reply({ content: 'This command only works inside a server.', ephemeral: true });
    return;
  }
  if (running) {
    await interaction.reply({ content: 'Another /cleanup is already running -- wait for it to finish.', ephemeral: true });
    return;
  }
  running = true;
  try {
    await runCleanup(interaction, guild);
  } finally {
    running = false;
  }
}

async function runCleanup(interaction: ChatInputCommandInteraction, guild: NonNullable<ChatInputCommandInteraction['guild']>): Promise<void> {
  const confirm = interaction.options.getBoolean('confirm') ?? false;
  await interaction.deferReply({ ephemeral: true });

  await guild.channels.fetch();
  await guild.roles.fetch();
  const autoModRules = await guild.autoModerationRules.fetch();

  const categories = [...guild.channels.cache.values()]
    .filter((c): c is CategoryChannel => c.type === ChannelType.GuildCategory);
  const textChannels = [...guild.channels.cache.values()]
    .filter((c): c is TextChannel | NewsChannel => TEXT_LIKE_TYPES.includes(c.type));
  const roles = [...guild.roles.cache.values()].filter((r): r is Role => r.id !== guild.id); // exclude @everyone
  const rules = [...autoModRules.values()] as AutoModerationRule[];

  const dupes: Dupe[] = [];
  dupes.push(...findDupes(categories).dupes.map(c => ({ kind: 'category' as const, name: c.name, id: c.id })));
  dupes.push(...findDupes(textChannels).dupes.map(c => ({ kind: 'channel' as const, name: c.name, id: c.id })));
  dupes.push(...findDupes(roles).dupes.map(r => ({ kind: 'role' as const, name: r.name, id: r.id })));
  dupes.push(...findDupes(rules).dupes.map(r => ({ kind: 'automod' as const, name: r.name, id: r.id })));

  if (!dupes.length) {
    await interaction.editReply('No duplicates found.');
    return;
  }

  if (confirm) {
    for (const d of dupes) {
      try {
        if (d.kind === 'role') await guild.roles.delete(d.id);
        else if (d.kind === 'automod') await guild.autoModerationRules.delete(d.id);
        else await guild.channels.delete(d.id);
      } catch (err) {
        console.error(`[cleanup] Failed to delete ${d.kind} "${d.name}" (${d.id}):`, err);
      }
    }
  }

  const byKind = { category: [] as string[], channel: [] as string[], role: [] as string[], automod: [] as string[] };
  for (const d of dupes) byKind[d.kind].push(d.name);

  const lines = [
    confirm ? '**Deleted:**' : '**Would delete (dry run -- re-run with `confirm:true` to apply):**',
    byKind.category.length ? `Categories:\n${byKind.category.map(n => `- ${n}`).join('\n')}` : null,
    byKind.channel.length ? `Channels:\n${byKind.channel.map(n => `- #${n}`).join('\n')}` : null,
    byKind.role.length ? `Roles:\n${byKind.role.map(n => `- ${n}`).join('\n')}` : null,
    byKind.automod.length ? `AutoMod rules:\n${byKind.automod.map(n => `- ${n}`).join('\n')}` : null,
    confirm ? '\nRun `/setup confirm:true` afterward to fix up parenting/permissions on the survivors.' : null,
  ].filter(Boolean).join('\n\n');

  await interaction.editReply(lines.slice(0, 1990));
}
