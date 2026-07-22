/**
 * Directly exercises the exact same guild.editOnboarding() call setup.ts's
 * applyOnboarding() would make, bypassing the Discord slash-command layer
 * entirely -- dry-run mode in /setup never actually calls this API, so it
 * can't validate a real API-shape bug like the one that just failed live
 * (ROLE_OR_CHANNEL_REQUIRED). This is the only way to confirm the fix
 * actually works before asking for another live /setup confirm:true attempt.
 */
import { Client, GatewayIntentBits, ChannelType, GuildOnboardingMode } from 'discord.js';
import { config } from '../src/config';
import { ONBOARDING_DEFAULT_CHANNEL_KEYS, ONBOARDING_PROMPT } from '../src/discord/provision/onboarding';

async function main(): Promise<void> {
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(config.discord.token);
  await new Promise<void>(resolve => client.once('ready', () => resolve()));

  const guild = await client.guilds.fetch(config.discord.guildId);
  await guild.channels.fetch();

  const channelByKey = new Map<string, { id: string }>();
  for (const c of guild.channels.cache.values()) {
    if (c.type !== ChannelType.GuildText && c.type !== ChannelType.GuildAnnouncement) continue;
    if (c.name === '🛑・rules') channelByKey.set('rules', c);
    if (c.name === '📢・announcements') channelByKey.set('announcements', c);
    if (c.name === '🪪・verify-here') channelByKey.set('verify-here', c);
    if (c.name === '❓・faq') channelByKey.set('faq', c);
  }

  const ourDefaultChannels = ONBOARDING_DEFAULT_CHANNEL_KEYS.map(k => channelByKey.get(k)).filter(Boolean) as { id: string }[];
  console.log(`Resolved ${ourDefaultChannels.length}/${ONBOARDING_DEFAULT_CHANNEL_KEYS.length} default channels`);

  const current = await guild.fetchOnboarding();
  const existingPrompt = [...current.prompts.values()].find(p => p.title === ONBOARDING_PROMPT.title);

  const promptData = {
    ...(existingPrompt ? { id: existingPrompt.id } : {}),
    title: ONBOARDING_PROMPT.title,
    singleSelect: ONBOARDING_PROMPT.singleSelect,
    required: ONBOARDING_PROMPT.required,
    inOnboarding: ONBOARDING_PROMPT.inOnboarding,
    type: ONBOARDING_PROMPT.type,
    options: ONBOARDING_PROMPT.options.map(o => {
      const existingOption = existingPrompt?.options.find(eo => eo.title === o.title);
      const target = channelByKey.get(o.channelKey);
      if (!target) throw new Error(`Missing channel for option "${o.title}": ${o.channelKey}`);
      return {
        ...(existingOption ? { id: existingOption.id } : {}),
        title: o.title,
        description: o.description,
        emoji: o.emoji,
        channels: [target.id],
      };
    }),
  };

  const otherPrompts = [...current.prompts.values()].filter(p => p.id !== existingPrompt?.id).map(p => ({
    id: p.id, title: p.title, singleSelect: p.singleSelect, required: p.required,
    inOnboarding: p.inOnboarding, type: p.type,
    options: [...p.options.values()].map(o => ({
      id: o.id, title: o.title, description: o.description, emoji: o.emoji,
      channels: [...o.channels.keys()], roles: [...o.roles.keys()],
    })),
  }));
  const mergedPrompts = [...otherPrompts, promptData];

  const ourIds = new Set(ourDefaultChannels.map(c => c.id));
  const mergedDefaultChannels = [
    ...[...current.defaultChannels.keys()].filter(id => !ourIds.has(id)),
    ...ourDefaultChannels.map(c => c.id),
  ];

  console.log('Submitting editOnboarding with', mergedPrompts.length, 'prompt(s),', mergedDefaultChannels.length, 'default channel(s)...');
  console.log(JSON.stringify({ prompts: mergedPrompts, defaultChannels: mergedDefaultChannels }, null, 2));

  const result = await guild.editOnboarding({
    enabled: true,
    mode: GuildOnboardingMode.OnboardingDefault,
    defaultChannels: mergedDefaultChannels,
    prompts: mergedPrompts,
  });

  console.log('\nSUCCESS. Live onboarding now has', result.prompts.size, 'prompt(s):');
  for (const p of result.prompts.values()) console.log(`  "${p.title}" (${p.options.size} options)`);

  await client.destroy();
}

main().catch(err => { console.error('FAILED:', err); process.exit(1); });
