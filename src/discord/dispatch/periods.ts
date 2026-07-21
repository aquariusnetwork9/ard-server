/**
 * ISO week / calendar month boundaries, in UTC -- shared by /leaderboard (to
 * scope a "this week"/"this month" read) and tiers.ts's rotating-badge job (to
 * detect when a new period has started and the previous winner's badge needs
 * to move). One definition so both agree on exactly where a period starts.
 */

/** Start of the current ISO week (Monday 00:00:00 UTC) in epoch ms. */
export function weekStartMs(now: Date = new Date()): number {
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const isoDay = d.getUTCDay() === 0 ? 7 : d.getUTCDay(); // Mon=1 .. Sun=7
  d.setUTCDate(d.getUTCDate() - (isoDay - 1));
  return d.getTime();
}

/** Start of the current calendar month (1st, 00:00:00 UTC) in epoch ms. */
export function monthStartMs(now: Date = new Date()): number {
  return Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1);
}

/** A stable key identifying "which week/month is this" -- changes exactly
 *  when weekStartMs/monthStartMs would return a new value, used to detect a
 *  period rollover without re-deriving the boundary every check. */
export function periodKey(cadence: 'weekly' | 'monthly', now: Date = new Date()): string {
  return cadence === 'weekly' ? String(weekStartMs(now)) : String(monthStartMs(now));
}

export function periodStartMs(cadence: 'weekly' | 'monthly', now: Date = new Date()): number {
  return cadence === 'weekly' ? weekStartMs(now) : monthStartMs(now);
}
