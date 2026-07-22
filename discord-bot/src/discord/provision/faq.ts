import { EmbedBuilder } from 'discord.js';

/**
 * Real FAQ content grounded in how the network actually works -- not generic
 * placeholder Q&A. Posted read-only: the public can view this channel but
 * SendMessages is denied to everyone except staff (see structure.ts's
 * `readOnly` flag + _utils.ts's buildReadOnlyOverwrites), so it stays a
 * reference doc, not a chat channel.
 */
export function buildFaqEmbed(verifyChannelId?: string): EmbedBuilder {
  const verifyRef = verifyChannelId ? `<#${verifyChannelId}>` : '#verify-here';
  return new EmbedBuilder()
    .setTitle('Frequently Asked Questions')
    .setColor(0x5865f2)
    .addFields(
      {
        name: 'What is Aquarius Road Department?',
        value:
          'A crowdsourced, real-time map of nether highway conditions (holes, obstructions, ' +
          'lava, etc.) for 2b2t and 6b6t -- fed by players and proxy bots as they travel. Map: ' +
          '<https://map.aquariusconnect.org>',
      },
      {
        name: 'How do I link my Minecraft account?',
        value:
          `Run \`/link <code>\` in ${verifyRef}. The code comes from your producer (proxy plugin ` +
          'or client mod) while logged into Minecraft -- that\'s what proves the account is yours. ' +
          'Linking swaps your Traveler rank for Highway Worker on that server and unlocks its channels.',
      },
      {
        name: "I linked but don't see the 2b2t/6b6t channels",
        value: 'Roles apply instantly, but if something looks off, try leaving and rejoining the ' +
          'channel view, or ping Highway Patrol.',
      },
      {
        name: 'Are my coordinates safe if I use this?',
        value:
          "Yes -- structurally, not just by policy. The report format can't carry an (x, z) at all, " +
          'only "how far along this known public road," so there\'s nothing to extract regardless of ' +
          'what happens to the system that stores it. Full details: <https://map.aquariusconnect.org/privacy.html>',
      },
      {
        name: "What's the difference between all the ranks?",
        value:
          'Traveler (default) -> Highway Worker (linked account, per server) -> Highway Supervisor ' +
          '(vouched). Highway Inspector is a maintainer badge. Highway Patrol, Director, and Branch ' +
          'Director are staff, assigned manually, not self-service.',
      },
      {
        name: 'How do I report a hazard or a fixed road?',
        value:
          'Reports come from the proxy plugin or client mod as you travel the highways -- there\'s ' +
          'no manual "type it in Discord" path today. See the map for what\'s currently reported.',
      },
      {
        name: 'What are the Survey / Road Crew ranks and the leaderboard?',
        value:
          'Cosmetic-only recognition for contributing -- Survey tracks confirmed reports, Road Crew ' +
          'tracks completed dispatch repairs. They carry no extra channel or dispatch access; they\'re ' +
          'just a badge. Road Crew always counts (a dispatch claim is never anonymous). Survey credit is ' +
          '**opt-in only** (`/credit on <server>`, off by default) -- opting in means your real Discord ID ' +
          'is permanently recorded against every confirmed report you make from then on, specifically so ' +
          'it can be attributed to you on the leaderboard. That record is kept even if you later run ' +
          '`/credit off` -- opting out only stops *future* reports from being credited. See `/leaderboard`.',
      },
      {
        name: "Can I get help if I'm stuck or something looks wrong?",
        value: 'Ask in your community\'s -help channel, or ping Highway Patrol directly.',
      },
    );
}
