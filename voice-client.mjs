import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { chmod, mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { chunkMessage } from './message-chunks.mjs';
import { Client, generateIdentity, parseIdentity } from '@honeybbq/teamspeak/client';
import {
  asChannelID,
  getClientProperties,
  listServerGroups,
  moveClient,
  noopLogger,
  sendChannelMessage,
  sendPrivateMessage,
} from '@honeybbq/teamspeak';

const MAX_QUEUE = 64;
const MAX_INPUT_LENGTH = 8192;
const MAX_WORKER_LINE = 65536;
const WORKER_TIMEOUT_MS = 10000;
const CONNECT_TIMEOUT_MS = 30000;

// Only these messages may be logged: library errors can contain credential-bearing commands.
class BridgeError extends Error {}

function config() {
  const env = process.env;
  const channelId = env.DICEBOT_PUBLIC_CHANNEL_ID ?? '2';
  const level = env.DICEBOT_IDENTITY_LEVEL ?? '10';
  if (!/^[1-9]\d*$/.test(channelId) || BigInt(channelId) > 0xffffffffffffffffn) {
    throw new BridgeError('DICEBOT_PUBLIC_CHANNEL_ID must be a positive channel ID.');
  }
  if (!/^\d+$/.test(level) || Number(level) > 160) {
    throw new BridgeError('DICEBOT_IDENTITY_LEVEL must be an integer from 0 to 160.');
  }
  const result = {
    address: env.DICEBOT_SERVER_ADDRESS ?? '127.0.0.1:9987',
    serverPassword: env.DICEBOT_SERVER_PASSWORD || undefined,
    channelId: asChannelID(BigInt(channelId)),
    channelPassword: env.DICEBOT_CHANNEL_PASSWORD ?? '',
    nickname: env.DICEBOT_NICKNAME ?? 'The FateMaker',
    gmGroupName: env.DICEBOT_GM_GROUP_NAME ?? 'GM',
    identityPath: env.DICEBOT_IDENTITY_PATH ?? '/data/identity.json',
    identityLevel: Number(level),
  };
  if (!result.address || !result.nickname || !result.identityPath || !result.gmGroupName) {
    throw new BridgeError('Server address, nickname, identity path, and GM group name must not be empty.');
  }
  return result;
}

async function loadIdentity(path, level, signal) {
  let identity;
  try {
    const saved = JSON.parse(await readFile(path, 'utf8'));
    if (typeof saved.identity !== 'string' || !/^[A-Za-z0-9+/]+={0,2}:\d+$/.test(saved.identity)) {
      throw new Error();
    }
    identity = parseIdentity(saved.identity);
    if (identity.export() !== saved.identity) throw new Error();
  } catch (error) {
    if (error.code !== 'ENOENT') {
      throw new BridgeError('Cannot read persisted identity or identity is corrupt; refusing to replace it.');
    }
    identity = await generateIdentity(0, { signal });
  }
  if (identity.securityLevel() < level) identity = await identity.upgradeToLevel(level, { signal });
  if (signal.aborted) throw new BridgeError('Identity preparation interrupted.');

  // Rename in the same directory guarantees a complete private key or the previous complete key.
  const directory = dirname(path);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
  let file;
  try {
    file = await open(temporary, 'wx', 0o600);
    await file.writeFile(`${JSON.stringify({ identity: identity.export() })}\n`, 'utf8');
    await file.sync();
    await file.close();
    file = undefined;
    await rename(temporary, path);
    await chmod(path, 0o600);
    const folder = await open(directory, 'r');
    try {
      await folder.sync();
    } finally {
      await folder.close();
    }
  } finally {
    await file?.close();
    await unlink(temporary).catch((error) => {
      if (error.code !== 'ENOENT') throw error;
    });
  }
  return identity;
}

class GameWorker {
  constructor(fatal) {
    this.fatal = fatal;
    this.pending = undefined;
    this.nextId = 0;
    this.buffer = '';
    this.stopping = false;
    this.child = spawn('python3', ['-u', fileURLToPath(new URL('./worker.py', import.meta.url))], {
      env: process.env,
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk) => this.receive(chunk));
    this.child.stdout.on('error', () => this.fail('Cannot read game worker output.'));
    this.child.stdin.on('error', () => this.fail('Cannot write to game worker.'));
    this.child.on('error', () => this.fail('Cannot start game worker.'));
    this.child.on('exit', () => this.fail('Game worker exited unexpectedly.'));
  }

  fail(message) {
    if (this.stopping) return;
    this.stopping = true;
    const pending = this.pending;
    this.pending = undefined;
    if (pending) {
      clearTimeout(pending.timer);
      pending.reject(new BridgeError(message));
    }
    this.fatal(new BridgeError(message));
  }

  receive(chunk) {
    if (this.stopping) return;
    this.buffer += chunk;
    if (this.buffer.length > MAX_WORKER_LINE) {
      this.fail('Game worker output exceeded the protocol limit.');
      return;
    }
    while (this.buffer.includes('\n')) {
      const end = this.buffer.indexOf('\n');
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      let reply;
      try {
        reply = JSON.parse(line);
      } catch {
        this.fail('Game worker returned invalid JSON.');
        return;
      }
      const pending = this.pending;
      if (!pending || !reply || reply.id !== pending.id ||
          (reply.response !== null && typeof reply.response !== 'string')) {
        this.fail('Game worker violated the response protocol.');
        return;
      }
      clearTimeout(pending.timer);
      this.pending = undefined;
      pending.resolve(reply.response);
    }
  }

  request(message) {
    if (this.stopping || this.pending) {
      return Promise.reject(new BridgeError('Game worker is unavailable or already handling a request.'));
    }
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail('Game worker request timed out.'), WORKER_TIMEOUT_MS);
      this.pending = { id, resolve, reject, timer };
      this.child.stdin.write(`${JSON.stringify({ id, ...message })}\n`, (error) => {
        if (error) this.fail('Cannot write to game worker.');
      });
    });
  }

  stop() {
    this.stopping = true;
    if (this.pending) {
      clearTimeout(this.pending.timer);
      this.pending.reject(new BridgeError('Game worker stopped.'));
      this.pending = undefined;
    }
    this.child.kill('SIGTERM');
  }
}

async function main() {
  let client;
  let worker;
  let ready = false;
  let stopping = false;
  let draining = false;
  const queue = [];
  const stopController = new AbortController();

  function shutdown(code, error) {
    if (stopping) return;
    stopping = true;
    ready = false;
    queue.length = 0;
    if (error) console.error(error instanceof BridgeError ? error.message : 'Unexpected voice bridge failure.');
    stopController.abort();
    worker?.stop();
    const forceExit = setTimeout(() => {
      worker?.child.kill('SIGKILL');
      process.exit(code);
    }, 3000);
    Promise.resolve(client?.disconnect()).catch(() => {}).finally(() => {
      clearTimeout(forceExit);
      worker?.child.kill('SIGKILL');
      process.exit(code);
    });
  }

  const fatal = (error) => shutdown(1, error);
  process.on('SIGTERM', () => shutdown(0));
  process.on('SIGINT', () => shutdown(0));
  process.on('uncaughtException', fatal);
  process.on('unhandledRejection', fatal);

  try {
    const options = config();
    const identity = await loadIdentity(options.identityPath, options.identityLevel, stopController.signal);
    if (stopping) return;
    let nextCommandAt = 0;
    client = new Client(identity, options.address, options.nickname, {
      serverPassword: options.serverPassword,
      lineMiddleware: [(next) => async (line, commandOptions) => {
        const wait = Math.max(0, nextCommandAt - performance.now());
        nextCommandAt = performance.now() + wait + 300;
        if (wait) await sleep(wait, undefined, { signal: commandOptions.signal });
        return next(line, commandOptions);
      }],
      // Do not pass defaultChannel: that option is a name, whereas configuration is an exact ID.
      logger: noopLogger,
    });
    client.on('disconnected', () => fatal(new BridgeError('TeamSpeak disconnected; exiting for supervisor restart.')));
    client.on('kicked', () => fatal(new BridgeError('TeamSpeak kicked the client; exiting for supervisor restart.')));
    client.on('clientMoved', (event) => {
      if (ready && event.clientID === client.clientID && event.targetChannelID !== options.channelId) {
        fatal(new BridgeError('Client was moved outside its configured channel; exiting for supervisor restart.'));
      }
    });

    async function gmGroupIds() {
      const rows = await listServerGroups(client, { timeoutMs: 5000 });
      if (!rows.length || rows.some((row) => row.id <= 0n || typeof row.name !== 'string')) {
        throw new BridgeError('Server did not return a valid server group list.');
      }
      return [...new Set(rows.filter((row) => row.name === options.gmGroupName).map((row) => String(row.id)))];
    }

    async function reply(event, text) {
      if (!text) return;
      if (event.targetMode === 2 && client.channelID !== options.channelId) {
        throw new BridgeError('Refusing a channel reply outside the configured channel.');
      }
      for (const response of chunkMessage(text)) {
        if (event.targetMode === 2) {
          await sendChannelMessage(client, response, { timeoutMs: 5000 });
        } else {
          await sendPrivateMessage(client, event.invokerID, response, { timeoutMs: 5000 });
        }
      }
    }

    async function handle(event) {
      let info;
      let groups;
      try {
        // Never authorize from invokerGroups or directory snapshots: they can be stale.
        info = await getClientProperties(client, event.invokerID, { timeoutMs: 5000 });
        if (!/^[1-9]\d*$/.test(info.cid ?? '') ||
            typeof info.client_servergroups !== 'string' ||
            !/^(?:[1-9]\d*(?:,[1-9]\d*)*)?$/.test(info.client_servergroups)) {
          throw new BridgeError('Client information is incomplete.');
        }
        if (event.invokerUID && info.client_unique_identifier !== event.invokerUID) {
          throw new BridgeError('Sender identity changed before processing.');
        }
        if (event.targetMode === 2 && BigInt(info.cid) !== options.channelId) return;
        groups = await gmGroupIds();
      } catch {
        console.error('Cannot verify current sender permissions; message was not dispatched.');
        await sendPrivateMessage(client, event.invokerID, 'Cannot verify your current TeamSpeak permissions. Please try again.', { timeoutMs: 5000 });
        return;
      }
      const response = await worker.request({
        message: event.message,
        channel_id: BigInt(info.cid).toString(),
        sender_name: info.client_nickname ?? event.invokerName,
        sender_groups: info.client_servergroups ? info.client_servergroups.split(',').map((id) => BigInt(id).toString()) : [],
        gm_group_ids: groups,
      });
      if (!stopping) await reply(event, response);
    }

    async function drain() {
      if (draining || stopping) return;
      draining = true;
      try {
        while (queue.length && !stopping) await handle(queue.shift());
      } catch (error) {
        fatal(error);
      } finally {
        draining = false;
      }
    }

    client.on('textMessage', (event) => {
      if (!ready || stopping || event.invokerID === client.clientID || event.invokerUID === client.uid) return;
      if (![1, 2, 3].includes(event.targetMode) || !Number.isSafeInteger(event.invokerID) || event.invokerID <= 0) return;
      if (!event.message.trim()) return;
      if (event.targetMode === 2 && client.channelID !== options.channelId) return;
      if (event.message.length > MAX_INPUT_LENGTH) {
        fatal(new BridgeError('Incoming message exceeded the bridge input limit.'));
        return;
      }
      if (queue.length >= MAX_QUEUE) {
        fatal(new BridgeError('Incoming message queue is full; exiting for supervisor restart.'));
        return;
      }
      queue.push(event);
      void drain();
    });

    await client.connect({
      signal: AbortSignal.any([stopController.signal, AbortSignal.timeout(CONNECT_TIMEOUT_MS)]),
    });
    await client.view.whenReady({
      signal: AbortSignal.any([stopController.signal, AbortSignal.timeout(5000)]),
    });
    const channel = client.view.channel(options.channelId);
    if (!channel) throw new BridgeError('Configured channel ID was not found in the server directory.');
    if (client.channelID !== channel.id) {
      await moveClient(client, client.clientID, channel.id, { password: options.channelPassword, timeoutMs: 10000 });
    }
    const self = await getClientProperties(client, client.clientID, { timeoutMs: 5000 });
    if (self.cid !== String(options.channelId)) {
      throw new BridgeError('Server did not place the client in the configured channel ID.');
    }
    await gmGroupIds();
    worker = new GameWorker(fatal);
    ready = true;
    console.log(`[dicebot] connected and ready (client ${client.clientID}, channel ${options.channelId}, uid ${client.uid}).`);
  } catch (error) {
    fatal(error);
  }
}

await main();
