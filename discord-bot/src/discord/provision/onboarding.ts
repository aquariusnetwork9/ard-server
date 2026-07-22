import { GuildOnboardingPromptType } from 'discord.js';

/**
 * Native Discord Server Onboarding config -- shown to brand-new members
 * during the join flow. Deliberately informational only: no prompt option
 * here grants a role. Highway Worker (verified) access only ever comes
 * through /link -- an onboarding prompt is NOT an alternate path around
 * that, since Discord's onboarding role grants would bypass ARD's own
 * account-linking verification entirely if used that way.
 *
 * Discord's API requires every prompt option to carry at least one role OR
 * channel (confirmed live: submitting neither fails with "Invalid Form Body
 * / ROLE_OR_CHANNEL_REQUIRED"). Since roles are off the table for the reason
 * above, every option instead points at `channelKey: 'verify-here'` --
 * already a default channel visible to every member regardless of which
 * option they pick, so listing it here grants nothing NEW; it just satisfies
 * Discord's schema and doubles as a nudge toward where you'd actually act on
 * the interest you just expressed.
 */
export const ONBOARDING_DEFAULT_CHANNEL_KEYS: Array<'rules' | 'announcements' | 'verify-here' | 'faq'> = [
  'rules', 'announcements', 'verify-here', 'faq',
];

export const ONBOARDING_PROMPT = {
  title: 'Which community are you here for?',
  singleSelect: false,
  required: false,
  inOnboarding: true,
  type: GuildOnboardingPromptType.MultipleChoice,
  options: [
    { title: '2b2t', description: 'The original anarchy server', emoji: '2️⃣', channelKey: 'verify-here' as const },
    { title: '6b6t', description: 'The 6b6t anarchy server', emoji: '6️⃣', channelKey: 'verify-here' as const },
    { title: 'Just looking around', description: 'Not sure yet', emoji: '👀', channelKey: 'verify-here' as const },
  ],
};
