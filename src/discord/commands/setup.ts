import {
  SlashCommandBuilder,
  ChatInputCommandInteraction,
  OverwriteResolvable,
  Role,
  TextChannel,
  NewsChannel,
  AutoModerationRuleTriggerType,
  AutoModerationRuleEventType,
  AutoModerationActionType,
  GuildOnboardingMode,
  GuildOnboardingPromptData,
  PermissionFlagsBits,
} from 'discord.js';
import {
  requireAdmin, findOrCreateRole, findOrCreateCategory, findOrCreateTextChannel,
  buildOverwrites, buildReadOnlyOverwrites,
} from './_utils';
import { ROLES, CATEGORIES } from '../provision/structure';
import { buildRulesEmbed } from '../provision/rules';
import { buildFaqEmbed } from '../provision/faq';
import { ONBOARDING_DEFAULT_CHANNEL_KEYS, ONBOARDING_PROMPT } from '../provision/onboarding';
import {
  STAFF_ROLES_FOR_MODERATION, RESTRICTED_BASE_PERMISSIONS, NO_THREAD_PERMISSIONS,
  MENTION_EVERYONE_ROLES, UNMENTIONABLE_ROLES, AUTOMOD_RULES,
} from '../provision/moderation';

export const data = new SlashCommandBuilder()
  .setName('setup')
  .setDescription('Provision (or re-check) the server: roles, categories, channels, and rules')
  .addBooleanOption(o => o
    .setName('confirm')
    .setDescription('Actually create things. Without this, shows a dry-run preview only.')
    .setRequired(false));

const RULES_EMBED_TITLE = 'Aquarius Road Dept -- Server Rules';
const FAQ_EMBED_TITLE = 'Frequently Asked Questions';

// Prevents two /setup invocations racing each other -- both would see
// "doesn't exist yet" if they check the cache concurrently before either has
// finished creating, and both would then create it, leaving a duplicate. Only
// one guild to worry about, so a single module-level flag is enough.
let running = false;

export async function execute(interaction: ChatInputCommandInteraction): Promise<void> {
  if (await requireAdmin(interaction)) return;
  const guild = interaction.guild;
  if (!guild) {
    await interaction.reply({ content: 'This command only works inside a server.', ephemeral: true });
    return;
  }
  if (running) {
    await interaction.reply({ content: 'Another /setup is already running -- wait for it to finish.', ephemeral: true });
    return;
  }
  running = true;
  try {
    await runSetup(interaction, guild);
  } finally {
    running = false;
  }
}

async function runSetup(interaction: ChatInputCommandInteraction, guild: NonNullable<ChatInputCommandInteraction['guild']>): Promise<void> {
  const confirm = interaction.options.getBoolean('confirm') ?? false;
  await interaction.deferReply({ ephemeral: true });

  // Force a fresh REST fetch rather than trusting whatever's in the gateway
  // cache -- a stale/incomplete cache (e.g. right after a bot restart) is
  // exactly what makes a "does this already exist?" check wrongly say no and
  // create a duplicate of everything.
  await guild.channels.fetch();
  await guild.roles.fetch();

  const created: string[] = [];
  const renamed: string[] = [];
  const existing: string[] = [];
  const roleByName = new Map<string, Role>();

  // 1. Roles first -- categories/channels need them for permission overwrites.
  for (const spec of ROLES) {
    const { role, outcome } = await findOrCreateRole(
      guild, spec.name, spec.color, spec.hoist, !confirm, spec.oldNames
    );
    if (role) roleByName.set(spec.name, role);
    if (outcome === 'created') created.push(`role "${spec.name}"`);
    else if (outcome === 'renamed') renamed.push(`role "${spec.oldNames?.[0]}" -> "${spec.name}"`);
    else existing.push(`role "${spec.name}"`);
  }

  // 2. Categories + their channels.
  const channelByKey = new Map<string, TextChannel | NewsChannel>();
  const everyoneId = guild.roles.everyone.id;

  for (const cat of CATEGORIES) {
    const catOverwrites: OverwriteResolvable[] = cat.visibleTo.includes('everyone')
      ? [] : buildOverwrites(everyoneId, cat.visibleTo, cat.visibleTo, roleByName);

    const { category, outcome: catOutcome } = await findOrCreateCategory(
      guild, cat.name, catOverwrites, !confirm, cat.oldNames
    );
    if (catOutcome === 'created') created.push(`category "${cat.name}"`);
    else if (catOutcome === 'renamed') renamed.push(`category "${cat.oldNames?.[0]}" -> "${cat.name}"`);
    else existing.push(`category "${cat.name}"`);

    for (const ch of cat.channels) {
      // A channel with its own visibleTo narrows below the category default
      // (e.g. Barracks under Staff, visible to Highway Patrol + Director but
      // not Branch Director). A `readOnly` channel keeps the category's
      // visibility but denies SendMessages to everyone except staff. Neither
      // set means it just inherits the category as-is. The two aren't
      // currently composable (buildOverwrites only touches ViewChannel,
      // buildReadOnlyOverwrites only touches SendMessages) -- fail loudly
      // here rather than silently dropping readOnly if a future entry ever
      // sets both, since nothing in the type system prevents it.
      if (ch.visibleTo && ch.readOnly) {
        throw new Error(`ChannelSpec "${ch.name}" sets both visibleTo and readOnly -- not supported yet`);
      }
      const chOverwrites = ch.visibleTo
        ? buildOverwrites(everyoneId, ch.visibleTo, cat.visibleTo, roleByName)
        : ch.readOnly
        ? buildReadOnlyOverwrites(everyoneId, STAFF_ROLES_FOR_MODERATION, roleByName)
        : undefined;
      const { channel, outcome: chOutcome } = await findOrCreateTextChannel(
        guild, ch.name, ch.topic, category?.id ?? null, !confirm, ch.oldNames, chOverwrites
      );
      if (chOutcome === 'created') created.push(`#${ch.name}`);
      else if (chOutcome === 'renamed') renamed.push(`#${ch.oldNames?.[0]} -> #${ch.name}`);
      else existing.push(`#${ch.name}`);
      if (ch.key && channel) channelByKey.set(ch.key, channel);
    }
  }

  const verifyChannel = channelByKey.get('verify-here') ?? null;
  const rulesChannel = channelByKey.get('rules') ?? null;
  const faqChannel = channelByKey.get('faq') ?? null;

  // 3. Rules + FAQ embeds -- upsert the bot's own pinned message rather than
  // reposting every run, so re-running /setup never spams the channel.
  await upsertPinnedEmbed(interaction, created, existing, rulesChannel, confirm, RULES_EMBED_TITLE,
    () => buildRulesEmbed(verifyChannel?.id), 'rules message');
  await upsertPinnedEmbed(interaction, created, existing, faqChannel, confirm, FAQ_EMBED_TITLE,
    () => buildFaqEmbed(verifyChannel?.id), 'FAQ message');

  // 4. Server-wide moderation: strip file uploads + thread creation from the
  // public, re-grant uploads to staff (never thread creation -- no one gets
  // that back), protect Director/Branch Director from being @mentioned by
  // the public, and block invite/GIF links via AutoMod.
  await applyModeration(guild, roleByName, confirm, created, renamed, existing);

  // 5. Native Discord onboarding: default channels + one informational
  // prompt. Confirmed live: Community mode + onboarding are already enabled
  // on this guild with rulesChannel/publicUpdatesChannel already set, so no
  // prerequisite dance is needed here -- just the actual config.
  await applyOnboarding(guild, channelByKey, confirm, created, renamed, existing);

  const summary = confirm
    ? [
        created.length ? `**Created:**\n${created.map(c => `- ${c}`).join('\n')}` : null,
        renamed.length ? `**Renamed/re-themed/updated:**\n${renamed.map(c => `- ${c}`).join('\n')}` : null,
        existing.length ? `**Already correct (skipped):**\n${existing.map(c => `- ${c}`).join('\n')}` : null,
      ].filter(Boolean).join('\n\n')
    : [
        '**Dry run -- nothing was changed.** Re-run with `/setup confirm:true` to apply.',
        created.length ? `**Would create:**\n${created.map(c => `- ${c}`).join('\n')}` : null,
        renamed.length ? `**Would rename/re-theme/update:**\n${renamed.map(c => `- ${c}`).join('\n')}` : null,
        existing.length ? `**Already correct:**\n${existing.map(c => `- ${c}`).join('\n')}` : null,
      ].filter(Boolean).join('\n\n');

  // Discord caps a single message at 2000 chars -- this structure stays well
  // under that (a few dozen short lines), but guard it rather than assume.
  await interaction.editReply(summary.slice(0, 1990));
}

async function upsertPinnedEmbed(
  interaction: ChatInputCommandInteraction, created: string[], existing: string[],
  channel: TextChannel | NewsChannel | null, confirm: boolean, title: string,
  buildEmbed: () => import('discord.js').EmbedBuilder, label: string
): Promise<void> {
  if (!confirm) {
    (channel ? existing : created).push(label);
    return;
  }
  if (!channel) return;
  const embed = buildEmbed();
  const pins = await channel.messages.fetchPinned();
  const existingMsg = pins.find(m => m.author.id === interaction.client.user.id && m.embeds[0]?.title === title);
  if (existingMsg) {
    await existingMsg.edit({ embeds: [embed] });
    existing.push(label);
  } else {
    const sent = await channel.send({ embeds: [embed] });
    try {
      await sent.pin();
    } catch (err) {
      // If pinning fails after the send succeeded (pin cap, a transient
      // error), a stray unpinned message would be invisible to the
      // fetchPinned() lookup above on the next run -- it'd never be found as
      // "existing," so every retry would send (and try to pin) yet another
      // copy, accumulating orphaned duplicates. Roll back the send so this
      // stays atomic: either both succeed, or neither persists.
      await sent.delete().catch(() => {});
      throw err;
    }
    created.push(label);
  }
}

/** Reverse-lookup a PermissionFlagsBits value back to its string key, needed
 * for PermissionOverwrites#edit(), which takes { [key: PermissionString]: ... }. */
function permissionFlagName(flag: bigint): keyof typeof PermissionFlagsBits {
  const found = (Object.entries(PermissionFlagsBits) as [keyof typeof PermissionFlagsBits, bigint][])
    .find(([, value]) => value === flag);
  if (!found) throw new Error(`Unknown permission flag: ${flag}`);
  return found[0];
}

async function applyModeration(
  guild: NonNullable<ChatInputCommandInteraction['guild']>, roleByName: Map<string, Role>, confirm: boolean,
  created: string[], renamed: string[], existing: string[]
): Promise<void> {
  // -- Base permissions: strip file uploads + thread creation from @everyone.
  // File uploads come back for staff below; thread creation does not -- "no
  // one should be able to start threads in ANY channel" had no staff exception.
  const everyone = guild.roles.everyone;
  const toStrip = [...RESTRICTED_BASE_PERMISSIONS, ...NO_THREAD_PERMISSIONS];
  if (toStrip.some(p => everyone.permissions.has(p))) {
    if (confirm) await everyone.setPermissions(everyone.permissions.remove(toStrip));
    renamed.push('@everyone: removed file-upload + thread-creation permissions');
  } else {
    existing.push('@everyone: file uploads + threads already restricted');
  }

  for (const roleName of STAFF_ROLES_FOR_MODERATION) {
    const role = roleByName.get(roleName);
    if (!role) continue;
    const wantsAttach = !role.permissions.has(RESTRICTED_BASE_PERMISSIONS);
    const wantsMentionEveryone = MENTION_EVERYONE_ROLES.includes(roleName)
      && !role.permissions.has('MentionEveryone');
    if (wantsAttach || wantsMentionEveryone) {
      if (confirm) {
        let perms = role.permissions;
        if (wantsAttach) perms = perms.add(RESTRICTED_BASE_PERMISSIONS);
        if (wantsMentionEveryone) perms = perms.add('MentionEveryone');
        await role.setPermissions(perms);
      }
      renamed.push(`role "${roleName}": granted staff permissions (uploads/mention-everyone)`);
    } else {
      existing.push(`role "${roleName}": staff permissions already granted`);
    }
  }

  // -- Audit every channel for a pre-existing per-channel ALLOW overwrite on
  // thread creation. Removing the base @everyone permission above doesn't
  // help if some channel already carries a manual role/member-level ALLOW
  // for it (Discord's per-channel overwrites beat base role permissions) --
  // "no one should be able to start threads in ANY channel" means this needs
  // checking directly, not just assumed from the base-permission change.
  for (const channel of guild.channels.cache.values()) {
    if (!('permissionOverwrites' in channel)) continue;
    for (const overwrite of channel.permissionOverwrites.cache.values()) {
      const leaking = NO_THREAD_PERMISSIONS.filter(p => overwrite.allow.has(p));
      if (!leaking.length) continue;
      if (confirm) {
        await overwrite.edit(Object.fromEntries(leaking.map(p => [permissionFlagName(p), null])));
      }
      renamed.push(`#${channel.name}: cleared a channel-level thread-creation allow`);
    }
  }

  // -- Mention protection: Director/Branch Director can't be pinged by the public.
  for (const roleName of UNMENTIONABLE_ROLES) {
    const role = roleByName.get(roleName);
    if (!role) continue;
    if (role.mentionable) {
      if (confirm) await role.setMentionable(false);
      renamed.push(`role "${roleName}": made unmentionable to the public`);
    } else {
      existing.push(`role "${roleName}": already unmentionable`);
    }
  }

  // -- AutoMod: block invite links + GIF-hosting links, exempting staff.
  const existingRules = await guild.autoModerationRules.fetch();
  const exemptRoleIds = STAFF_ROLES_FOR_MODERATION
    .map(n => roleByName.get(n)?.id)
    .filter((id): id is string => Boolean(id));

  for (const spec of AUTOMOD_RULES) {
    const already = existingRules.find(r => r.name === spec.name);
    if (already) {
      existing.push(`AutoMod rule "${spec.name}"`);
      continue;
    }
    if (confirm) {
      await guild.autoModerationRules.create({
        name: spec.name,
        eventType: AutoModerationRuleEventType.MessageSend,
        triggerType: AutoModerationRuleTriggerType.Keyword,
        triggerMetadata: {
          regexPatterns: spec.regexPatterns,
          keywordFilter: spec.keywordFilter,
        },
        actions: [{
          type: AutoModerationActionType.BlockMessage,
          metadata: { customMessage: spec.blockMessage },
        }],
        enabled: true,
        exemptRoles: exemptRoleIds,
      });
    }
    created.push(`AutoMod rule "${spec.name}"`);
  }
}

/**
 * Converts a live GuildOnboardingPrompt back into the *Data shape
 * editOnboarding expects, unchanged -- used to round-trip every OTHER prompt
 * (anything not matching ONBOARDING_PROMPT.title) so this bot's one managed
 * prompt can be updated without wiping any prompt an admin configured by
 * hand through Discord's own UI. guild.editOnboarding is a full-replace PUT
 * (confirmed against discord.js's real implementation), not a merge -- so
 * every prompt/default-channel that should survive MUST be resent explicitly.
 */
function promptToData(p: import('discord.js').GuildOnboardingPrompt): GuildOnboardingPromptData {
  return {
    id: p.id,
    title: p.title,
    singleSelect: p.singleSelect,
    required: p.required,
    inOnboarding: p.inOnboarding,
    type: p.type,
    options: [...p.options.values()].map(o => ({
      id: o.id,
      title: o.title,
      description: o.description,
      emoji: o.emoji,
      channels: [...o.channels.keys()],
      roles: [...o.roles.keys()],
    })),
  };
}

async function applyOnboarding(
  guild: NonNullable<ChatInputCommandInteraction['guild']>, channelByKey: Map<string, TextChannel | NewsChannel>,
  confirm: boolean, created: string[], renamed: string[], existing: string[]
): Promise<void> {
  // Runs first and unconditionally -- independent of the onboarding
  // prompt/default-channel logic below, which can legitimately bail out
  // early (e.g. a dry run against a not-yet-provisioned server). This check
  // must not be skipped just because that other logic didn't get to run.
  const announcements = channelByKey.get('announcements');
  if (!announcements) {
    existing.push('guild: publicUpdatesChannel check skipped (#announcements doesn\'t exist yet)');
  } else if (guild.publicUpdatesChannelId !== announcements.id) {
    if (confirm) await guild.edit({ publicUpdatesChannel: announcements.id });
    renamed.push('guild: publicUpdatesChannel -> #announcements');
  } else {
    existing.push('guild: publicUpdatesChannel already correct');
  }

  const ourDefaultChannels = ONBOARDING_DEFAULT_CHANNEL_KEYS
    .map(k => channelByKey.get(k))
    .filter((c): c is TextChannel | NewsChannel => Boolean(c));
  const label = `onboarding: default channels + "${ONBOARDING_PROMPT.title}" prompt`;

  if (ourDefaultChannels.length !== ONBOARDING_DEFAULT_CHANNEL_KEYS.length) {
    // Some default channels don't exist yet (channel creation above must have
    // failed) -- skip rather than submit an incomplete/wrong onboarding config.
    existing.push('onboarding: skipped (not all default channels exist yet)');
    return;
  }

  // Fetch + diff in BOTH modes -- a dry run should report what would actually
  // change, not just "channels exist so we're done," and a confirmed run
  // should skip the write entirely (and skip logging a change) if nothing
  // is actually different, same discipline as every other resource above.
  const current = await guild.fetchOnboarding();
  const existingPrompt = [...current.prompts.values()].find(p => p.title === ONBOARDING_PROMPT.title);

  const promptMatches = Boolean(existingPrompt)
    && existingPrompt!.singleSelect === ONBOARDING_PROMPT.singleSelect
    && existingPrompt!.required === ONBOARDING_PROMPT.required
    && existingPrompt!.inOnboarding === ONBOARDING_PROMPT.inOnboarding
    && existingPrompt!.type === ONBOARDING_PROMPT.type
    && existingPrompt!.options.size === ONBOARDING_PROMPT.options.length
    && ONBOARDING_PROMPT.options.every(o => {
      const target = channelByKey.get(o.channelKey);
      return [...existingPrompt!.options.values()].some(eo => eo.title === o.title
        && eo.description === (o.description ?? null)
        // eo.emoji is a live Emoji object (name = the raw unicode character
        // for a standard emoji, id = null); o.emoji is the plain unicode
        // string ONBOARDING_PROMPT declares it as -- compare by name.
        && (eo.emoji?.name ?? null) === (o.emoji ?? null)
        // Discord requires >=1 role or channel per option (confirmed live:
        // "ROLE_OR_CHANNEL_REQUIRED"); each option points at verify-here.
        && Boolean(target) && eo.channels.has(target!.id));
    });
  // We only require OUR default channels to be present -- other admin-added
  // ones are left alone, so their presence/absence doesn't count as "different."
  const defaultChannelsMatch = ourDefaultChannels.every(c => current.defaultChannels.has(c.id));
  const upToDate = current.enabled && current.mode === GuildOnboardingMode.OnboardingDefault
    && promptMatches && defaultChannelsMatch;

  if (upToDate) {
    existing.push(label);
  } else if (!confirm) {
    created.push(label);
  } else {
    const promptData: GuildOnboardingPromptData = {
      ...(existingPrompt ? { id: existingPrompt.id } : {}),
      title: ONBOARDING_PROMPT.title,
      singleSelect: ONBOARDING_PROMPT.singleSelect,
      required: ONBOARDING_PROMPT.required,
      inOnboarding: ONBOARDING_PROMPT.inOnboarding,
      type: ONBOARDING_PROMPT.type,
      options: ONBOARDING_PROMPT.options.map(o => {
        const existingOption = existingPrompt?.options.find(eo => eo.title === o.title);
        const target = channelByKey.get(o.channelKey);
        if (!target) throw new Error(`onboarding option "${o.title}" needs channel "${o.channelKey}", which doesn't exist`);
        return {
          ...(existingOption ? { id: existingOption.id } : {}),
          title: o.title,
          description: o.description,
          emoji: o.emoji,
          // Discord requires every option to carry >=1 role or channel --
          // verify-here is already a default channel visible to everyone
          // regardless of which option they pick, so this satisfies the
          // requirement without granting anything new (see onboarding.ts).
          channels: [target.id],
        };
      }),
    };
    // Merge: every other existing prompt survives unchanged; ours is
    // replaced/updated in place (by id, when it already existed).
    const otherPrompts = [...current.prompts.values()]
      .filter(p => p.id !== existingPrompt?.id)
      .map(promptToData);
    const mergedPrompts = [...otherPrompts, promptData];

    // Same merge principle for default channels: keep any other existing
    // default channel, on top of ensuring ours are present. IDs, not channel
    // objects -- GuildChannel is broader than the ChannelResolvable union
    // editOnboarding expects, but a plain snowflake always resolves fine.
    const ourIds = new Set(ourDefaultChannels.map(c => c.id));
    const mergedDefaultChannels = [
      ...[...current.defaultChannels.keys()].filter(id => !ourIds.has(id)),
      ...ourDefaultChannels.map(c => c.id),
    ];

    await guild.editOnboarding({
      enabled: true,
      mode: GuildOnboardingMode.OnboardingDefault,
      defaultChannels: mergedDefaultChannels,
      prompts: mergedPrompts,
    });
    renamed.push(label);
  }
}
