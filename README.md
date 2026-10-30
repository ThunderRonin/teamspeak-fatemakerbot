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

- initiative is in-memory only
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

TeamSpeak 6 has no raw ServerQuery, only SSH query. Set `DICEBOT_QUERY_PROTOCOL=ssh` to use it. The default, `raw`, is TeamSpeak 3 ServerQuery and needs no extra packages.

- `DICEBOT_QUERY_USER` and `DICEBOT_QUERY_PASS` are the query login.
- `DICEBOT_QUERY_PORT` defaults to `10022` in ssh mode (`10011` in raw mode).
- ssh mode needs `paramiko`: `pip install -r requirements.txt`. The Docker image already includes it. Behind a network that needs a PyPI mirror, pass `--build-arg PIP_INDEX_URL=<mirror>` to `docker build`.
- `DICEBOT_SSH_HOST_KEY_SHA256` (optional) pins the server key, as `SHA256:<base64>` or bare base64. When unset, the bot accepts any key and logs the fingerprint on every connect, so you can copy it into the setting.
- `DICEBOT_PUBLIC_CHANNEL_NAME` (optional) sets the channel name used in replies. When unset, the bot reads it from `channelinfo` and falls back to `channel <id>`.
- After 60 seconds without a command the bot sends `whoami` as a keepalive, and it sends `quit` before closing the connection.
- The bot spaces its query commands at least 0.4 s apart to stay under TeamSpeak's default flood limit; operators can also add the bot's IP to `query_ip_allowlist.txt`.

## Tests

`python -m unittest discover -s tests -v` (no `paramiko` needed)
