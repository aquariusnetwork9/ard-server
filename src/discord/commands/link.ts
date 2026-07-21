import { SlashCommandBuilder, ChatInputCommandInteraction, GuildMember } from 'discord.js';
import { completeLink, ArdLinkError } from '../../ard-client';
import { SERVER_ROLE, TRAVELER_ROLE } from '../provision/structure';

export const data = new SlashCommandBuilder()
  .setName('link')
  .setDescription('Link your Minecraft account using the code your producer showed you in-game')
  .addStringOption(o => o
    .setName('code')
    .setDescription('The link code, e.g. A1B2-C3D4')
    .setRequired(true));

export async function execute(interaction: ChatInputCommandInteraction): Promise<void> {
  await interaction.deferReply({ ephemeral: true });
  const code = interaction.options.getString('code', true).trim().toUpperCase();

  let result;
  try {
    result = await completeLink(code, interaction.user.id);
  } catch (e) {
    const message = e instanceof ArdLinkError ? e.message : 'Could not reach the ARD server -- try again shortly.';
    await interaction.editReply(`Link failed: ${message}`);
    return;
  }

  const roleName = SERVER_ROLE[result.server];
  if (!roleName) {
    await interaction.editReply(
      `Linked for **${result.server}**, but no verified role is configured for that server yet -- ping an admin.`
    );
    return;
  }

  const guild = interaction.guild;
  const member = interaction.member;
  if (!guild || !(member instanceof GuildMember)) {
    await interaction.editReply(`Linked for **${result.server}**, but this only works run inside the server.`);
    return;
  }

  const role = guild.roles.cache.find(r => r.name === roleName);
  if (!role) {
    await interaction.editReply(
      `Linked for **${result.server}**, but the **${roleName}** role doesn't exist yet -- ` +
      'an admin needs to run `/setup` first, then re-run `/link` with a fresh code.'
    );
    return;
  }

  await member.roles.add(role);
  // Graduate off the default rank -- once someone's a Highway Worker on at
  // least one server, "Traveler" no longer describes them. Best-effort: a
  // missing Traveler role (never ran /setup, or already removed) isn't an
  // error worth surfacing here.
  const traveler = guild.roles.cache.find(r => r.name === TRAVELER_ROLE);
  if (traveler && member.roles.cache.has(traveler.id)) {
    await member.roles.remove(traveler).catch(() => {});
  }

  await interaction.editReply(
    `Linked! You now have **${roleName}** and access to the ${result.server} channels.`
  );
}
