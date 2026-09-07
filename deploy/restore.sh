#!/usr/bin/env bash
# Ubuntu 24.04+/Debian 12+: restore into a new directory and prepare services.
# Usage: sudo bash deploy/restore.sh /private/xass.tar.gz /opt/serverredus example.com
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $# -eq 3 ]] || { echo "Usage: sudo bash deploy/restore.sh ARCHIVE EMPTY_TARGET DOMAIN" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "Run this deployment command with sudo." >&2; exit 2; }
command -v python3 >/dev/null || { echo "Install python3 (3.11+) first." >&2; exit 2; }
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
command -v apt-get >/dev/null || { echo "Automatic setup supports Debian/Ubuntu. Other systems: use migrate.py restore and docs/MIGRATION.md." >&2; exit 2; }

ARCHIVE="$(realpath -- "$1")"
TARGET="$(realpath -m -- "$2")"
DOMAIN="$3"
[[ "$TARGET" =~ ^/[A-Za-z0-9_./-]+$ && "$TARGET" != / && "$TARGET" != /opt && "$TARGET" != /home && "$TARGET" != /root && "$TARGET" != /usr && "$TARGET" != /var ]] || { echo "Choose a dedicated installation directory, e.g. /opt/serverredus." >&2; exit 2; }
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$DOMAIN" == *.* ]] || { echo "Use a plain domain name, without https:// or a path." >&2; exit 2; }
[[ ! -e /etc/systemd/system/serverredus-backend.service && ! -e /etc/nginx/sites-available/xass ]] || { echo "An XASS service/site already exists. Use a fresh destination server; existing services were not changed." >&2; exit 2; }

# Restore verifies all checksums and rejects a nonempty/symlink target before
# installing packages or changing any service. It never runs archived code.
python3 "$SCRIPT_DIR/migrate.py" restore "$ARCHIVE" --target "$TARGET"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip git nginx php-fpm curl ca-certificates certbot python3-certbot-nginx
python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/python" -m pip install --disable-pip-version-check -r "$TARGET/requirements.txt"

# Initialize update tracking without replacing the restored source files.
REVISION="$("$TARGET/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("source_revision", ""))' "$TARGET/data/migration-receipt.json")"
git -C "$TARGET" init -b main
git -C "$TARGET" remote add origin https://github.com/lucifervalter-a11y/XASS.git
if [[ "$REVISION" =~ ^[0-9a-f]{40,64}$ ]] && git -C "$TARGET" fetch --depth=1 origin "$REVISION"; then
  git -C "$TARGET" reset --mixed FETCH_HEAD
else
  echo "Source is restored; update history unavailable. Reconnect origin when GitHub becomes reachable."
fi

PHP_SOCKET="$(find /run/php -maxdepth 1 -type s -name 'php*-fpm.sock' -print -quit)"
[[ -n "$PHP_SOCKET" ]] || { echo "PHP-FPM socket not found. Start php-fpm and follow docs/MIGRATION.md; restored data remains at $TARGET." >&2; exit 1; }
cd "$TARGET"
PUBLIC_CONFIG="$("$TARGET/.venv/bin/python" - <<'PY'
from pathlib import Path
import re
from app.config import Settings
settings = Settings()
for key in ('profile_json_path', 'projects_json_path', 'quotes_json_path', 'site_config_json_path', 'profile_avatars_dir'):
    value = Path(getattr(settings, key)).resolve().relative_to(Path.cwd()).as_posix()
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
        raise SystemExit('Custom public paths need manual nginx setup; see docs/MIGRATION.md')
    print(value)
PY
)"
mapfile -t PUBLIC_PATHS <<< "$PUBLIC_CONFIG"

# TARGET was resolved and checked above, is a newly restored directory, and
# the importer forbids symlinks. No other installation is touched.
chown -R www-data:www-data -- "$TARGET"
chmod 755 -- "$TARGET"
chmod 600 -- "$TARGET/.env"

cat > /etc/systemd/system/serverredus-backend.service <<EOF
[Unit]
Description=XASS personal control server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=$TARGET
Environment=PYTHONUNBUFFERED=1
Environment=SERVICE_RESTART_MODE=systemd
Environment=SYSTEMD_SERVICE_NAME=serverredus-backend
ExecStart=$TARGET/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=2
TimeoutStopSec=15
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

# Serve only public assets/PHP, never source, backups or .env. APIs go straight
# to localhost, avoiding PHP response-envelope/status ambiguity for PC agents.
cat > /etc/nginx/sites-available/xass <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    root $TARGET;
    index index.php;
    client_max_body_size 40m;
    location ~ /\. { deny all; }
    location ^~ /${PUBLIC_PATHS[4]}/ {
        default_type application/octet-stream;
        add_header X-Content-Type-Options nosniff;
        try_files \$uri =404;
    }
    location ~ ^/(app|pc_client|deploy|docs|tests|data)(/|$) { return 404; }
    location ~ \.(py|sh|bat|md|txt|ya?ml|toml|ini|log|db|sqlite3?|tar|gz|zst|env|dump)$ { return 404; }
    location ~ ^/(api|agent|telegram)(/|$) {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_read_timeout 75s;
        proxy_request_buffering off;
    }
    location = /health { proxy_pass http://127.0.0.1:8000; }
    location = /sw.js { add_header Cache-Control "no-cache"; }
    location = /manifest.webmanifest { add_header Cache-Control "no-cache"; }
    location ~ \.php$ {
        try_files \$uri =404;
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:$PHP_SOCKET;
        fastcgi_param HTTP_X_FORWARDED_PROTO \$scheme;
        fastcgi_param PROFILE_JSON_PATH $TARGET/${PUBLIC_PATHS[0]};
        fastcgi_param PROJECTS_JSON_PATH $TARGET/${PUBLIC_PATHS[1]};
        fastcgi_param QUOTES_JSON_PATH $TARGET/${PUBLIC_PATHS[2]};
        fastcgi_param SITE_CONFIG_JSON_PATH $TARGET/${PUBLIC_PATHS[3]};
    }
    location /assets/ { try_files \$uri =404; }
    location / { try_files \$uri \$uri/ /index.php?\$query_string; }
}
EOF
ln -s /etc/nginx/sites-available/xass /etc/nginx/sites-enabled/xass
nginx -t
systemctl daemon-reload
systemctl enable --now serverredus-backend
systemctl reload nginx
READY=false
for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8000/health >/dev/null; then READY=true; break; fi
  sleep 1
done
[[ "$READY" == true ]] || { echo "Backend has not become ready. Run: journalctl -u serverredus-backend -n 80 --no-pager" >&2; exit 1; }
printf '\nXASS restored at %s. Backend health: OK.\n' "$TARGET"
printf 'Keep the old server stopped, point %s to this server, then enable HTTPS:\n' "$DOMAIN"
printf 'sudo certbot --nginx -d %s\n' "$DOMAIN"
printf 'Finally open https://%s/miniapp.php and run diagnostics. See %s/docs/MIGRATION.md\n' "$DOMAIN" "$TARGET"
