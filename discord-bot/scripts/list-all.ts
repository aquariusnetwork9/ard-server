import { Client, GatewayIntentBits, ChannelType } from 'discord.js';
import { config } from '../src/config';

async function main(): Promise<void> {
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(config.discord.token);
  await new Promise<void>(resolve => client.once('ready', () => resolve()));

  const guild = await client.guilds.fetch(config.discord.guildId);
  await guild.channels.fetch();
  await guild.roles.fetch();

  console.log(`Guild "${guild.name}" [${guild.id}]`);
  console.log(`publicUpdatesChannelId: ${guild.publicUpdatesChannelId}`);
  console.log(`rulesChannelId: ${guild.rulesChannelId}`);

  const cats = [...guild.channels.cache.values()].filter(c => c.type === ChannelType.GuildCategory);
  console.log(`\n--- Categories (${cats.length}) ---`);
  for (const c of cats) {
    console.log(`  ${c.name}`);
    const children = [...guild.channels.cache.values()].filter(ch => ch.parentId === c.id);
    for (const ch of children) console.log(`    - ${ch.name} (${ChannelType[ch.type]})`);
  }

  const orphanChannels = [...guild.channels.cache.values()].filter(c => c.type !== ChannelType.GuildCategory && !c.parentId);
  console.log(`\n--- Uncategorized channels (${orphanChannels.length}) ---`);
  for (const c of orphanChannels) console.log(`  ${c.name} (${ChannelType[c.type]})`);

  const onboarding = await guild.fetchOnboarding();
  console.log(`\n--- Onboarding ---`);
  console.log(`enabled: ${onboarding.enabled}, mode: ${onboarding.mode}`);
  console.log(`defaultChannels: ${[...onboarding.defaultChannels.values()].map(c => c.name).join(', ')}`);
  console.log(`prompts: ${onboarding.prompts.size}`);
  for (const p of onboarding.prompts.values()) {
    console.log(`  "${p.title}" (${p.options.size} options)`);
  }

  await client.destroy();
}

main().catch(err => { console.error(err); process.exit(1); });
