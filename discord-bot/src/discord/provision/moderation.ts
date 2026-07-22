import { PermissionFlagsBits } from 'discord.js';
import {
  SERVER_ROLE, SURVEY_TIER_NAMES, CREW_TIER_NAMES, tierRoleName, rotatingBadgeName, Track, Cadence,
} from './structure';

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
// post evidence/screenshots, share links, and reach each other. Used for
// AutoMod exemption and read-only-channel posting rights specifically --
// RESTRICTED_PERMISSIONS_EXEMPT_ROLES below is the broader set for uploads/embeds.
export const STAFF_ROLES_FOR_MODERATION = ['Highway Patrol', 'Director', 'Branch Director'];

// Base guild permissions @everyone loses (removed even if a server template
// granted them by default) and RESTRICTED_PERMISSIONS_EXEMPT_ROLES regain on
// their own role. EmbedLinks blocks link-preview embeds, not the link text
// itself -- Traveler/Highway Worker can still share a raw URL, it just won't
// unfurl into a preview.
export const RESTRICTED_BASE_PERMISSIONS = [PermissionFlagsBits.AttachFiles, PermissionFlagsBits.EmbedLinks];

const CONTRIBUTION_TRACKS: Track[] = ['survey', 'crew'];
const CONTRIBUTION_CADENCES: Cadence[] = ['weekly', 'monthly'];

// Every Survey/Road Crew tier + rotating badge role, generated the same way
// structure.ts generates the roles themselves (tierRoleName/rotatingBadgeName)
// so this can never drift from what tiers.ts actually grants members.
const GENERATED_LEADERBOARD_ROLES: string[] = [];
for (const server of Object.keys(SERVER_ROLE)) {
  SURVEY_TIER_NAMES.forEach((_, i) => GENERATED_LEADERBOARD_ROLES.push(tierRoleName(server, 'survey', i)));
  CREW_TIER_NAMES.forEach((_, i) => GENERATED_LEADERBOARD_ROLES.push(tierRoleName(server, 'crew', i)));
  for (const cadence of CONTRIBUTION_CADENCES) {
    for (const track of CONTRIBUTION_TRACKS) {
      GENERATED_LEADERBOARD_ROLES.push(rotatingBadgeName(server, track, cadence));
    }
  }
}

// Every credit-leaderboard/reward role -- the generated Survey/Crew tiers and
// badges above, vouched Tier A (Highway Supervisor), and the other manually-
// granted reward roles (Supporter, Dispatcher). Deliberately NOT Highway
// Inspector -- this file's own STAFF_ROLES_FOR_MODERATION note and
// structure.ts both already document that role as a privilege-less badge.
export const LEADERBOARD_AND_REWARD_ROLES = [
  '2b2t Highway Supervisor', '6b6t Highway Supervisor',
  'Supporter', 'Dispatcher',
  ...GENERATED_LEADERBOARD_ROLES,
];

// Roles that regain RESTRICTED_BASE_PERMISSIONS (uploads + embeds) -- staff,
// plus everyone in LEADERBOARD_AND_REWARD_ROLES. Deliberately NOT used for
// MENTION_EVERYONE_ROLES/AutoMod/read-only-channel exemption -- those stay
// staff-only.
export const RESTRICTED_PERMISSIONS_EXEMPT_ROLES = [...STAFF_ROLES_FOR_MODERATION, ...LEADERBOARD_AND_REWARD_ROLES];

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
