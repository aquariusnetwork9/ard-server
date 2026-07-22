import {
  ChatInputCommandInteraction,
  Guild,
  PermissionFlagsBits,
  Role,
  CategoryChannel,
  ChannelType,
  TextChannel,
  NewsChannel,
  OverwriteResolvable,
} from 'discord.js';
import { config } from '../../config';

// A channel this bot manages (rules, announcements, etc.) could be a plain
// text channel OR get converted to an Announcement channel later by an admin
// through Discord's own UI (a normal, expected operation) -- both share the
// same TextChannel | NewsChannel API (send/messages/permissionOverwrites/topic/
// setParent), so matching/typing against that common base, not TextChannel
// specifically, means a type conversion doesn't make findOrCreateTextChannel
// blind to the existing channel and create a duplicate alongside it.
const TEXT_LIKE_TYPES = [ChannelType.GuildText, ChannelType.GuildAnnouncement];

/** Returns true if the interaction user can run admin-only commands (/setup). */
export function isAdmin(interaction: ChatInputCommandInteraction): boolean {
  if (config.discord.ownerIds.includes(interaction.user.id)) return true;
  return interaction.memberPermissions?.has(PermissionFlagsBits.Administrator) ?? false;
}

/** Reject non-admins with an ephemeral error reply. Returns true if rejected. */
export async function requireAdmin(interaction: ChatInputCommandInteraction): Promise<boolean> {
  if (isAdmin(interaction)) return false;
  await interaction.reply({ content: 'You need Administrator permission to use this command.', ephemeral: true });
  return true;
}

/**
 * Builds ViewChannel overwrites for a role list: deny @everyone, allow each
 * role in `allowed`, and explicitly deny any role in `scope` (roles that
 * would otherwise inherit visibility, e.g. from a parent category) that isn't
 * in `allowed` -- that's what lets a channel be narrower than its category.
 *
 * If the literal 'everyone' pseudo-role is itself in `allowed`, the channel
 * is meant to be visible to literally everyone -- including every role in
 * `scope` -- so this returns just the single @everyone-allow overwrite and
 * skips the per-role loop entirely. (An earlier version of this function
 * still ran the loop in that case and ended up explicitly DENYING every
 * `scope` role, which would have made a "visible to everyone" channel
 * invisible to exactly the roles it was scoped from -- the opposite of
 * intended. No current ChannelSpec triggers this today since categories
 * special-case 'everyone' before ever calling this helper, but a future
 * channel-level `visibleTo: ['everyone']` would have.)
 */
export function buildOverwrites(
  everyoneId: string, allowed: string[], scope: string[], roleByName: Map<string, Role>
): OverwriteResolvable[] {
  if (allowed.includes('everyone')) {
    return [{ id: everyoneId, allow: [PermissionFlagsBits.ViewChannel] }];
  }
  const overwrites: OverwriteResolvable[] = [{ id: everyoneId, deny: [PermissionFlagsBits.ViewChannel] }];
  const allRoleNames = new Set([...scope, ...allowed]);
  for (const roleName of allRoleNames) {
    const role = roleByName.get(roleName);
    if (!role) continue;
    overwrites.push({
      id: role.id,
      ...(allowed.includes(roleName) ? { allow: [PermissionFlagsBits.ViewChannel] }
                                      : { deny: [PermissionFlagsBits.ViewChannel] }),
    });
  }
  return overwrites;
}

/**
 * Builds SendMessages-only overwrites for a "public can view, can't post"
 * channel: deny @everyone, allow the given staff roles. Deliberately doesn't
 * touch ViewChannel at all -- visibility for a read-only channel is meant to
 * come from wherever it normally would (category default, usually
 * @everyone), this only restricts who can post in it.
 */
export function buildReadOnlyOverwrites(
  everyoneId: string, staffRoleNames: string[], roleByName: Map<string, Role>
): OverwriteResolvable[] {
  const overwrites: OverwriteResolvable[] = [{ id: everyoneId, deny: [PermissionFlagsBits.SendMessages] }];
  for (const roleName of staffRoleNames) {
    const role = roleByName.get(roleName);
    if (role) overwrites.push({ id: role.id, allow: [PermissionFlagsBits.SendMessages] });
  }
  return overwrites;
}

/**
 * Composes buildOverwrites' ViewChannel narrowing with buildReadOnlyOverwrites'
 * SendMessages restriction on the SAME channel -- e.g. the Dispatch Center's
 * queue channels, each both narrower than their (now-broadened) category
 * default AND post-only-by-staff. Calling the two functions above separately
 * and concatenating their arrays would emit two different overwrite entries
 * for the same role id (one from each), which permissionOverwrites.set() can't
 * reconcile -- this merges allow/deny bits per id instead, so every role gets
 * exactly one overwrite entry covering both permissions.
 */
export function buildComposedOverwrites(
  everyoneId: string, allowedView: string[], categoryScope: string[],
  staffRoleNames: string[], roleByName: Map<string, Role>
): OverwriteResolvable[] {
  const perId = new Map<string, { allow: bigint[]; deny: bigint[] }>();
  const bump = (id: string, kind: 'allow' | 'deny', perm: bigint) => {
    const entry = perId.get(id) ?? { allow: [], deny: [] };
    entry[kind].push(perm);
    perId.set(id, entry);
  };

  if (allowedView.includes('everyone')) {
    bump(everyoneId, 'allow', PermissionFlagsBits.ViewChannel);
  } else {
    bump(everyoneId, 'deny', PermissionFlagsBits.ViewChannel);
    for (const roleName of new Set([...categoryScope, ...allowedView])) {
      const role = roleByName.get(roleName);
      if (!role) continue;
      bump(role.id, allowedView.includes(roleName) ? 'allow' : 'deny', PermissionFlagsBits.ViewChannel);
    }
  }

  bump(everyoneId, 'deny', PermissionFlagsBits.SendMessages);
  for (const roleName of staffRoleNames) {
    const role = roleByName.get(roleName);
    if (role) bump(role.id, 'allow', PermissionFlagsBits.SendMessages);
  }

  return [...perId.entries()].map(([id, { allow, deny }]) => ({
    id, ...(allow.length ? { allow } : {}), ...(deny.length ? { deny } : {}),
  }));
}

export type Outcome = 'created' | 'renamed' | 'existing';

/**
 * Finds a role by exact name; failing that, by any name in `oldNames` (an
 * earlier /setup's name(s) for the same rank, checked in order -- if found,
 * it's renamed/recolored in place rather than left behind as a stale
 * duplicate); otherwise creates it.
 */
export async function findOrCreateRole(
  guild: Guild, name: string, color: number, hoist: boolean, dryRun: boolean, oldNames?: string[]
): Promise<{ role: Role | null; outcome: Outcome }> {
  const existing = guild.roles.cache.find(r => r.name === name);
  if (existing) return { role: existing, outcome: 'existing' };

  const stale = (oldNames ?? []).map(n => guild.roles.cache.find(r => r.name === n)).find(Boolean);
  if (stale) {
    if (dryRun) return { role: stale, outcome: 'renamed' };
    const role = await stale.edit({ name, color, hoist, mentionable: false });
    return { role, outcome: 'renamed' };
  }

  if (dryRun) return { role: null, outcome: 'created' };
  const role = await guild.roles.create({ name, color, hoist, mentionable: false });
  return { role, outcome: 'created' };
}

/** Same match-by-name-then-oldNames-then-create shape as findOrCreateRole, for categories. */
export async function findOrCreateCategory(
  guild: Guild, name: string, overwrites: OverwriteResolvable[], dryRun: boolean, oldNames?: string[]
): Promise<{ category: CategoryChannel | null; outcome: Outcome }> {
  const existing = guild.channels.cache.find(
    (c): c is CategoryChannel => c.type === ChannelType.GuildCategory && c.name === name
  );
  if (existing) {
    // Reconcile permissions even when the category already has the right
    // name -- otherwise a later edit to CATEGORIES' visibleTo (or manual
    // drift on the live server) never gets fixed by a re-run, unlike
    // findOrCreateTextChannel's equivalent branch, which does reconcile.
    if (!dryRun) await existing.permissionOverwrites.set(overwrites);
    return { category: existing, outcome: 'existing' };
  }

  const stale = (oldNames ?? [])
    .map(n => guild.channels.cache.find(
      (c): c is CategoryChannel => c.type === ChannelType.GuildCategory && c.name === n))
    .find(Boolean);
  if (stale) {
    if (dryRun) return { category: stale, outcome: 'renamed' };
    const category = await stale.edit({ name, permissionOverwrites: overwrites });
    return { category, outcome: 'renamed' };
  }

  if (dryRun) return { category: null, outcome: 'created' };
  const category = await guild.channels.create({
    name, type: ChannelType.GuildCategory, permissionOverwrites: overwrites,
  });
  return { category, outcome: 'created' };
}

/**
 * Same match-by-name-then-oldNames-then-create shape as findOrCreateRole, for
 * text channels. Matches by name ALONE (not scoped to parentId) -- channel
 * names are unique across this whole structure, never reused between
 * categories, so a name match anywhere in the guild is unambiguous. That
 * matters for self-healing: if a channel ever ends up under the wrong
 * category (e.g. after a messy partial cleanup), this still finds it and
 * moves it to the right parent instead of creating a fresh duplicate there.
 */
export async function findOrCreateTextChannel(
  guild: Guild, name: string, topic: string | undefined, parentId: string | null, dryRun: boolean,
  oldNames?: string[], overwrites?: OverwriteResolvable[]
): Promise<{ channel: TextChannel | NewsChannel | null; outcome: Outcome }> {
  const existing = guild.channels.cache.find(
    (c): c is TextChannel | NewsChannel => TEXT_LIKE_TYPES.includes(c.type) && c.name === name
  );
  if (existing) {
    if (!dryRun) {
      if (existing.parentId !== parentId) await existing.setParent(parentId, { lockPermissions: false });
      if (overwrites) await existing.permissionOverwrites.set(overwrites);
    }
    return { channel: existing, outcome: 'existing' };
  }

  const stale = (oldNames ?? [])
    .map(n => guild.channels.cache.find(
      (c): c is TextChannel | NewsChannel => TEXT_LIKE_TYPES.includes(c.type) && c.name === n))
    .find(Boolean);
  if (stale) {
    if (dryRun) return { channel: stale, outcome: 'renamed' };
    const channel = await stale.edit({ name, topic, parent: parentId ?? undefined });
    if (overwrites) await channel.permissionOverwrites.set(overwrites);
    return { channel, outcome: 'renamed' };
  }

  if (dryRun) return { channel: null, outcome: 'created' };
  const channel = await guild.channels.create({
    name, type: ChannelType.GuildText, topic, parent: parentId ?? undefined,
    permissionOverwrites: overwrites,
  });
  return { channel, outcome: 'created' };
}
