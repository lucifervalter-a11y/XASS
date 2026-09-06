#!/usr/bin/env bash
# Fresh Ubuntu/Debian host only. Existing XASS units/configuration are never overwritten.
set -Eeuo pipefail
umask 077
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
domain="${1:-}"
[[ "${2:-}" == '--source-stopped' ]] || { echo 'Stop the old backend and agent first, then pass --source-stopped'; exit 1; }
[[ "$EUID" == 0 ]] || { echo 'Run with sudo on the NEW server'; exit 1; }
[[ "$domain" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ && "$domain" == *.* ]] || { echo 'Specify the public domain, without https:// or a path'; exit 1; }
[[ "$root" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Installation path must contain only letters, digits, /, _, . and -'; exit 1; }
[[ -f "$root/.migration-pending" ]] || { echo 'No staged migration found'; exit 1; }
[[ ! -e /etc/systemd/system/serverredus-backend.service && ! -e /etc/nginx/sites-available/xass-migrated ]] || { echo 'Existing server configuration found; refusing to overwrite it'; exit 1; }
apt-get update
apt-get install -y nginx php-fpm python3-venv git sudo
id xass >/dev/null 2>&1 || useradd --system --home-dir "$root" --shell /usr/sbin/nologin xass
python3 -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install -r "$root/requirements.txt"
php_socket="$(find /run/php -maxdepth 1 -name 'php*-fpm.sock' -print | sort | head -n 1)"
[[ -S "$php_socket" ]] || { echo 'Start PHP-FPM, then rerun this script'; exit 1; }
backup_dir="$(cd "$root" && .venv/bin/python -c 'from pathlib import Path; from app.config import Settings; print(Path(Settings().server_backup_dir).resolve())')"
install -d -m 700 -o xass -g www-data "$backup_dir"
chown -R xass:www-data "$root"
# Public PHP/assets must be readable by FPM; secrets remain owner-only.
find "$root" -path "$root/.venv" -prune -o -type d -exec chmod 750 {} +
find "$root" -path "$root/.venv" -prune -o -type f -exec chmod g+r {} +
chmod 600 "$root/.env"
[[ ! -f "$root/.env.source" ]] || chmod 600 "$root/.env.source"
sudoers_file=/etc/sudoers.d/xass-serverredus
[[ ! -e "$sudoers_file" ]] || { echo 'Existing XASS sudoers file found; review it before installation'; exit 1; }
printf '%s\n' 'xass ALL=(root) NOPASSWD: /usr/bin/systemctl restart serverredus-backend' > "$sudoers_file"
chmod 440 "$sudoers_file"
visudo -cf "$sudoers_file"
cat > /etc/systemd/system/serverredus-backend.service <<EOF
[Unit]
Description=XASS migrated backend
After=network.target
[Service]
Type=simple
User=xass
Group=www-data
WorkingDirectory=$root
EnvironmentFile=$root/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$root/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/nginx/sites-available/xass-migrated <<EOF
server {
    listen 80;
    server_name $domain;
    root $root;
    index index.php;
    client_max_body_size 64m;
    location ~ /\. { deny all; }
    location ~ ^/data/avatars/[A-Za-z0-9_.-]+\.(png|jpg|jpeg|webp)$ { try_files \$uri =404; }
    location ~ ^/(app|agent|pc_client|tests|deploy|docs|data|restored|migration-system)(/|$) { deny all; }
    location ~ ^/(migration-manifest\.json|requirements\.txt|.*\.(db|sqlite3|xass-server))$ { deny all; }
    location /telegram/ { proxy_pass http://127.0.0.1:8000; }
    location /api/ { proxy_pass http://127.0.0.1:8000; proxy_set_header X-Forwarded-Host \$host; proxy_set_header X-Forwarded-Proto \$scheme; }
    location ^~ /agent/ { proxy_pass http://127.0.0.1:8000; }
    location /health { proxy_pass http://127.0.0.1:8000; }
    location / { try_files \$uri \$uri/ =404; }
    location ~ \.php$ {
        try_files \$uri =404;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_pass unix:$php_socket;
        fastcgi_read_timeout 1800;
        fastcgi_buffering off;
    }
}
EOF
ln -s /etc/nginx/sites-available/xass-migrated /etc/nginx/sites-enabled/xass-migrated
nginx -t
"$root/.venv/bin/python" "$root/deploy/migrate.py" activate --root "$root" --source-stopped
systemctl daemon-reload
systemctl enable --now serverredus-backend
systemctl reload nginx
echo "Backend started. Point $domain to this host and configure HTTPS before using the Mini App."
echo 'The original nginx/TLS configuration, when included, is under migration-system for review.'
echo 'Reconfigure the heartbeat agent and GitHub deployment SSH secrets for this new host.'
