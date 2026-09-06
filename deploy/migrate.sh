#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo="${1:-https://github.com/lucifervalter-a11y/XASS.git}"
destination="${2:-/opt/xass-new}"
case "$repo" in https://github.com/*/*) ;; *) echo 'Use an HTTPS GitHub repository URL'; exit 1;; esac
[[ ! -e "$destination" && ! -L "$destination" ]] || { echo 'Destination already exists; use a new directory'; exit 1; }
command -v git >/dev/null || { echo 'Install git first'; exit 1; }
command -v python3 >/dev/null || { echo 'Install python3 and python3-venv first'; exit 1; }
tools_dir="$(mktemp -d)"
trap 'rm -rf -- "$tools_dir"' EXIT
git clone --depth 1 --branch main -- "$repo" "$tools_dir/source"
python3 -m venv "$tools_dir/venv"
"$tools_dir/venv/bin/python" -m pip install -r "$tools_dir/source/requirements.txt"
"$tools_dir/venv/bin/python" "$tools_dir/source/deploy/migrate.py" receive --destination "$destination"
git -C "$destination" init -b main
git -C "$destination" remote add origin "$repo"
git -C "$destination" fetch origin main
revision="$("$tools_dir/venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' "$destination/migration-manifest.json")"
if [[ "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  git -C "$destination" fetch origin "$revision"
  git -C "$destination" reset --mixed "$revision"
fi
git -C "$destination" branch --set-upstream-to=origin/main main
python3 -m venv "$destination/.venv"
"$destination/.venv/bin/python" -m pip install -r "$destination/requirements.txt"
echo "Restored to $destination. The new bot is NOT running yet."
echo 'Follow docs/SERVER_MIGRATION.md to stop the source, configure HTTPS and activate.'
