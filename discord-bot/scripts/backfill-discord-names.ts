/**
 * One-off migration: resolves and fills in `discordUsername` on ARD for every
 * linked identity that predates username capture (see ard-server's
 * identity.py -- complete_link only started storing a name once the OAuth/bot
 * link flows started passing one; anyone linked before that has NULL).
 *
 * The bot is the only thing that can do this at all: resolving an arbitrary
 * Discord id into a display name needs a real bot token (client.users.fetch),
 * which ARD itself never holds -- its own OAuth exchange can only ever learn
 * the name of whoever is completing that exact link, not an arbitrary id from
 * the roster.
 *
 * Read-only unless run with --confirm (same convention as find-duplicates.ts):
 * without it, this only logs what it WOULD backfill.
 */
import { Client, GatewayIntentBits } from 'discord.js';
import { config } from '../src/config';
import { SERVER_ROLE } from '../src/discord/provision/structure';
import { listIdentities, backfillNames } from '../src/ard-client';

const CONFIRM = process.argv.includes('--confirm');

async function main(): Promise<void> {
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(config.discord.token);
  await new Promise<void>(resolve => client.once('ready', () => resolve()));

  for (const server of Object.keys(SERVER_ROLE)) {
    const identities = await listIdentities(server);
    const missing = identities.filter(id => !id.discordUsername);
    console.log(`\n--- ${server}: ${identities.length} linked, ${missing.length} missing a name ---`);
    if (!missing.length) continue;

    const resolved: Record<string, string> = {};
    for (const id of missing) {
      try {
        const user = await client.users.fetch(id.discordId);
        resolved[id.discordId] = user.displayName;
        console.log(`  ${id.discordId} -> "${user.displayName}"`);
      } catch (e) {
        // Left Discord, deleted account, or an id that never was one -- skip it,
        // it just stays nameless (shows as the bare id in the dashboard).
        console.log(`  ${id.discordId} -> unresolvable (${(e as Error).message})`);
      }
    }

    const resolvedCount = Object.keys(resolved).length;
    if (!resolvedCount) continue;
    if (!CONFIRM) {
      console.log(`  (dry run -- would backfill ${resolvedCount} name(s); re-run with --confirm to apply)`);
      continue;
    }
    const updated = await backfillNames(server, resolved);
    console.log(`  backfilled ${updated}/${resolvedCount} on ARD`);
  }

  await client.destroy();
}

main().catch(err => { console.error(err); process.exit(1); });
