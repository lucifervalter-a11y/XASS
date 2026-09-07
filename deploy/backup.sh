#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-/opt/serverredus}"
BACKUP_DIR="${2:-/opt/serverredus-backups}"
mkdir -p "${BACKUP_DIR}"
umask 077
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi
exec "${PYTHON_BIN}" "${ROOT_DIR}/deploy/migrate.py" export --root "${ROOT_DIR}" --output "${BACKUP_DIR}"

