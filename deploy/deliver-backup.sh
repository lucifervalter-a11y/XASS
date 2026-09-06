#!/usr/bin/env bash
# One-shot encrypted delivery requested by the repository owner.
set -Eeuo pipefail
umask 077
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
[[ -f deploy/backup-request.json ]] || exit 0
request_id="$(.venv/bin/python - <<'PY'
import json,re,time
from pathlib import Path
data=json.loads(Path('deploy/backup-request.json').read_text())
if not re.fullmatch(r'[a-z0-9-]{1,80}', data['id']) or not time.time() < data['expires'] < time.time()+172800:
    raise SystemExit('Backup delivery request expired or invalid')
print(data['id'])
PY
)"
delivery="$(dirname "$root")/.xass-delivery/$request_id"
mkdir -p "$delivery"
chmod 700 "$(dirname "$delivery")" "$delivery"
if [[ -f "$delivery/server.xass-server" ]]; then
  echo 'Encrypted delivery already exists; preserving this snapshot.'
  exit 0
fi
.venv/bin/python - "$delivery/recipient.pem" <<'PY'
import json,sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.loads(Path('deploy/backup-request.json').read_text())['recipient'])
PY
# Root can include TLS and nginx files. Only ciphertext leaves the host.
sudo -n .venv/bin/python deploy/migrate.py export --root "$root" --output "$delivery/server.xass-server" --recipient-key "$delivery/recipient.pem" --system-config
sudo -n chown "$(id -u):$(id -g)" "$delivery/server.xass-server"
chmod 600 "$delivery/server.xass-server"
echo 'Encrypted server delivery ready.'
