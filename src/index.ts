import { Client, GatewayIntentBits, REST, Routes, Collection, ChatInputCommandInteraction } from 'discord.js';
import { config } from './config';
import * as linkCommand from './discord/commands/link';
import * as setupCommand from './discord/commands/setup';
import * as cleanupCommand from './discord/commands/cleanup';
import { TRAVELER_ROLE } from './discord/provision/structure';
import { claimSession, isCurrentSession } from './runtime-lock';
import { startDispatchPolling, handleDispatchButton } from './discord/dispatch/poller';

interface Command {
  data: { name: string; toJSON: () => unknown };
  execute: (interaction: ChatInputCommandInteraction) => Promise<void>;
}

const commands: Collection<string, Command> = new Collection();
for (const cmd of [linkCommand, setupCommand, cleanupCommand]) {
  commands.set(cmd.data.name, cmd);
}

const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMembers] });

async function registerCommands(): Promise<void> {
  const rest = new REST().setToken(config.discord.token);
  const body = [...commands.values()].map(c => c.data.toJSON());
  // Guild-scoped registration: propagates instantly, unlike global commands
  // (up to an hour) -- this bot only ever operates in one guild anyway.
  await rest.put(
    Routes.applicationGuildCommands(config.discord.clientId, config.discord.guildId),
    { body }
  );
  console.log(`[commands] Registered ${body.length} command(s) to guild ${config.discord.guildId}`);
}

client.once('ready', async () => {
  console.log(`[bot] Logged in as ${client.user?.tag}`);
  // Claim the cross-process lock as soon as we're confirmed logged in. Any
  // older zombie process (the exact class of bug that already caused a real
  // incident) now has a stale session id in its own memory and will fail
  // isCurrentSession() on every check below, regardless of how it was
  // supposed to have been killed and wasn't.
  try {
    claimSession();
  } catch (err) {
    // Fail closed, not catastrophic: if the lock file couldn't be written
    // (permissions, disk, a transient lock from AV/OneDrive, etc.), leave
    // mySessionId unset so isCurrentSession() keeps returning false -- this
    // process refuses to act until restarted, rather than either crashing
    // outright (a plain fs hiccup taking the whole bot offline) or silently
    // proceeding without the one protection that already mattered once.
    console.error('[runtime-lock] Failed to claim session lock -- this process will refuse to act until restarted:', err);
  }
  try {
    await registerCommands();
  } catch (err) {
    // A registration failure (e.g. the bot was invited without the
    // applications.commands OAuth2 scope -- 403 "Missing Access") shouldn't
    // take the whole gateway connection down with it; log it and stay online
    // so the bot's presence/role-granting still work once it's fixed and the
    // process is restarted.
    console.error('[commands] Failed to register slash commands:', err);
  }
  startDispatchPolling(client);
});

client.on('guildMemberAdd', async member => {
  if (!(await isCurrentSession())) return; // stale zombie process -- a newer one is handling this
  if (member.guild.id !== config.discord.guildId || member.user.bot) return;
  const traveler = member.guild.roles.cache.find(r => r.name === TRAVELER_ROLE);
  if (!traveler) {
    console.error(`[join] "${TRAVELER_ROLE}" role doesn't exist yet -- run /setup first.`);
    return;
  }
  await member.roles.add(traveler).catch(err => console.error('[join] Failed to grant Traveler role:', err));
});

client.on('interactionCreate', async interaction => {
  if (interaction.isButton() && interaction.customId.startsWith('dispatch:')) {
    if (!(await isCurrentSession())) {
      await interaction.reply({ content: 'This bot process is stale -- a newer instance should handle this. Try again.', ephemeral: true }).catch(() => {});
      return;
    }
    try {
      await handleDispatchButton(interaction);
    } catch (err) {
      console.error('[dispatch] Button handler failed:', err);
      const payload = { content: 'Something went wrong handling that button.', ephemeral: true };
      if (interaction.deferred || interaction.replied) {
        await interaction.editReply(payload).catch(() => {});
      } else {
        await interaction.reply(payload).catch(() => {});
      }
    }
    return;
  }
  if (!interaction.isChatInputCommand()) return;
  const command = commands.get(interaction.commandName);
  if (!command) return;
  if (!(await isCurrentSession())) {
    // A newer process has since claimed the lock -- this process is a stale
    // zombie (the exact scenario that already caused duplicate-creation
    // damage once). Refuse rather than race whatever process is now current.
    await interaction.reply({ content: 'This bot process is stale -- a newer instance should handle this. Try again.', ephemeral: true }).catch(() => {});
    return;
  }
  try {
    await command.execute(interaction);
  } catch (err) {
    console.error(`[command:${interaction.commandName}] failed:`, err);
    const payload = { content: 'Something went wrong running that command.', ephemeral: true };
    if (interaction.deferred || interaction.replied) {
      await interaction.editReply(payload).catch(() => {});
    } else {
      await interaction.reply(payload).catch(() => {});
    }
  }
});

client.login(config.discord.token);
