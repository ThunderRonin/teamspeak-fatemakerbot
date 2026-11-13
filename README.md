# The FateMaker

TeamSpeak dice bot for `hallucinators`. Connects as a normal user, like Roadie, using a Node.js client bridge and a Python game engine. No audio playback or ServerQuery access is needed for normal operation.

## Files

- `voice-client.mjs`: normal-client connection, persistent identity, chat routing, and Python worker lifecycle
- `worker.py`: line-delimited JSON interface to the Python game engine
- `dicebot.py`: shared dice/initiative engine and optional legacy ServerQuery client
- `package.json` / `package-lock.json`: exact-pinned normal-client library and dependency integrity
- `requirements.txt`: optional `paramiko` for legacy SSH query mode
- `tests/`: unit tests
- `dicebot.env.example`: runtime configuration template
- `scripts/deploy.sh`: deploy a pinned image with pre-deployment state and identity backups
- `teamspeak-dicebot.service`: systemd unit

## Behavior

- listens to private chat everywhere
- listens to channel chat only in the configured public channel
- persists initiative state to a local JSON file across bot restarts
- supports:
  - `!roll <expr>`
  - `!adv <expr>`
  - `!dis <expr>`
  - `!init add <name> <score|expr>` — e.g. `!init add VMA 14` or `!init add VMA d20+3`
  - `!init set <name> <score|expr>` — correct an existing entry
  - `!init list` — one initiative entry per line; long lists are split across messages
  - `!init next`
  - `!init remove <name>`
  - `!init undo` — undo the last initiative change
  - `!init clear`
  - `!help`

## Notes

- anyone can roll
- only `GM` can edit, advance, remove, clear, or undo initiative
- shorthand works with or without `!` when the whole message is a dice expression:
  - `!d20`
  - `!2d6+3`
  - `d20+2`
  - `2d6+3`
  - `adv d20+5`
  - `dis d20+1`
  - `wd20+2`
  - `!2d20h1` — advantage (keep highest)
  - `!2d20l1` — disadvantage (keep lowest)
  - `ld20-6`
- keep/drop selectors are supported:
  - `4d6k3`
  - `4d6kh3`
  - `4d6kl3`
  - `4d6d1`
  - `4d6dh1`
  - `4d6dl1`
- initiative rounds start at 1 on the first `!init next` and advance when the turn order wraps
- up to 50 initiative changes per channel can be undone, including after a bot restart
- reconnect logic accounts for delayed TeamSpeak query startup

## Normal user setup (default)

1. Copy `dicebot.env.example` to `dicebot.env`. Set `DICEBOT_SERVER_ADDRESS` to the server's voice address (`host:9987`), `DICEBOT_PUBLIC_CHANNEL_ID`, and nickname. Set server/channel passwords only if required.
2. Start with `docker compose up -d --build`. The image includes Node.js, Python, and the pinned client library. The bridge starts Python itself; do not start a second worker against the same state file.
3. Grant the bot only the normal server-group permissions needed to join its channel, inspect sender/group membership, and send private/channel text. Add game masters to the group named by `DICEBOT_GM_GROUP_NAME` (default `GM`); the bot itself does not need GM membership.

For busy dice chat, give the dedicated bot group `b_client_ignore_antiflood`; native-client flood protection also counts permission lookups, so ServerQuery-style command spacing alone is insufficient. Set `i_client_private_textmessage_power` high enough to reply to the intended recipients (including administrators). These are bot transport permissions, not GM authorization; do not grant the bot Server Admin.

For a non-Docker installation, install Node.js 24 and Python 3, then run:

```sh
npm ci --ignore-scripts
node --env-file=dicebot.env voice-client.mjs
```

Set writable host paths for `DICEBOT_IDENTITY_PATH` and `DICEBOT_STATE_PATH` outside Docker. The defaults inside the image are `/data/identity.json` and `/data/state.json`. The Compose data volume persists both. **Back up the identity file:** it is the bot's account, and replacing it loses its normal-user identity and group assignments. Corrupt identities are not silently regenerated.

The bridge connects over TeamSpeak's UDP voice protocol but sends only text. It does not play or record audio. Connection/worker failure exits the process; Docker's `unless-stopped` policy reconnects it with the same identity. `SIGTERM`/`SIGINT` shut down the client and worker together.

The bridge uses the MIT [`@honeybbq/teamspeak`](https://github.com/HoneyBBQ/teamspeak-js) library, the maintained successor of Roadie's `@echosixhiya/teamspeak-client`. Version `1.0.0-beta.30` is pinned exactly in `package-lock.json`. Roadie's older `0.2.4` drops native server-group list responses; the newer release handles those responses so GM authorization can resolve group names correctly.

### Deploying with backups on arazel

Use the server-side deployment script with the immutable GitHub Actions image tag:

```sh
scripts/deploy.sh ghcr.io/thunderronin/teamspeak-fatemakerbot:sha-<40-character-commit>
```

The server's `ts/compose.yml` must use the `FATEMAKER_IMAGE` variable for FateMaker's image. The script pulls from GHCR, stops only FateMaker, verifies a private snapshot of `identity.json` and the existing `state.json`, then changes the pinned image and restarts the bot. Snapshots are retained under `ts/data/fatemaker-backups` with directory mode `0700` and file mode `0600`. If backup or cutover fails, the script restores the previous bot configuration. It does not restart TeamSpeak.

## Optional legacy ServerQuery mode

The standalone `python dicebot.py` entrypoint still supports TS3 raw query and TS6 SSH query. This is separate from the default normal-user bridge; query credentials are not used by `voice-client.mjs`.

### Setup

1. Enable SSH query on the **TeamSpeak server** with `TSSERVER_QUERY_SSH_ENABLED=1` (or `tsserver --query-ssh-enable`). It is disabled by default. Make TCP port `10022` reachable from the bot; publish that port if the server runs in Docker. See the [official server configuration](https://github.com/teamspeak/teamspeak6-server/blob/main/CONFIG.md).
2. Copy `dicebot.env.example` to `dicebot.env`. Set `DICEBOT_QUERY_PROTOCOL=ssh`, the server host, query credentials, virtual server ID, and public channel ID. Remove an old `DICEBOT_QUERY_PORT=10011` override; the SSH default is `10022`. The bot joins the configured channel unless it is already there.
3. Install `requirements.txt` and run `python dicebot.py` with the environment loaded. For Docker query mode, explicitly set `command: ["python", "-u", "dicebot.py"]` on the Compose service; the default image entrypoint is the normal-user bridge.

Use a dedicated query account with permission to select the virtual server, inspect clients/groups/channels, join the public channel, register notifications, and send private/channel messages. HTTP WebQuery credentials alone do not establish an SSH session.

- `DICEBOT_QUERY_USER` and `DICEBOT_QUERY_PASS` are the query login.
- `DICEBOT_QUERY_PORT` defaults to `10022` in ssh mode (`10011` in raw mode).
- ssh mode needs `paramiko`: `pip install -r requirements.txt`. The Docker image already includes it. Behind a network that needs a PyPI mirror, pass `--build-arg PIP_INDEX_URL=<mirror>` to `docker build`.
- `DICEBOT_SSH_HOST_KEY_SHA256` (optional) pins the server key, as `SHA256:<base64>` or bare base64. Without it, any server key is accepted: use this only on a trusted network during setup. Verify the logged fingerprint against the server's host key through a trusted channel, then pin it before sending credentials over an untrusted network.
- `DICEBOT_PUBLIC_CHANNEL_NAME` (optional) sets the channel name used in replies. When unset, the bot reads it from `channelinfo` and falls back to `channel <id>`.

## Legacy query connection behavior

These apply only to the standalone Python query client:

- After 60 seconds without a command the bot sends `whoami` as a keepalive, and it sends `quit` before closing the connection.
- The bot spaces its query commands at least 0.4 s apart to stay under TeamSpeak's default flood limit (10 commands per 3 s); operators can also add the bot's IP to `query_ip_allowlist.txt`.
- SSH connection, authentication, session opening, and shell negotiation are bounded by 10-second timeouts; a stalled shell is closed so reconnect handling can run.
- Response deadlines cover the complete command, including fragmented reads and interleaved chat events. UTF-8 names and messages are decoded only after a complete line arrives.

## Tests

`python -m unittest discover -s tests -v` (no `paramiko` needed)

`node --test tests/test_voice-client.mjs` (requires `npm ci`)

Normal-client smoke verification against TS6 `6.0.0-beta13.1` used a second native client, not a query client: `client_type=0` visibility, private/channel rolls, Unicode initiative, fresh GM grant/revocation, cross-channel private replies, and UID/database identity plus initiative persistence after graceful restart. The built Docker image also reached `connected and ready`.

The arazel deployment was also verified through an ordinary native client's channel directory: normal-user visibility in Hallucinators, private/channel replies, non-GM denial, and the same UID/database account plus unchanged identity/state files after restarting only FateMaker.

The legacy SSH repair was smoke-tested against `teamspeaksystems/teamspeak6-server:6.0.0-beta13.1`, including chat, initiative persistence, keepalive, reconnect, and host-key pinning.
