#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

command -v git >/dev/null 2>&1 || { echo "[ERROR] git is not installed"; exit 1; }

echo "[INFO] Fetching origin/main..."
before="$(git rev-parse HEAD)"
git fetch --prune origin main
git merge --ff-only origin/main
after="$(git rev-parse HEAD)"
mapfile -t changed_files < <(git diff --name-only "$before" "$after")

dependencies_changed="false"
backend_changed="false"
agent_changed="false"
for file in "${changed_files[@]}"; do
  case "$file" in
    requirements.txt|bootstrap_server_dependencies.py)
      dependencies_changed="true"
      backend_changed="true"
      ;;
    app/*)
      backend_changed="true"
      ;;
    agent/*)
      agent_changed="true"
      ;;
  esac
done

if [[ ! -x ".venv/bin/python" || "$dependencies_changed" == "true" ]]; then
  echo "[INFO] Checking Python dependencies..."
  if [[ -x ".venv/bin/python" ]]; then
    .venv/bin/python bootstrap_server_dependencies.py
  else
    command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 is not installed"; exit 1; }
    python3 bootstrap_server_dependencies.py
  fi
else
  echo "[INFO] Python dependencies unchanged; skipping install."
fi

if command -v systemctl >/dev/null 2>&1; then
  if [[ "$backend_changed" == "true" ]]; then
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

    echo "[INFO] Backend is healthy."
  else
    echo "[INFO] Backend code unchanged; skipping restart."
  fi

  if [[ "$backend_changed" == "true" || "$agent_changed" == "true" ]]; then
    echo "[INFO] Restarting heartbeat agent..."
    sudo systemctl try-restart serverredus-agent || true
  else
    echo "[INFO] Heartbeat agent unchanged; skipping restart."
  fi
fi

echo "[OK] Update complete."
