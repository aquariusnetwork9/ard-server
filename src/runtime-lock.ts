import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

/**
 * Cross-PROCESS guard against the exact incident this bot already hit once:
 * multiple zombie Node processes from botched restarts staying connected to
 * Discord simultaneously, all reacting to the same interaction. A module-
 * level boolean (see setup.ts/cleanup.ts's `running` flag) only guards
 * within one process's memory -- it does nothing across OS processes.
 *
 * This claims a shared lock file on startup, unconditionally overwriting
 * whatever session id was there -- the most-recently-started process always
 * wins. Any older process (a zombie that should have been killed but wasn't)
 * keeps its own now-stale id in memory and will fail isCurrentSession() on
 * every check, since the file no longer matches what it remembers writing.
 */

const LOCK_PATH = path.join(process.cwd(), '.runtime-session');

let mySessionId: string | null = null;

export function claimSession(): string {
  mySessionId = crypto.randomUUID();
  fs.writeFileSync(LOCK_PATH, mySessionId, 'utf8');
  return mySessionId;
}

export async function isCurrentSession(): Promise<boolean> {
  if (!mySessionId) return false;
  try {
    return (await fs.promises.readFile(LOCK_PATH, 'utf8')) === mySessionId;
  } catch {
    return false;
  }
}
