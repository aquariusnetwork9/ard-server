import { PermissionFlagsBits } from 'discord.js';

/**
 * Server-wide (not per-channel) content/permission restrictions for the
 * "public" ranks -- everyone below Highway Patrol. Deliberately implemented
 * as base role permissions + AutoMod rules rather than per-channel
 * overwrites: these are meant to apply everywhere, including channels that
 * don't exist yet, and per-channel overwrites would need to be repeated on
 * every single channel (and re-applied on every future one) to mean the same
 * thing.
 */

// Roles exempt from the public restrictions below -- staff need to be able to
// post evidence/screenshots, share links, and reach each other.
export const STAFF_ROLES_FOR_MODERATION = ['Highway Patrol', 'Director', 'Branch Director'];

// Base guild permissions @everyone loses (removed even if a server template
// granted them by default) and the staff roles above regain on their own role.
export const RESTRICTED_BASE_PERMISSIONS = [PermissionFlagsBits.AttachFiles];

// Thread creation, removed from @everyone with NO staff exemption -- "no one
// should be able to start threads in ANY channel" was explicit and, unlike
// the other restrictions above, didn't carve out a staff exception.
export const NO_THREAD_PERMISSIONS = [PermissionFlagsBits.CreatePublicThreads, PermissionFlagsBits.CreatePrivateThreads];

// Lets holders @mention roles that have been marked unmentionable (below) --
// granted to staff so Highway Patrol can still escalate to Director/Branch
// Director even though the public can't ping them.
export const MENTION_EVERYONE_ROLES = ['Highway Patrol', 'Director', 'Branch Director'];

// Roles the public may not @mention -- deliberately NOT Highway Patrol, which
// stays pingable by anyone (that's how the public actually reaches staff).
export const UNMENTIONABLE_ROLES = ['Director', 'Branch Director'];

export interface AutoModRuleSpec {
  name: string;
  regexPatterns?: string[];
  keywordFilter?: string[];
  blockMessage: string;
}

// Discord invite links are blocked via regex (any discord.gg/... or
// discord.com/invite/... link); GIF-hosting links via a wildcard keyword
// filter, since AutoMod's keyword matching supports leading/trailing `*`.
// Both exempt the staff roles above (AutoModerationRuleManager.create's
// exemptRoles), computed at apply-time once role IDs are known.
export const AUTOMOD_RULES: AutoModRuleSpec[] = [
  {
    name: 'Block Discord invite links',
    regexPatterns: [
      'discord\\.gg\\/[a-zA-Z0-9-]+',
      'discord(?:app)?\\.com\\/invite\\/[a-zA-Z0-9-]+',
    ],
    blockMessage: "Discord invite links aren't allowed here.",
  },
  {
    name: 'Block GIF-hosting links',
    keywordFilter: ['*tenor.com*', '*giphy.com*', '*gfycat.com*'],
    blockMessage: "Links to GIF sites aren't allowed here.",
  },
];
