# The FateMaker

Air-gapped TeamSpeak dice bot for `hallucinators`.

## Files

- `dicebot.py`: bot implementation, pure Python stdlib
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
