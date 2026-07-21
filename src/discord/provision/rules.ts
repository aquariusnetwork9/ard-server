import { EmbedBuilder } from 'discord.js';

/**
 * Real rules copy, not placeholder text -- reviewed before /setup ever posts
 * it live. Rule 2 ties directly back to what this whole project is built on:
 * ARD's wire protocol makes real coordinates structurally impossible to leak
 * (PROTOCOL.md SS1), so the community norm here is the same guarantee, not a
 * generic anti-doxxing boilerplate line.
 */
export function buildRulesEmbed(verifyChannelId?: string): EmbedBuilder {
  const verifyRef = verifyChannelId ? `<#${verifyChannelId}>` : '#verify-here';
  return new EmbedBuilder()
    .setTitle('Aquarius Road Dept -- Server Rules')
    .setColor(0x3ddc97)
    .setDescription(
      'This server is the community home for the Aquarius Road Department highway-' +
      'conditions network, covering both 2b2t and 6b6t. Read this once; it\'s short.'
    )
    .addFields(
      {
        name: '1. Be respectful',
        value: 'No harassment, hate speech, or targeted insults. Disagreements happen -- keep them civil.',
      },
      {
        name: '2. No coordinates, no doxxing',
        value:
          'Don\'t post or ask for anyone\'s real base coordinates, IRL info, or other identifying ' +
          'details. The highway network itself is built so that\'s never necessary -- see how at ' +
          '<https://map.aquariusconnect.org/privacy.html>. This server holds the same line.',
      },
      {
        name: '3. Report accurately',
        value:
          'Don\'t submit false hazard or CLEAR reports to the highway network. Bad data costs ' +
          'someone a bad reroute or a false sense of safety.',
      },
      {
        name: '4. Follow Discord\'s rules too',
        value: 'Discord\'s [Terms of Service](https://discord.com/terms) and [Community Guidelines](https://discord.com/guidelines) apply here as everywhere.',
      },
      {
        name: '5. No spam or unsolicited self-promo',
        value: 'Including unsolicited DMs to other members.',
      },
      {
        name: '6. Stay on-topic per channel',
        value: '2b2t discussion in the 2b2t category, 6b6t discussion in 6b6t\'s. Shared topics go in Community.',
      },
      {
        name: '7. Staff calls are final',
        value: 'Highway Patrol/Director decisions stand. If you think one was wrong, say so in a DM -- not by escalating in-channel.',
      },
      {
        name: 'Getting verified',
        value:
          `Run \`/link <code>\` in ${verifyRef} with the code your producer (proxy or client mod) ` +
          'showed you in-game. It swaps your **Traveler** rank for **Highway Worker** on that server ' +
          'and unlocks its channels.',
      },
      {
        name: 'Ranks',
        value:
          'Traveler → **Highway Worker** (linked account) → **Highway Supervisor** (vouched, hand-picked) ' +
          '-- plus **Highway Inspector** (a maintainer badge), **Highway Patrol** (moderation), ' +
          '**Director** and **Branch Director** (server leadership). Everything past Highway Worker ' +
          'is assigned by staff, not self-service.',
      },
    )
    .setFooter({ text: 'Full privacy policy & terms: map.aquariusconnect.org' });
}
