#!/usr/bin/env bash
# Deploy from the server's checkout; only the fatemaker service is stopped.
set -Eeuo pipefail

if (( $# < 1 || $# > 2 )); then
    echo "Usage: $0 ghcr.io/thunderronin/teamspeak-fatemakerbot:sha-<40 lowercase hex> [server-root]" >&2
    exit 2
fi
image=$1
root=${2:-/root/arazel-server}
if [[ ! $image =~ ^ghcr\.io/thunderronin/teamspeak-fatemakerbot:sha-[0-9a-f]{40}$ ]]; then
    echo "Expected an immutable FateMaker sha-<40 lowercase hex> image" >&2
    exit 2
fi

ts=$root/ts
env_file=$ts/.env
compose_file=$ts/compose.yml
data=$ts/data/fatemaker
backups=$ts/data/fatemaker-backups
[[ -f $env_file && ! -L $env_file && -r $env_file && -f $compose_file && -d $data ]] || {
    echo "Missing or unsafe compose, .env, or FateMaker data path" >&2
    exit 1
}
unset FATEMAKER_IMAGE
exec 9>"$ts/data/.fatemaker-deploy.lock"
flock -x 9
compose=(docker compose --project-directory "$ts" --env-file "$env_file" -f "$compose_file")
# Fail before stopping anything if the compose file does not honor the pin.
configured=$(FATEMAKER_IMAGE="$image" "${compose[@]}" config --images fatemaker)
[[ $configured == "$image" ]] || { echo "Compose does not use FATEMAKER_IMAGE for fatemaker" >&2; exit 1; }
docker pull "$image"

old_env=$(mktemp "$ts/.fatemaker-env-old.XXXXXXXX")
new_env=$(mktemp "$ts/.fatemaker-env-new.XXXXXXXX")
staging=
restore_needed=0
env_changed=0
cleanup() {
    status=$?
    trap - EXIT
    if (( restore_needed )); then
        echo "Deployment failed; restoring the previous FateMaker configuration" >&2
        can_restart=1
        if (( env_changed )); then
            if ! mv -f -- "$old_env" "$env_file"; then
                echo "ERROR: could not restore $env_file; previous copy is $old_env" >&2
                old_env=
                can_restart=0
                status=1
            fi
        fi
        if (( can_restart )) && ! "${compose[@]}" up -d --no-deps --no-build --pull never fatemaker; then
            echo "ERROR: could not restart the previous FateMaker service" >&2
            status=1
        fi
    fi
    [[ -z $staging ]] || rm -rf -- "$staging"
    [[ -z $old_env ]] || rm -f -- "$old_env"
    rm -f -- "$new_env"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp -p -- "$env_file" "$old_env"
cp -p -- "$env_file" "$new_env"
awk '!/^[[:space:]]*(export[[:space:]]+)?FATEMAKER_IMAGE[[:space:]]*=/' "$env_file" > "$new_env"
printf 'FATEMAKER_IMAGE=%s\n' "$image" >> "$new_env"

# A stopped bot cannot mutate either file between the snapshot and cutover.
restore_needed=1
"${compose[@]}" stop fatemaker
umask 077
mkdir -p -- "$backups"
chmod 700 -- "$backups"
staging=$(mktemp -d "$backups/.pending.XXXXXXXX")
for file in identity.json state.json; do
    source=$data/$file
    if [[ $file == state.json && ! -e $source && ! -L $source ]]; then
        continue # A brand-new installation may not have initiative state yet.
    fi
    [[ -f $source && -r $source ]] || { echo "Missing or unreadable $source" >&2; exit 1; }
    cp -- "$source" "$staging/$file"
    chmod 600 -- "$staging/$file"
    cmp -s -- "$source" "$staging/$file" || { echo "Backup verification failed for $source" >&2; exit 1; }
done
# Rename publishes only a complete, verified snapshot; mktemp makes names unique.
snapshot=$backups/$(date -u +%Y%m%dT%H%M%SZ)-${staging##*.}
mv -- "$staging" "$snapshot"
staging=
mv -f -- "$new_env" "$env_file"
env_changed=1
"${compose[@]}" up -d --no-deps --no-build --pull never fatemaker
restore_needed=0
printf 'Deployed %s; backup: %s\n' "$image" "$snapshot"
