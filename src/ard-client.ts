import { config } from './config';

export class ArdLinkError extends Error {}

export interface CompleteLinkResult {
  tokenId: string;
  token: string;
  server: string;
}

/**
 * Completes a pending /link/init code via ARD's bot-authenticated path
 * (PROTOCOL.md SS6.2.1) -- proves the bot's own identity with ARD_BOT_SECRET
 * instead of a Discord OAuth code, since discordId is already Discord-verified
 * (it comes straight off the slash-command interaction).
 */
export async function completeLink(linkCode: string, discordId: string): Promise<CompleteLinkResult> {
  const resp = await fetch(`${config.ard.baseUrl}/link/bot-complete`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: config.ard.botSecret,
    },
    body: JSON.stringify({ linkCode, discordId }),
  });
  const body: any = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new ArdLinkError(body?.error ?? `ARD returned ${resp.status}`);
  }
  return body as CompleteLinkResult;
}
