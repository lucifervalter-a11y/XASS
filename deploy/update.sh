#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "[ERROR] git is not installed"
  exit 1
fi

echo "[INFO] Fetching updates from origin/main..."
git fetch origin
git pull --rebase --autostash origin main

if [[ -f ".venv/bin/activate" ]]; then
  echo "[INFO] Installing Python requirements..."
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -r requirements.txt
fi

if command -v systemctl >/dev/null 2>&1; then
  echo "[INFO] Restarting services..."
  sudo systemctl restart serverredus-backend || true
  sudo systemctl restart serverredus-agent || true
fi

echo "[OK] Update complete."
