# The FateMaker

Air-gapped TeamSpeak dice bot for `hallucinators`.

## Files

- `dicebot.py`: bot implementation; raw mode is pure Python stdlib, ssh mode needs `paramiko`
- `requirements.txt`: pinned `paramiko` for ssh mode
- `tests/`: unit tests
- `dicebot.env.example`: runtime configuration template
- `teamspeak-dicebot.service`: systemd unit

## Behavior

- listens to private chat everywhere
- listens to channel chat only in the configured public channel
- persists initiative state to a local JSON file across bot restarts
- supports:
  - `!roll <expr>`
  - `!adv <expr>`
  - `!dis <expr>`
  - `!init add <name> <expr>`
  - `!init list`
  - `!init next`
  - `!init remove <name>`
  - `!init clear`
  - `!help`

## Notes

- anyone can roll
- only `GM` can advance, remove, or clear initiative
- shorthand works without prefixes when the whole message is a dice expression:
  - `d20+2`
  - `2d6+3`
  - `adv d20+5`
  - `dis d20+1`
  - `wd20+2`
  - `ld20-6`
- keep/drop selectors are supported:
  - `4d6k3`
  - `4d6kh3`
  - `4d6kl3`
  - `4d6d1`
  - `4d6dh1`
  - `4d6dl1`
- reconnect logic accounts for delayed TeamSpeak query startup

## TeamSpeak 6 (SSH query)

TeamSpeak 6 has no raw ServerQuery. Of its query interfaces (SSH and HTTP WebQuery) only SSH delivers chat events, so the bot uses SSH. Set `DICEBOT_QUERY_PROTOCOL=ssh` to use it. The default, `raw`, is TeamSpeak 3 ServerQuery and needs no extra packages.

### Setup

1. Enable SSH query on the **TeamSpeak server** with `TSSERVER_QUERY_SSH_ENABLED=1` (or `tsserver --query-ssh-enable`). It is disabled by default. Make TCP port `10022` reachable from the bot; publish that port if the server runs in Docker. See the [official server configuration](https://github.com/teamspeak/teamspeak6-server/blob/main/CONFIG.md).
2. Copy `dicebot.env.example` to `dicebot.env`. Set `DICEBOT_QUERY_PROTOCOL=ssh`, the server host, query credentials, virtual server ID, and public channel ID. Remove an old `DICEBOT_QUERY_PORT=10011` override; the SSH default is `10022`. The bot joins the configured channel unless it is already there.
3. Start with `docker compose up -d --build`, or install `requirements.txt` and run `python dicebot.py` with the environment loaded.

Use a dedicated query account with permission to select the virtual server, inspect clients/groups/channels, join the public channel, register notifications, and send private/channel messages. HTTP WebQuery credentials alone do not establish an SSH session.

- `DICEBOT_QUERY_USER` and `DICEBOT_QUERY_PASS` are the query login.
- `DICEBOT_QUERY_PORT` defaults to `10022` in ssh mode (`10011` in raw mode).
- ssh mode needs `paramiko`: `pip install -r requirements.txt`. The Docker image already includes it. Behind a network that needs a PyPI mirror, pass `--build-arg PIP_INDEX_URL=<mirror>` to `docker build`.
- `DICEBOT_SSH_HOST_KEY_SHA256` (optional) pins the server key, as `SHA256:<base64>` or bare base64. Without it, any server key is accepted: use this only on a trusted network during setup. Verify the logged fingerprint against the server's host key through a trusted channel, then pin it before sending credentials over an untrusted network.
- `DICEBOT_PUBLIC_CHANNEL_NAME` (optional) sets the channel name used in replies. When unset, the bot reads it from `channelinfo` and falls back to `channel <id>`.

## Connection behavior (both modes)

These apply to both `raw` (TeamSpeak 3) and `ssh` (TeamSpeak 6) modes:

- After 60 seconds without a command the bot sends `whoami` as a keepalive, and it sends `quit` before closing the connection.
- The bot spaces its query commands at least 0.4 s apart to stay under TeamSpeak's default flood limit (10 commands per 3 s); operators can also add the bot's IP to `query_ip_allowlist.txt`.
- SSH connection, authentication, session opening, and shell negotiation are bounded by 10-second timeouts; a stalled shell is closed so reconnect handling can run.
- Response deadlines cover the complete command, including fragmented reads and interleaved chat events. UTF-8 names and messages are decoded only after a complete line arrives.

## Tests

`python -m unittest discover -s tests -v` (no `paramiko` needed)

TS6 repair smoke-tested against `teamspeaksystems/teamspeak6-server:6.0.0-beta13.1`: private/channel dice replies, shorthand/selectors, Unicode initiative persistence, GM restrictions, keepalive, reconnect, and pinned host keys. The Docker executable reached `connected and ready`. Chat was driven by SSH query clients; a real voice client was not available.
