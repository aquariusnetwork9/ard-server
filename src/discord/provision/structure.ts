/**
 * Declarative server structure -- the "content" /setup applies. Kept separate
 * from the command that walks it so the actual roles/channels/copy can be
 * reviewed and tweaked without touching command logic.
 *
 * Rank theme, mapped from ARD's own trust tiers (PROTOCOL.md SS6) onto Discord
 * roles:
 *   Tier C (anonymous)         -> Traveler        -- default on join, auto-removed once linked
 *   Tier B (linked identity)   -> Highway Worker   -- granted by /link, per server
 *   Tier A (vouched/full)      -> Highway Supervisor -- Owner-issued, per server, manual
 *   Tier M (maintainer)        -> Highway Inspector  -- Owner-issued, badge only, no channel grants
 *   moderator scope            -> Highway Patrol   -- manual
 *   admin scope / server owner -> Director          -- manual, owner-assigned only
 *   (no ARD equivalent)        -> Branch Director   -- manual, group/crew leadership
 *   (no ARD equivalent)        -> Dispatcher         -- manual, staff-granted; a vetted Highway
 *                                                       Worker earns dispatch-queue access this
 *                                                       way, same as Tier A/M already have via
 *                                                       their own rank (PROTOCOL.md SS6.7)
 *
 * Highway Worker and Highway Supervisor split per-server (2b2t/6b6t) because
 * ARD's own Tier A/B grants are strictly per-server -- someone verified on
 * 2b2t.org has zero standing on 6b6t.org by design (PROTOCOL.md SS6).
 * Everything else is a Discord-side rank with no server-specific ARD meaning.
 */

export interface RoleSpec {
  name: string;
  color: number;
  hoist: boolean;
  // Name(s) a role /setup previously created this rank under -- checked in
  // order if the current name isn't found, so it's renamed/recolored in place
  // instead of leaving a stale duplicate behind. Covers "never renamed yet"
  // and "renamed once already" without needing to know which happened.
  oldNames?: string[];
}

export const TRAVELER_ROLE = 'Traveler';

export const ROLES: RoleSpec[] = [
  { name: TRAVELER_ROLE, color: 0x99aab5, hoist: false },
  { name: '2b2t Highway Worker', color: 0x57f287, hoist: true, oldNames: ['2b2t Verified'] },
  { name: '6b6t Highway Worker', color: 0x5865f2, hoist: true, oldNames: ['6b6t Verified'] },
  { name: '2b2t Highway Supervisor', color: 0x1f8b4c, hoist: true },
  { name: '6b6t Highway Supervisor', color: 0x206694, hoist: true },
  { name: 'Highway Inspector', color: 0xf1c40f, hoist: true },
  // Manual, staff-granted only -- deliberately no self-serve command (see
  // ard-server PROTOCOL.md SS6.7). A Highway Worker who's earned this can
  // claim/complete dispatch targets in Dispatch Center; Tier A/M members
  // never need it themselves (their existing Supervisor/Inspector rank
  // already grants Dispatch Center access -- see DISPATCH_ACCESS_ROLES).
  { name: 'Dispatcher', color: 0x11cdef, hoist: true },
  { name: 'Highway Patrol', color: 0xed4245, hoist: true, oldNames: ['Moderator'] },
  { name: 'Director', color: 0x9b59b6, hoist: true },
  { name: 'Branch Director', color: 0x71368a, hoist: true },
];

// Who can see/use the Dispatch Center category (structure below): every
// per-server Tier A rank plus Tier M's own badge role -- Highway Inspector is
// deliberately listed here even though CATEGORIES below otherwise follows the
// "Inspector carries no channel grants of its own" convention (see the module
// note above and moderation.ts's STAFF_ROLES) -- dispatch access specifically
// IS an Inspector's own real capability (ard-server's bot-mediated auth
// treats Tier A/M as having default access), not something they only get
// through a Worker/Supervisor role held on the side.
export const DISPATCH_ACCESS_ROLES = [
  '2b2t Highway Supervisor', '6b6t Highway Supervisor', 'Highway Inspector', 'Dispatcher',
];

// Exact channel names -- shared between the CATEGORIES declaration below and
// the dispatch poller (discord/dispatch/poller.ts), which looks these up by
// name at runtime independent of any /setup run. One definition so the two
// can never drift apart.
export const DISPATCH_CHANNEL_NAMES = {
  open: '🎯・open',
  barracks: '🪖・dispatch-barracks',
  closed: '📦・closed',
  records: '📜・records',
} as const;

// Maps the ARD `server` field (from /link/bot-complete's response) to the
// Discord role /link grants automatically.
export const SERVER_ROLE: Record<string, string> = {
  '2b2t.org': '2b2t Highway Worker',
  '6b6t.org': '6b6t Highway Worker',
};

export interface ChannelSpec {
  name: string;
  topic?: string;
  // Stable identifier for channels the bot needs to find again by role, not
  // display name -- the display name carries an emoji prefix that's free to
  // change without breaking the lookup.
  key?: 'rules' | 'announcements' | 'verify-here' | 'faq';
  // See RoleSpec.oldNames -- same idea, checked within the same category.
  oldNames?: string[];
  // Narrows visibility below the category default -- roles in the category's
  // own visibleTo that aren't listed here are explicitly denied on this one
  // channel. Omit to just inherit the category's visibility as-is. Mutually
  // exclusive with `readOnly` (visibility vs. who can post are separate axes,
  // but nothing here currently needs both narrowed at once).
  visibleTo?: string[];
  // Anyone who can view this channel may still not post in it, except the
  // staff roles in moderation.ts's STAFF_ROLES_FOR_MODERATION.
  readOnly?: boolean;
}

export interface CategorySpec {
  name: string;
  // Role names that can view this category; 'everyone' means @everyone (no
  // overwrite needed, Discord's default). Anything else denies @everyone and
  // grants exactly these roles view access. Highway Inspector is deliberately
  // never listed -- it's a badge with "no special privileges," so it carries
  // no channel grants of its own; an Inspector's actual access comes from
  // whichever Worker/Supervisor role they also hold.
  visibleTo: string[];
  channels: ChannelSpec[];
  // See RoleSpec.oldNames.
  oldNames?: string[];
}

const STAFF_ROLES = ['Highway Patrol', 'Director', 'Branch Director'];

export const CATEGORIES: CategorySpec[] = [
  {
    name: '🚦 Welcome',
    oldNames: ['Welcome'],
    visibleTo: ['everyone'],
    channels: [
      { name: '🛑・rules', topic: 'Read before posting anywhere else.', key: 'rules', oldNames: ['rules'] },
      { name: '📢・announcements', topic: 'Network status + community news.', key: 'announcements', oldNames: ['announcements'] },
      { name: '🪪・verify-here', topic: 'Run /link <code> here to link your Minecraft account and become a Highway Worker.', key: 'verify-here', oldNames: ['verify-here'] },
      { name: '❓・faq', topic: 'Read-only -- answers to common questions.', key: 'faq', readOnly: true },
    ],
  },
  {
    name: '🛣️ Community',
    oldNames: ['Community'],
    visibleTo: ['2b2t Highway Worker', '6b6t Highway Worker',
                '2b2t Highway Supervisor', '6b6t Highway Supervisor', ...STAFF_ROLES],
    channels: [
      { name: '💬・general', oldNames: ['general'] },
      { name: '📸・media', oldNames: ['media'] },
      { name: '🛠️・support', oldNames: ['support'] },
    ],
  },
  {
    name: '2️⃣ 2b2t',
    oldNames: ['2b2t'],
    visibleTo: ['2b2t Highway Worker', '2b2t Highway Supervisor', ...STAFF_ROLES],
    channels: [
      { name: '🗨️・2b2t-general', oldNames: ['2b2t-general'] },
      { name: '🗺️・2b2t-highway-map', topic: 'https://map.aquariusconnect.org', oldNames: ['2b2t-highway-map'] },
      { name: '🆘・2b2t-help', oldNames: ['2b2t-help'] },
    ],
  },
  {
    name: '6️⃣ 6b6t',
    oldNames: ['6b6t'],
    visibleTo: ['6b6t Highway Worker', '6b6t Highway Supervisor', ...STAFF_ROLES],
    channels: [
      { name: '🗨️・6b6t-general', oldNames: ['6b6t-general'] },
      { name: '🗺️・6b6t-highway-map', topic: 'https://map.aquariusconnect.org', oldNames: ['6b6t-highway-map'] },
      { name: '🆘・6b6t-help', oldNames: ['6b6t-help'] },
    ],
  },
  {
    name: '🛰️ Dispatch Center',
    // See DISPATCH_ACCESS_ROLES's own comment for why Highway Inspector is
    // listed directly here, unlike everywhere else in this file.
    visibleTo: [...DISPATCH_ACCESS_ROLES, ...STAFF_ROLES],
    channels: [
      {
        name: DISPATCH_CHANNEL_NAMES.open,
        topic: 'Open dispatch targets -- claim one with the button on its post.',
        readOnly: true,
      },
      {
        name: DISPATCH_CHANNEL_NAMES.barracks,
        topic: 'Coordination for whoever\'s actively out on a claim.',
      },
      {
        name: DISPATCH_CHANNEL_NAMES.closed,
        topic: 'Recently resolved dispatch targets.',
        readOnly: true,
      },
      {
        name: DISPATCH_CHANNEL_NAMES.records,
        topic: 'Full dispatch audit log -- every claim, completion, and expiry.',
        readOnly: true,
      },
    ],
  },
  {
    name: '🚓 Staff',
    oldNames: ['Staff'],
    visibleTo: STAFF_ROLES,
    channels: [
      {
        name: '🛡️・barracks', topic: 'Highway Patrol + Director coordination.',
        oldNames: ['🚨・mod-chat', 'mod-chat'],
        visibleTo: ['Highway Patrol', 'Director'],
      },
      {
        name: '📋・mod-log', topic: 'Wick posts moderation actions here once installed.',
        oldNames: ['mod-log'],
      },
      {
        name: '🧭・branch-directors', topic: 'Branch Director + Director coordination.',
        visibleTo: ['Branch Director', 'Director'],
      },
      {
        name: '🏛️・headquarters', topic: 'Director-only.',
        visibleTo: ['Director'],
      },
    ],
  },
];
