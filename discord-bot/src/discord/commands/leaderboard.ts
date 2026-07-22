import { SlashCommandBuilder, ChatInputCommandInteraction, EmbedBuilder } from 'discord.js';
import { getLeaderboard, ArdCreditsError, LeaderboardEntry } from '../../ard-client';
import { SERVER_ROLE } from '../provision/structure';
import { weekStartMs } from '../dispatch/periods';

export const data = new SlashCommandBuilder()
  .setName('leaderboard')
  .setDescription('Show the Survey and Road Crew leaderboards for a server')
  .addStringOption(o => o
    .setName('server')
    .setDescription('Which server')
    .setRequired(true)
    .addChoices(...Object.keys(SERVER_ROLE).map(s => ({ name: s, value: s }))));

function renderRows(entries: LeaderboardEntry[], limit: number): string {
  if (!entries.length) return 'no entries yet';
  return entries.slice(0, limit).map((e, i) => `${i + 1}. <@${e.discordId}> -- ${e.count}`).join('\n');
}

async function renderTrack(server: string, kind: 'survey' | 'crew'): Promise<string> {
  const [allTime, thisWeek] = await Promise.all([
    getLeaderboard(server, kind),
    getLeaderboard(server, kind, weekStartMs()),
  ]);
  return `**All-time**\n${renderRows(allTime, 5)}\n\n**This week**\n${renderRows(thisWeek, 3)}`;
}

export async function execute(interaction: ChatInputCommandInteraction): Promise<void> {
  await interaction.deferReply();
  const server = interaction.options.getString('server', true);
  try {
    const [survey, crew] = await Promise.all([renderTrack(server, 'survey'), renderTrack(server, 'crew')]);
    const embed = new EmbedBuilder()
      .setTitle(`${server} -- leaderboards`)
      .addFields(
        { name: '🔎 Survey (confirmed reports)', value: survey, inline: true },
        { name: '🛠️ Road Crew (completed repairs)', value: crew, inline: true },
      );
    await interaction.editReply({ embeds: [embed] });
  } catch (e) {
    const message = e instanceof ArdCreditsError ? e.message : 'Could not reach the ARD server -- try again shortly.';
    await interaction.editReply(`Failed: ${message}`);
  }
}
