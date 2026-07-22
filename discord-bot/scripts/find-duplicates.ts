/**
 * Standalone diagnostic/cleanup: logs in with the bot token, fetches the
 * guild's actual current state directly from Discord's API, and finds two
 * distinct kinds of mess:
 *
 *   1. Exact-name duplicates (same name existing more than once) -- keeps
 *      the oldest (lowest snowflake) of each, flags the rest.
 *   2. Stale old-named strays: for every ROLES/CATEGORIES entry that has
 *      `oldNames`, if BOTH the current name and one of its old names exist
 *      as separate live objects, the old-named one is a leftover from a
 *      stale process that never got the memo -- flagged for removal too.
 *
 * Read-only unless run with --confirm. Only ever touches things whose name
 * (current or listed as an oldName) actually appears in structure.ts --
 * anything else on the server (other bots' roles, pre-existing channels
 * like #public/Voice Channels) is left alone.
 */
import { Client, GatewayIntentBits, ChannelType } from 'discord.js';
import { config } from '../src/config';
import { ROLES, CATEGORIES } from '../src/discord/provision/structure';

const CONFIRM = process.argv.includes('--confirm');

function oldestFirst<T extends { id: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1));
}

function exactDupes<T extends { id: string; name: string }>(items: T[]): T[] {
  const byName = new Map<string, T[]>();
  for (const item of items) {
    const list = byName.get(item.name) ?? [];
    list.push(item);
    byName.set(item.name, list);
  }
  const dupes: T[] = [];
  for (const list of byName.values()) {
    if (list.length < 2) continue;
    dupes.push(...oldestFirst(list).slice(1));
  }
  return dupes;
}

async function main(): Promise<void> {
  const client = new Client({ intents: [GatewayIntentBits.Guilds] });
  await client.login(config.discord.token);
  await new Promise<void>(resolve => client.once('ready', () => resolve()));

  const guild = await client.guilds.fetch(config.discord.guildId);
  await guild.channels.fetch();
  await guild.roles.fetch();

  const categories = [...guild.channels.cache.values()].filter(c => c.type === ChannelType.GuildCategory);
  const textChannels = [...guild.channels.cache.values()].filter(c => c.type === ChannelType.GuildText);
  const roles = [...guild.roles.cache.values()].filter(r => r.id !== guild.id);

  console.log(`Guild "${guild.name}": ${categories.length} categories, ${textChannels.length} text channels, ${roles.length} roles (excl. @everyone)\n`);

  const toDelete: { kind: string; name: string; id: string; reason: string }[] = [];

  // -- Pass 1: exact-name duplicates
  for (const c of exactDupes(categories)) toDelete.push({ kind: 'category', name: c.name, id: c.id, reason: 'exact duplicate' });
  for (const c of exactDupes(textChannels)) toDelete.push({ kind: 'channel', name: c.name, id: c.id, reason: 'exact duplicate' });
  for (const r of exactDupes(roles)) toDelete.push({ kind: 'role', name: r.name, id: r.id, reason: 'exact duplicate' });

  // -- Pass 2: stale old-named strays (current name AND an old name both exist)
  const alreadyFlagged = new Set(toDelete.map(d => d.id));

  function flagStale(currentExists: boolean, oldNames: string[] | undefined, matches: { id: string; name: string }[]) {
    if (!currentExists || !oldNames?.length) return [];
    const found: { id: string; name: string }[] = [];
    for (const m of matches) {
      if (oldNames.includes(m.name) && !alreadyFlagged.has(m.id)) {
        alreadyFlagged.add(m.id);
        found.push(m);
      }
    }
    return found;
  }

  for (const spec of ROLES) {
    const currentExists = roles.some(r => r.name === spec.name);
    for (const stale of flagStale(currentExists, spec.oldNames, roles)) {
      toDelete.push({ kind: 'role', name: stale.name, id: stale.id, reason: `stale old name of "${spec.name}"` });
    }
  }
  for (const cat of CATEGORIES) {
    const currentExists = categories.some(c => c.name === cat.name);
    for (const stale of flagStale(currentExists, cat.oldNames, categories)) {
      toDelete.push({ kind: 'category', name: stale.name, id: stale.id, reason: `stale old name of "${cat.name}"` });
    }
    for (const ch of cat.channels) {
      const chCurrentExists = textChannels.some(c => c.name === ch.name);
      for (const stale of flagStale(chCurrentExists, ch.oldNames, textChannels)) {
        toDelete.push({ kind: 'channel', name: stale.name, id: stale.id, reason: `stale old name of "${ch.name}"` });
      }
    }
  }

  if (!toDelete.length) {
    console.log('Nothing to clean up.');
    await client.destroy();
    return;
  }

  console.log(`=== ${toDelete.length} item(s) to remove ===`);
  for (const d of toDelete) console.log(`  [${d.kind}] "${d.name}"  (${d.reason})  id=${d.id}`);

  if (CONFIRM) {
    console.log('\n--confirm passed: deleting...');
    for (const d of toDelete) {
      try {
        if (d.kind === 'role') await guild.roles.delete(d.id);
        else await guild.channels.delete(d.id);
        console.log(`  deleted [${d.kind}] "${d.name}"`);
      } catch (err) {
        console.error(`  FAILED to delete [${d.kind}] "${d.name}":`, err);
      }
    }
  } else {
    console.log('\nDry run only -- re-run with --confirm to actually delete these.');
  }

  await client.destroy();
}

main().catch(err => { console.error(err); process.exit(1); });
