import { SlashCommandBuilder, ChatInputCommandInteraction } from 'discord.js';
import { setCreditOptIn, ArdCreditsError } from '../../ard-client';
import { SERVER_ROLE } from '../provision/structure';

export const data = new SlashCommandBuilder()
  .setName('credit')
  .setDescription('Opt in or out of Survey-leaderboard credit for your confirmed reports')
  .addStringOption(o => o
    .setName('state')
    .setDescription('on or off')
    .setRequired(true)
    .addChoices({ name: 'on', value: 'on' }, { name: 'off', value: 'off' }))
  .addStringOption(o => o
    .setName('server')
    .setDescription('Which server this applies to')
    .setRequired(true)
    .addChoices(...Object.keys(SERVER_ROLE).map(s => ({ name: s, value: s }))));

export async function execute(interaction: ChatInputCommandInteraction): Promise<void> {
  await interaction.deferReply({ ephemeral: true });
  const optIn = interaction.options.getString('state', true) === 'on';
  const server = interaction.options.getString('server', true);

  try {
    await setCreditOptIn(interaction.user.id, server, optIn);
  } catch (e) {
    const message = e instanceof ArdCreditsError ? e.message : 'Could not reach the ARD server -- try again shortly.';
    await interaction.editReply(`Failed: ${message}. You need to have run /link for ${server} first.`);
    return;
  }

  if (optIn) {
    await interaction.editReply(
      `Opted in for **${server}**. From now on, every confirmed report you make there permanently records your ` +
      'Discord ID toward the Survey leaderboard -- kept even if you opt out again later. See #faq for details.'
    );
  } else {
    await interaction.editReply(
      `Opted out for **${server}**. Future confirmed reports there won't earn credit -- anything already ` +
      'credited stays on the leaderboard.'
    );
  }
}
