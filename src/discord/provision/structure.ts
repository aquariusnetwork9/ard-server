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

// Bare per-server display prefix (no " Highway Worker" suffix) -- used to build the
// Survey/Road Crew tier and rotating-badge role names below and the radar channel
// names further down, none of which are part of the trust-tier role family
// SERVER_ROLE (declared later in this file) maps.
const SERVER_PREFIX: Record<string, string> = {
  '2b2t.org': '2b2t',
  '6b6t.org': '6b6t',
};

// Survey (confirmed hazard reports) and Road Crew (completed dispatch repairs) --
// cosmetic contribution ladders siloed per server, same as every trust concept in
// this file. Tiers STACK (every tier earned is kept, never swapped -- see
// discord/dispatch/tiers.ts) and carry ZERO channel/dispatch privilege of their
// own: report/repair volume is never conflated with trust. Thresholds are lifetime
// counts on that one server (ARD's own /credits/<server>/leaderboard is already
// siloed the same way).
export const SURVEY_TIER_NAMES = ['Survey Tech', 'Surveyor', 'Senior Surveyor', 'Chief Surveyor'];
export const CREW_TIER_NAMES = ['Crew Member', 'Crew Leader', 'Foreman', 'Superintendent'];
export const TIER_THRESHOLDS = [10, 20, 50, 100];

const SURVEY_TIER_COLORS = [0x74b9ff, 0x2e86de, 0x1b4f9c, 0x0a2a5e]; // light -> dark blue
const CREW_TIER_COLORS = [0xffb74d, 0xf57c00, 0x9a5b13, 0x5d3a1a];   // light -> dark amber/brown
const ROTATING_BADGE_COLOR = 0xffd700; // gold, for every weekly/monthly top badge

export type Track = 'survey' | 'crew';
export type Cadence = 'weekly' | 'monthly';

/** e.g. tierRoleName('2b2t.org', 'survey', 2) -> "2b2t Senior Surveyor". Shared by
 *  this file's own role generation below and tiers.ts's role-sync job, so the two
 *  can never drift apart on naming. */
export function tierRoleName(server: string, track: Track, tierIndex: number): string {
  const names = track === 'survey' ? SURVEY_TIER_NAMES : CREW_TIER_NAMES;
  return `${SERVER_PREFIX[server]} ${names[tierIndex]}`;
}

/** e.g. rotatingBadgeName('2b2t.org', 'crew', 'weekly') -> "2b2t Weekly Top Crew". */
export function rotatingBadgeName(server: string, track: Track, cadence: Cadence): string {
  const label = track === 'survey' ? 'Scout' : 'Crew';
  const cadenceLabel = cadence === 'weekly' ? 'Weekly' : 'Monthly';
  return `${SERVER_PREFIX[server]} ${cadenceLabel} Top ${label}`;
}

const CADENCES: Cadence[] = ['weekly', 'monthly'];
const TRACKS: Track[] = ['survey', 'crew'];

// The 16 tier roles (4 tiers x 2 tracks x 2 servers) + 8 rotating badges (2
// cadences x 2 tracks x 2 servers) generated from the naming helpers above, so
// there's exactly one place that ever spells out a tier/badge role name.
const GENERATED_CONTRIBUTION_ROLES: RoleSpec[] = [];
for (const server of Object.keys(SERVER_PREFIX)) {
  SURVEY_TIER_NAMES.forEach((_, i) => GENERATED_CONTRIBUTION_ROLES.push({
    name: tierRoleName(server, 'survey', i), color: SURVEY_TIER_COLORS[i], hoist: true,
  }));
  CREW_TIER_NAMES.forEach((_, i) => GENERATED_CONTRIBUTION_ROLES.push({
    name: tierRoleName(server, 'crew', i), color: CREW_TIER_COLORS[i], hoist: true,
  }));
  for (const cadence of CADENCES) {
    for (const track of TRACKS) {
      GENERATED_CONTRIBUTION_ROLES.push({
        name: rotatingBadgeName(server, track, cadence), color: ROTATING_BADGE_COLOR, hoist: true,
      });
    }
  }
}

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
  // Survey/Road Crew tiers + weekly/monthly rotating badges -- see the generation
  // loop above tierRoleName/rotatingBadgeName. Deliberately absent from every
  // CategorySpec.visibleTo below: these are pure badges with no channel or
  // dispatch access of their own.
  ...GENERATED_CONTRIBUTION_ROLES,
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

// Per-server situational-awareness channel, open to that server's Worker rank and
// up -- unlike the dispatch-queue channels above, this is NOT gated to
// DISPATCH_ACCESS_ROLES (see the Dispatch Center category's own visibleTo below,
// which is broadened for exactly these two channels and narrowed back down again
// per-channel for the queue channels). Shared with discord/dispatch/radar.ts the
// same way DISPATCH_CHANNEL_NAMES is shared with poller.ts.
export function radarChannelName(server: string): string {
  return `📡・${SERVER_PREFIX[server]}-radar`;
}

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
  // channel. Omit to just inherit the category's visibility as-is. Composable
  // with `readOnly` (setup.ts's buildComposedOverwrites merges both onto the
  // same role) -- e.g. Dispatch Center's queue channels are both narrower
  // than the category default AND post-only-by-staff.
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
    // Broadened to Worker-and-up so the two radar channels below (situational
    // awareness, everyone's business) can live in this category too -- the
    // actual dispatch-queue channels (open/barracks/closed/records) each carry
    // their own narrower visibleTo below to claw back down to dispatch-roles
    // only; they must NOT inherit this wider category default. See
    // DISPATCH_ACCESS_ROLES's own comment for why Highway Inspector is listed
    // directly here, unlike everywhere else in this file.
    visibleTo: ['2b2t Highway Worker', '6b6t Highway Worker', ...DISPATCH_ACCESS_ROLES, ...STAFF_ROLES],
    channels: [
      {
        name: DISPATCH_CHANNEL_NAMES.open,
        topic: 'Open dispatch targets -- claim one with the button on its post.',
        readOnly: true,
        visibleTo: [...DISPATCH_ACCESS_ROLES, ...STAFF_ROLES],
      },
      {
        name: DISPATCH_CHANNEL_NAMES.barracks,
        topic: 'Coordination for whoever\'s actively out on a claim.',
        visibleTo: [...DISPATCH_ACCESS_ROLES, ...STAFF_ROLES],
      },
      {
        name: DISPATCH_CHANNEL_NAMES.closed,
        topic: 'Recently resolved dispatch targets.',
        readOnly: true,
        visibleTo: [...DISPATCH_ACCESS_ROLES, ...STAFF_ROLES],
      },
      {
        name: DISPATCH_CHANNEL_NAMES.records,
        topic: 'Full dispatch audit log -- every claim, completion, and expiry.',
        readOnly: true,
        visibleTo: [...DISPATCH_ACCESS_ROLES, ...STAFF_ROLES],
      },
      {
        name: radarChannelName('2b2t.org'),
        topic: 'Live 2b2t road-condition situational awareness -- every report, not just the promoted dispatch queue.',
        readOnly: true,
        visibleTo: ['2b2t Highway Worker', '2b2t Highway Supervisor', 'Highway Inspector', 'Dispatcher', ...STAFF_ROLES],
      },
      {
        name: radarChannelName('6b6t.org'),
        topic: 'Live 6b6t road-condition situational awareness -- every report, not just the promoted dispatch queue.',
        readOnly: true,
        visibleTo: ['6b6t Highway Worker', '6b6t Highway Supervisor', 'Highway Inspector', 'Dispatcher', ...STAFF_ROLES],
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
