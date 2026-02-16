#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-/opt/serverredus}"
BACKUP_DIR="${2:-/opt/serverredus-backups}"
TS="$(date +%Y%m%d_%H%M%S)"

mkdir -p "${BACKUP_DIR}"

if [[ -f "${ROOT_DIR}/data/serverredus.db" ]]; then
  cp "${ROOT_DIR}/data/serverredus.db" "${BACKUP_DIR}/serverredus_${TS}.db"
fi

if [[ -d "${ROOT_DIR}/data/media" ]]; then
  tar -czf "${BACKUP_DIR}/media_${TS}.tar.gz" -C "${ROOT_DIR}/data" media
fi

echo "Backup complete: ${BACKUP_DIR}"

