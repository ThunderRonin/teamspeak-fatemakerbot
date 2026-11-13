#!/usr/bin/env bash
set -Eeuo pipefail
script=$(cd "$(dirname "$0")/.." && pwd)/scripts/deploy.sh
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
mkdir -p "$scratch/bin"
cat > "$scratch/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ $1 == pull ]]; then
    printf 'pull %s\n' "$2" >> "$FAKE_LOG"
    exit 0
fi
[[ $1 == compose ]]
shift
while [[ $1 != config && $1 != stop && $1 != up ]]; do shift; done
case $1 in
    config) printf '%s\n' "${FATEMAKER_IMAGE:-}" ;;
    stop) printf 'stop %s\n' "$2" >> "$FAKE_LOG" ;;
    up)
        printf 'up %s\n' "${*:2}" >> "$FAKE_LOG"
        if [[ -e $FAKE_FAIL_UP ]]; then rm "$FAKE_FAIL_UP"; exit 1; fi
        if grep -Fx "FATEMAKER_IMAGE=$FAKE_NEW_IMAGE" "$FAKE_ROOT/ts/.env" >/dev/null; then
            backup=$(find "$FAKE_ROOT/ts/data/fatemaker-backups" -mindepth 1 -maxdepth 1 -type d ! -name '.pending.*' -print -quit)
            [[ -n $backup && -f $backup/identity.json ]] || exit 1
            if [[ -f $FAKE_ROOT/ts/data/fatemaker/state.json ]]; then
                cmp "$FAKE_ROOT/ts/data/fatemaker/state.json" "$backup/state.json"
            fi
            cmp "$FAKE_ROOT/ts/data/fatemaker/identity.json" "$backup/identity.json"
        fi
        ;;
esac
DOCKER
chmod +x "$scratch/bin/docker"
export PATH="$scratch/bin:$PATH"
export FAKE_LOG="$scratch/log" FAKE_FAIL_UP="$scratch/fail-up"
image=ghcr.io/thunderronin/teamspeak-fatemakerbot:sha-0123456789abcdef0123456789abcdef01234567
old=ghcr.io/thunderronin/teamspeak-fatemakerbot:sha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
root=$scratch/server
export FAKE_NEW_IMAGE="$image" FAKE_ROOT="$root"
reset_fixture() {
    rm -rf "$root"
    mkdir -p "$root/ts/data/fatemaker"
    printf 'secret=unchanged\nFATEMAKER_IMAGE=%s\n' "$old" > "$root/ts/.env"
    chmod 600 "$root/ts/.env"
    : > "$root/ts/compose.yml"
    printf 'identity-original\n' > "$root/ts/data/fatemaker/identity.json"
    printf 'state-original\n' > "$root/ts/data/fatemaker/state.json"
    : > "$FAKE_LOG"
}
fail() { echo "FAIL: $*" >&2; exit 1; }

reset_fixture
"$script" "$image" "$root"
mapfile -t snapshots < <(find "$root/ts/data/fatemaker-backups" -mindepth 1 -maxdepth 1 -type d)
[[ ${#snapshots[@]} == 1 ]] || fail 'expected one snapshot'
cmp "$root/ts/data/fatemaker/identity.json" "${snapshots[0]}/identity.json"
cmp "$root/ts/data/fatemaker/state.json" "${snapshots[0]}/state.json"
[[ $(stat -c %a "${snapshots[0]}") == 700 && $(stat -c %a "${snapshots[0]}/identity.json") == 600 && $(stat -c %a "${snapshots[0]}/state.json") == 600 ]] || fail 'backup permissions'
grep -Fx "FATEMAKER_IMAGE=$image" "$root/ts/.env" >/dev/null
grep -Fx 'secret=unchanged' "$root/ts/.env" >/dev/null
[[ $(stat -c %a "$root/ts/.env") == 600 ]] || fail 'env permissions'
mapfile -t commands < "$FAKE_LOG"
[[ ${commands[0]} == "pull $image" && ${commands[1]} == 'stop fatemaker' && ${commands[2]} == 'up -d --no-deps --no-build --pull never fatemaker' && ${#commands[@]} == 3 ]] || fail 'deployment command order or scope'

# An absent identity must not cut over; the prior bot is restarted.
reset_fixture
rm "$root/ts/data/fatemaker/identity.json"
if "$script" "$image" "$root"; then fail 'missing identity accepted'; fi
grep -Fx "FATEMAKER_IMAGE=$old" "$root/ts/.env" >/dev/null
[[ $(grep -c '^up ' "$FAKE_LOG") == 1 ]] || fail 'old service not restarted after backup failure'
[[ -z $(find "$root/ts/data/fatemaker-backups" -mindepth 1 -maxdepth 1 -type d -print -quit) ]] || fail 'incomplete snapshot published'

# If starting the new container fails, restore the exact old env and retry only the bot.
reset_fixture
cp "$root/ts/.env" "$scratch/expected-env"
touch "$FAKE_FAIL_UP"
if "$script" "$image" "$root"; then fail 'failed cutover accepted'; fi
cmp "$scratch/expected-env" "$root/ts/.env"
[[ $(grep -c '^up ' "$FAKE_LOG") == 2 ]] || fail 'old service not restarted after cutover failure'
[[ -f $(find "$root/ts/data/fatemaker-backups" -name identity.json -print -quit) ]] || fail 'verified backup lost on failure'

reset_fixture
rm "$root/ts/data/fatemaker/state.json"
"$script" "$image" "$root"
[[ -f $(find "$root/ts/data/fatemaker-backups" -name identity.json -print -quit) ]] || fail 'initial deployment identity missing'
reset_fixture
if "$script" ghcr.io/thunderronin/teamspeak-fatemakerbot:latest "$root"; then fail 'mutable tag accepted'; fi
[[ ! -s $FAKE_LOG ]] || fail 'docker called for mutable tag'
echo 'Deployment safety tests passed'
