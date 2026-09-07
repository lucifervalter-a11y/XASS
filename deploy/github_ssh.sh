#!/usr/bin/env bash
# GitHub runner transport. Public host keys are pinned, never learned on first use.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${SSH_HOST:?}" "${SSH_USER:?}" "${SSH_PRIVATE_KEY:?}" "${RUNNER_TEMP:?}"
port="${SSH_PORT:-22}"
[[ "$port" =~ ^[0-9]{1,5}$ ]] || { echo 'Invalid SSH port'; exit 1; }
umask 077
ssh_dir="$(mktemp -d "$RUNNER_TEMP/xass-ssh.XXXXXX")"
key_file="$ssh_dir/key"
known_hosts="$ssh_dir/known_hosts"
trap 'rm -f -- "$key_file" "$known_hosts"; rmdir -- "$ssh_dir"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" > "$key_file"
host_label="$SSH_HOST"
if [[ "$port" != 22 ]]; then host_label="[$SSH_HOST]:$port"; fi
while IFS= read -r key || [[ -n "$key" ]]; do
  [[ -n "$key" && "$key" != \#* ]] || continue
  printf '%s %s\n' "$host_label" "$key" >> "$known_hosts"
done < "$root/deploy/xass-host-keys.pub"
options=(-i "$key_file" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts" -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
case "${1:-}" in
  exec)
    ssh -p "$port" "${options[@]}" "$SSH_USER@$SSH_HOST" 'bash -se'
    ;;
  upload)
    revision="${2:-}"
    [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid release revision'; exit 1; }
    target="/home/red/serverredus/data/releases/.incoming-$revision"
    ssh -p "$port" "${options[@]}" "$SSH_USER@$SSH_HOST" "mkdir -p '$target'"
    scp -P "$port" "${options[@]}" release/XASS-Setup.exe release/XASS-Setup.json "$SSH_USER@$SSH_HOST:$target/"
    ;;
  *) echo 'Expected exec or upload'; exit 1 ;;
esac
