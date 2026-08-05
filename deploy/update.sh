#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

command -v git >/dev/null 2>&1 || { echo "[ERROR] git is not installed"; exit 1; }

echo "[INFO] Fetching origin/main..."
git fetch --prune origin main
git merge --ff-only origin/main

echo "[INFO] Checking Python dependencies..."
if [[ -x ".venv/bin/python" ]]; then
  .venv/bin/python bootstrap_server_dependencies.py
else
  command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 is not installed"; exit 1; }
  python3 bootstrap_server_dependencies.py
fi

if command -v systemctl >/dev/null 2>&1; then
  echo "[INFO] Restarting backend..."
  sudo systemctl restart serverredus-backend

  port="${PORT:-}"
  if [[ -z "$port" && -f .env ]]; then
    port="$(sed -n 's/^PORT=//p' .env | tail -n 1 | tr -d '\r')"
  fi
  port="${port:-8000}"

  ready="false"
  for _ in {1..40}; do
    if curl -fsS --max-time 1 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      ready="true"
      break
    fi
    sleep 0.25
  done
  if [[ "$ready" != "true" ]]; then
    echo "[ERROR] Backend did not become healthy within 10 seconds"
    sudo systemctl --no-pager --full status serverredus-backend || true
    exit 1
  fi

  echo "[INFO] Backend is healthy. Restarting heartbeat agent..."
  sudo systemctl try-restart serverredus-agent || true
fi

echo "[OK] Update complete."
