import dotenv from 'dotenv';
import path from 'path';

// Load .env from the directory the process is launched from (same convention
// as the rest of the Aquarius Discord-bot fleet -- works in dev at the repo
// root and under a systemd EnvironmentFile= equally).
dotenv.config({ path: path.join(process.cwd(), '.env') });

function required(name: string): string {
  const val = process.env[name];
  if (!val) {
    console.error(`[config] Missing required environment variable: ${name}`);
    process.exit(1);
  }
  return val;
}

export const config = {
  discord: {
    token: required('DISCORD_TOKEN'),
    clientId: required('DISCORD_CLIENT_ID'),
    guildId: required('DISCORD_GUILD_ID'),
    ownerIds: (process.env.DISCORD_OWNER_IDS ?? '').split(',').map(s => s.trim()).filter(Boolean),
  },
  ard: {
    baseUrl: (process.env.ARD_BASE_URL ?? 'http://localhost:8788').replace(/\/$/, ''),
    botSecret: required('ARD_BOT_SECRET'),
  },
};
