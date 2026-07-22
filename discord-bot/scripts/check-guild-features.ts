import { Client, GatewayIntentBits } from 'discord.js';
import { config } from '../src/config';

async function main(): Promise<void> {
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(config.discord.token);
  await new Promise<void>(resolve => client.once('ready', () => resolve()));

  const guild = await client.guilds.fetch(config.discord.guildId);
  const full = await guild.fetch();
  console.log('features:', full.features);
  console.log('verificationLevel:', full.verificationLevel);
  console.log('rulesChannelId:', full.rulesChannelId);
  console.log('publicUpdatesChannelId:', full.publicUpdatesChannelId);
  console.log('explicitContentFilter:', full.explicitContentFilter);

  try {
    const onboarding = await full.fetchOnboarding();
    console.log('onboarding.enabled:', onboarding.enabled);
    console.log('onboarding.mode:', onboarding.mode);
    console.log('onboarding.defaultChannels:', [...onboarding.defaultChannels.values()].map(c => c.name));
    console.log('onboarding.prompts:', onboarding.prompts.size);
  } catch (err) {
    console.log('fetchOnboarding() failed:', (err as Error).message);
  }

  await client.destroy();
}

main().catch(err => { console.error(err); process.exit(1); });
