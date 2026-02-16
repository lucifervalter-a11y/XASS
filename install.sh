#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
AGENT_ENV_FILE="$PROJECT_ROOT/.agent.env"
VENV_DIR="$PROJECT_ROOT/.venv"
AGENT_RUN_SCRIPT="$PROJECT_ROOT/run-agent.sh"

RED="\033[31m"
YELLOW="\033[33m"
GREEN="\033[32m"
BLUE="\033[36m"
BOLD="\033[1m"
RESET="\033[0m"

print_info() {
  printf "%b[INFO]%b %s\n" "$BLUE" "$RESET" "$*"
}

print_warn() {
  printf "%b[WARN]%b %s\n" "$YELLOW" "$RESET" "$*"
}

print_error() {
  printf "%b[ERROR]%b %s\n" "$RED" "$RESET" "$*"
}

die() {
  print_error "$*"
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    if ! command_exists sudo; then
      die "sudo not found. Run as root or install sudo."
    fi
    sudo "$@"
  fi
}

prompt() {
  local message="$1"
  local default_value="${2:-}"
  local value
  if [[ -n "$default_value" ]]; then
    read -r -p "$message [$default_value]: " value
    printf "%s" "${value:-$default_value}"
  else
    read -r -p "$message: " value
    printf "%s" "$value"
  fi
}

prompt_required() {
  local message="$1"
  local default_value="${2:-}"
  local value=""
  while [[ -z "$value" ]]; do
    value="$(prompt "$message" "$default_value")"
    if [[ -z "$value" ]]; then
      print_warn "Value cannot be empty."
    fi
  done
  printf "%s" "$value"
}

prompt_yes_no() {
  local message="$1"
  local default_answer="${2:-y}"
  local suffix="[Y/n]"
  local answer
  if [[ "$default_answer" =~ ^[Nn]$ ]]; then
    suffix="[y/N]"
  fi

  while true; do
    read -r -p "$message $suffix: " answer
    answer="${answer:-$default_answer}"
    case "$answer" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) print_warn "Please answer y or n." ;;
    esac
  done
}

read_env_value() {
  local key="$1"
  local file_path="${2:-$ENV_FILE}"
  if [[ ! -f "$file_path" ]]; then
    printf ""
    return 0
  fi
  local line
  line="$(grep -E "^${key}=" "$file_path" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    printf ""
    return 0
  fi
  printf "%s" "${line#*=}"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local file_path="${3:-$ENV_FILE}"
  local escaped
  escaped="$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')"
  if [[ -f "$file_path" ]] && grep -q -E "^${key}=" "$file_path"; then
    sed -i -E "s|^${key}=.*|${key}=${escaped}|g" "$file_path"
  else
    printf "%s=%s\n" "$key" "$value" >> "$file_path"
  fi
}

random_secret() {
  "${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}

detect_package_manager() {
  if command_exists apt-get; then
    printf "apt"
    return 0
  fi
  if command_exists dnf; then
    printf "dnf"
    return 0
  fi
  if command_exists yum; then
    printf "yum"
    return 0
  fi
  if command_exists pacman; then
    printf "pacman"
    return 0
  fi
  printf "unknown"
}

install_system_packages() {
  local pm="$1"
  case "$pm" in
    apt)
      run_root apt-get update
      run_root apt-get install -y python3 python3-venv python3-pip curl tzdata
      ;;
    dnf)
      run_root dnf install -y python3 python3-pip python3-virtualenv curl tzdata
      ;;
    yum)
      run_root yum install -y python3 python3-pip python3-virtualenv curl tzdata
      ;;
    pacman)
      run_root pacman -Sy --noconfirm python python-pip curl tzdata
      ;;
    *)
      print_warn "Unsupported package manager. Install Python 3.11+, venv and curl manually."
      ;;
  esac
}

ensure_python() {
  if command_exists python3; then
    PYTHON_BIN="$(command -v python3)"
  elif command_exists python; then
    PYTHON_BIN="$(command -v python)"
  else
    die "Python not found. Install Python 3.11+ and retry."
  fi

  if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  then
    die "Python 3.11+ required. Current: $("$PYTHON_BIN" --version 2>&1)"
  fi
}

setup_venv_and_deps() {
  if [[ -d "$VENV_DIR" ]] && [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    print_warn "Existing virtualenv is incompatible for Linux (missing $VENV_DIR/bin/activate). Recreating..."
    rm -rf "$VENV_DIR"
  fi

  if [[ ! -d "$VENV_DIR" ]]; then
    print_info "Creating virtual environment..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi

  [[ -f "$VENV_DIR/bin/activate" ]] || die "Virtualenv was not created correctly at $VENV_DIR"

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip
  pip install -r "$PROJECT_ROOT/requirements.txt"
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    print_info "Creating .env from .env.example"
    cp "$PROJECT_ROOT/.env.example" "$ENV_FILE"
  fi
}

validate_repo_layout() {
  [[ -f "$PROJECT_ROOT/requirements.txt" ]] || die "requirements.txt not found in $PROJECT_ROOT"
  [[ -f "$PROJECT_ROOT/.env.example" ]] || die ".env.example not found in $PROJECT_ROOT"
  [[ -f "$PROJECT_ROOT/app/main.py" ]] || die "app/main.py not found in $PROJECT_ROOT"
  [[ -f "$PROJECT_ROOT/agent/agent.py" ]] || die "agent/agent.py not found in $PROJECT_ROOT"
}

write_common_env() {
  local mode="$1"
  local current_bot_token current_owner current_auth current_admin
  local current_agent current_setup current_path current_secret
  local current_notify current_profile_url current_timezone current_services
  local current_now_source current_iphone_key current_iphone_stale
  local current_vk_user_id current_vk_token current_vk_api current_vk_refresh

  current_bot_token="$(read_env_value BOT_TOKEN)"
  current_owner="$(read_env_value OWNER_USER_ID)"
  current_auth="$(read_env_value AUTHORIZED_USER_IDS)"
  current_admin="$(read_env_value ADMIN_USER_IDS)"
  current_agent="$(read_env_value AGENT_API_KEY)"
  current_setup="$(read_env_value SETUP_API_KEY)"
  current_path="$(read_env_value TELEGRAM_WEBHOOK_PATH)"
  current_secret="$(read_env_value TELEGRAM_SECRET_TOKEN)"
  current_notify="$(read_env_value NOTIFY_CHAT_ID)"
  current_profile_url="$(read_env_value PROFILE_PUBLIC_URL)"
  current_timezone="$(read_env_value TIMEZONE)"
  current_services="$(read_env_value MONITORED_SERVICES)"
  current_now_source="$(read_env_value NOW_PLAYING_SOURCE_DEFAULT)"
  current_iphone_key="$(read_env_value IPHONE_NOW_PLAYING_API_KEY)"
  current_iphone_stale="$(read_env_value IPHONE_NOW_PLAYING_STALE_MINUTES)"
  current_vk_user_id="$(read_env_value VK_USER_ID)"
  current_vk_token="$(read_env_value VK_ACCESS_TOKEN)"
  current_vk_api="$(read_env_value VK_API_VERSION)"
  current_vk_refresh="$(read_env_value VK_NOW_PLAYING_REFRESH_MINUTES)"

  local bot_token owner_user_id authorized_user_ids admin_user_ids
  local agent_api_key setup_api_key webhook_path webhook_secret
  local notify_chat_id profile_public_url timezone monitored_services

  bot_token="$(prompt_required "BOT_TOKEN (from BotFather)" "$current_bot_token")"
  owner_user_id="$(prompt_required "OWNER_USER_ID (your Telegram user id)" "$current_owner")"
  authorized_user_ids="$(prompt "AUTHORIZED_USER_IDS (comma-separated)" "${current_auth:-$owner_user_id}")"
  admin_user_ids="$(prompt "ADMIN_USER_IDS (comma-separated)" "${current_admin:-$owner_user_id}")"

  if [[ -z "$current_agent" ]]; then current_agent="$(random_secret)"; fi
  if [[ -z "$current_setup" ]]; then current_setup="$(random_secret)"; fi
  if [[ -z "$current_path" ]]; then current_path="$(random_secret | tr -cd '[:alnum:]' | head -c 24)"; fi
  if [[ -z "$current_secret" ]]; then current_secret="$(random_secret)"; fi
  if [[ -z "$current_iphone_key" ]]; then current_iphone_key="$(random_secret)"; fi
  if [[ -z "$current_now_source" ]]; then current_now_source="pc_agent"; fi
  if [[ -z "$current_iphone_stale" ]]; then current_iphone_stale="180"; fi
  if [[ -z "$current_vk_api" ]]; then current_vk_api="5.199"; fi
  if [[ -z "$current_vk_refresh" ]]; then current_vk_refresh="2"; fi

  agent_api_key="$(prompt_required "AGENT_API_KEY" "$current_agent")"
  setup_api_key="$(prompt_required "SETUP_API_KEY" "$current_setup")"
  webhook_path="$(prompt_required "TELEGRAM_WEBHOOK_PATH (random slug)" "$current_path")"
  webhook_secret="$(prompt_required "TELEGRAM_SECRET_TOKEN" "$current_secret")"

  notify_chat_id="$(prompt "NOTIFY_CHAT_ID (optional)" "$current_notify")"
  profile_public_url="$(prompt "PROFILE_PUBLIC_URL (optional, e.g. https://site.tld/profile.php)" "$current_profile_url")"
  timezone="$(prompt "TIMEZONE" "${current_timezone:-UTC}")"
  monitored_services="$(prompt "MONITORED_SERVICES (comma-separated)" "${current_services:-nginx,postgresql,docker}")"

  set_env_value BOT_TOKEN "$bot_token"
  set_env_value OWNER_USER_ID "$owner_user_id"
  set_env_value AUTHORIZED_USER_IDS "$authorized_user_ids"
  set_env_value ADMIN_USER_IDS "$admin_user_ids"
  set_env_value AGENT_API_KEY "$agent_api_key"
  set_env_value SETUP_API_KEY "$setup_api_key"
  set_env_value TELEGRAM_WEBHOOK_PATH "$webhook_path"
  set_env_value TELEGRAM_SECRET_TOKEN "$webhook_secret"
  set_env_value NOTIFY_CHAT_ID "$notify_chat_id"
  set_env_value PROFILE_PUBLIC_URL "$profile_public_url"
  set_env_value TIMEZONE "$timezone"
  set_env_value MONITORED_SERVICES "$monitored_services"
  set_env_value NOW_PLAYING_SOURCE_DEFAULT "$current_now_source"
  set_env_value IPHONE_NOW_PLAYING_API_KEY "$current_iphone_key"
  set_env_value IPHONE_NOW_PLAYING_STALE_MINUTES "$current_iphone_stale"
  set_env_value VK_USER_ID "$current_vk_user_id"
  set_env_value VK_ACCESS_TOKEN "$current_vk_token"
  set_env_value VK_API_VERSION "$current_vk_api"
  set_env_value VK_NOW_PLAYING_REFRESH_MINUTES "$current_vk_refresh"

  set_env_value PROFILE_JSON_PATH "./data/profile.json"
  set_env_value PROFILE_BACKUPS_DIR "./data/backups"
  set_env_value PROFILE_AUDIT_LOG_PATH "./data/audit.log"
  set_env_value PROFILE_AVATARS_DIR "./data/avatars"
  set_env_value MEDIA_ROOT "./data/media"
  set_env_value EXPORT_ROOT "./data/exports"
  set_env_value HEARTBEAT_CHECK_INTERVAL_SEC "30"
  set_env_value POLLING_REQUEST_TIMEOUT_SEC "25"
  set_env_value POLLING_RETRY_DELAY_SEC "2"
  set_env_value POLLING_DROP_PENDING_UPDATES "false"

  if [[ "$mode" == "local" ]]; then
    set_env_value USE_POLLING "true"
  else
    set_env_value USE_POLLING "false"
  fi
}

write_agent_env() {
  local current_server_url current_api_key current_source_name
  local current_source_type current_interval current_inc_process
  local current_disable_np current_disable_activity current_trust_proxy

  current_server_url="$(read_env_value AGENT_SERVER_URL "$AGENT_ENV_FILE")"
  current_api_key="$(read_env_value AGENT_API_KEY "$AGENT_ENV_FILE")"
  current_source_name="$(read_env_value AGENT_SOURCE_NAME "$AGENT_ENV_FILE")"
  current_source_type="$(read_env_value AGENT_SOURCE_TYPE "$AGENT_ENV_FILE")"
  current_interval="$(read_env_value AGENT_INTERVAL_SEC "$AGENT_ENV_FILE")"
  current_inc_process="$(read_env_value AGENT_INCLUDE_PROCESSES "$AGENT_ENV_FILE")"
  current_disable_np="$(read_env_value AGENT_DISABLE_NOW_PLAYING "$AGENT_ENV_FILE")"
  current_disable_activity="$(read_env_value AGENT_DISABLE_ACTIVITY "$AGENT_ENV_FILE")"
  current_trust_proxy="$(read_env_value AGENT_TRUST_ENV_PROXY "$AGENT_ENV_FILE")"

  if [[ -z "$current_api_key" ]]; then
    current_api_key="$(read_env_value AGENT_API_KEY "$ENV_FILE")"
  fi
  if [[ -z "$current_api_key" ]]; then
    current_api_key="$(random_secret)"
  fi

  local default_source_name
  default_source_name="$(hostname)"

  local server_url api_key source_name source_type interval_sec
  server_url="$(prompt_required "AGENT server URL" "${current_server_url:-http://127.0.0.1:8000}")"
  api_key="$(prompt_required "AGENT_API_KEY" "$current_api_key")"
  source_name="$(prompt_required "Source name for this machine" "${current_source_name:-$default_source_name}")"
  source_type="$(prompt_required "Source type (PC_AGENT or SERVER_AGENT)" "${current_source_type:-PC_AGENT}")"
  interval_sec="$(prompt_required "Heartbeat interval seconds" "${current_interval:-30}")"
  [[ "$interval_sec" =~ ^[0-9]+$ ]] || die "Interval must be numeric."

  local include_processes="false"
  if prompt_yes_no "Include top processes in heartbeat payload?" "${current_inc_process:-n}"; then
    include_processes="true"
  fi

  local disable_now_playing="false"
  if prompt_yes_no "Disable now playing collection?" "${current_disable_np:-n}"; then
    disable_now_playing="true"
  fi

  local disable_activity="false"
  if prompt_yes_no "Disable active window/activity collection?" "${current_disable_activity:-n}"; then
    disable_activity="true"
  fi

  local trust_env_proxy="false"
  if prompt_yes_no "Use system proxy settings for agent HTTP?" "${current_trust_proxy:-n}"; then
    trust_env_proxy="true"
  fi

  cat > "$AGENT_ENV_FILE" <<EOF
AGENT_SERVER_URL=${server_url}
AGENT_API_KEY=${api_key}
AGENT_SOURCE_NAME=${source_name}
AGENT_SOURCE_TYPE=${source_type}
AGENT_INTERVAL_SEC=${interval_sec}
AGENT_INCLUDE_PROCESSES=${include_processes}
AGENT_DISABLE_NOW_PLAYING=${disable_now_playing}
AGENT_DISABLE_ACTIVITY=${disable_activity}
AGENT_TRUST_ENV_PROXY=${trust_env_proxy}
EOF
}

write_agent_runner_script() {
  cat > "$AGENT_RUN_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_ACTIVATE="$ROOT_DIR/.venv/bin/activate"
AGENT_ENV="$ROOT_DIR/.agent.env"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "[ERROR] virtualenv not found at $VENV_ACTIVATE"
  exit 1
fi

if [[ ! -f "$AGENT_ENV" ]]; then
  echo "[ERROR] agent config not found at $AGENT_ENV"
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV_ACTIVATE"
set -a
# shellcheck disable=SC1090
source "$AGENT_ENV"
set +a

cmd=(
  python "$ROOT_DIR/agent/agent.py"
  --server-url "$AGENT_SERVER_URL"
  --api-key "$AGENT_API_KEY"
  --source-name "$AGENT_SOURCE_NAME"
  --source-type "$AGENT_SOURCE_TYPE"
  --interval-sec "$AGENT_INTERVAL_SEC"
)

if [[ "${AGENT_INCLUDE_PROCESSES:-false}" == "true" ]]; then
  cmd+=(--include-processes)
fi
if [[ "${AGENT_DISABLE_NOW_PLAYING:-false}" == "true" ]]; then
  cmd+=(--disable-now-playing)
fi
if [[ "${AGENT_DISABLE_ACTIVITY:-false}" == "true" ]]; then
  cmd+=(--disable-activity)
fi
if [[ "${AGENT_TRUST_ENV_PROXY:-false}" == "true" ]]; then
  cmd+=(--trust-env-proxy)
fi

exec "${cmd[@]}"
EOF
  chmod +x "$AGENT_RUN_SCRIPT"
}

install_backend_and_server_agent_service() {
  local port="$1"
  local enable_agent="$2"
  local server_agent_name="$3"
  local run_user="$4"
  local run_group="$5"

  local backend_service="/etc/systemd/system/serverredus-backend.service"
  local agent_service="/etc/systemd/system/serverredus-agent.service"

  print_info "Writing systemd backend service: $backend_service"
  run_root tee "$backend_service" >/dev/null <<EOF
[Unit]
Description=Serverredus Telegram Business Backend
After=network.target

[Service]
Type=simple
User=${run_user}
Group=${run_group}
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${PROJECT_ROOT}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${port}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  if [[ "$enable_agent" == "yes" ]]; then
    print_info "Writing systemd server-agent service: $agent_service"
    run_root tee "$agent_service" >/dev/null <<EOF
[Unit]
Description=Serverredus Heartbeat Agent (Server)
After=network.target

[Service]
Type=simple
User=${run_user}
Group=${run_group}
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${PROJECT_ROOT}/.venv/bin/python agent/agent.py --server-url http://127.0.0.1:${port} --api-key \${AGENT_API_KEY} --source-name ${server_agent_name} --source-type SERVER_AGENT --interval-sec 30
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  fi

  run_root systemctl daemon-reload
  run_root systemctl enable --now serverredus-backend
  if [[ "$enable_agent" == "yes" ]]; then
    run_root systemctl enable --now serverredus-agent
  fi
}

install_agent_client_service() {
  local run_user="$1"
  local run_group="$2"
  local service_file="/etc/systemd/system/serverredus-agent-client.service"

  print_info "Writing systemd agent-client service: $service_file"
  run_root tee "$service_file" >/dev/null <<EOF
[Unit]
Description=Serverredus Agent Client
After=network.target

[Service]
Type=simple
User=${run_user}
Group=${run_group}
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${AGENT_RUN_SCRIPT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  run_root systemctl daemon-reload
  run_root systemctl enable --now serverredus-agent-client
}

run_local_or_server_install() {
  local mode="$1"
  write_common_env "$mode"

  local port
  port="$(prompt "Backend port" "8000")"
  [[ "$port" =~ ^[0-9]+$ ]] || die "Port must be numeric."

  if [[ "$mode" == "server" ]]; then
    local install_systemd run_user run_group enable_agent server_agent_name

    if prompt_yes_no "Install systemd services now?" "y"; then
      install_systemd="yes"
    else
      install_systemd="no"
    fi

    if [[ "$install_systemd" == "yes" ]]; then
      run_user="$(prompt "Service user" "${SUDO_USER:-$USER}")"
      run_group="$(id -gn "$run_user" 2>/dev/null || true)"
      run_group="${run_group:-$run_user}"
      if prompt_yes_no "Enable server heartbeat agent service?" "y"; then
        enable_agent="yes"
        server_agent_name="$(prompt "Server agent source name" "$(hostname)-server")"
      else
        enable_agent="no"
        server_agent_name="$(hostname)-server"
      fi
      install_backend_and_server_agent_service "$port" "$enable_agent" "$server_agent_name" "$run_user" "$run_group"
    fi

    printf "\n%bDone.%b\n" "$GREEN" "$RESET"
    print_info "Environment written to $ENV_FILE"
    print_info "USE_POLLING=false (webhook mode)"
    print_info "Run backend in foreground:"
    printf "  cd %s && source .venv/bin/activate && uvicorn app.main:app --host 0.0.0.0 --port %s\n" "$PROJECT_ROOT" "$port"
    print_info "Webhook setup example:"
    printf "  curl -X POST \"https://YOUR_HOST/telegram/setup-webhook\" -H \"X-Api-Key: \$(grep ^SETUP_API_KEY= .env | cut -d= -f2-)\" -H \"Content-Type: application/json\" -d '{\"public_base_url\":\"https://YOUR_HOST\"}'\n"
    if [[ "$install_systemd" == "yes" ]]; then
      print_info "Service status:"
      printf "  sudo systemctl status serverredus-backend\n"
      if [[ "${enable_agent:-no}" == "yes" ]]; then
        printf "  sudo systemctl status serverredus-agent\n"
      fi
    fi
  else
    printf "\n%bDone.%b\n" "$GREEN" "$RESET"
    print_info "Environment written to $ENV_FILE"
    print_info "USE_POLLING=true (local mode)"
    print_info "Run backend:"
    printf "  cd %s && source .venv/bin/activate && uvicorn app.main:app --host 0.0.0.0 --port %s\n" "$PROJECT_ROOT" "$port"
    print_info "Optional local agent:"
    printf "  cd %s && source .venv/bin/activate && python agent/agent.py --server-url http://127.0.0.1:%s --api-key \$(grep ^AGENT_API_KEY= .env | cut -d= -f2-) --source-name local-pc --source-type PC_AGENT --interval-sec 30\n" "$PROJECT_ROOT" "$port"
  fi
}

run_agent_only_install() {
  write_agent_env
  write_agent_runner_script

  local install_systemd="no"
  local run_user run_group

  if prompt_yes_no "Install systemd auto-start service for agent?" "y"; then
    install_systemd="yes"
    run_user="$(prompt "Service user" "${SUDO_USER:-$USER}")"
    run_group="$(id -gn "$run_user" 2>/dev/null || true)"
    run_group="${run_group:-$run_user}"
    install_agent_client_service "$run_user" "$run_group"
  fi

  printf "\n%bDone.%b\n" "$GREEN" "$RESET"
  print_info "Agent config written to $AGENT_ENV_FILE"
  print_info "Runner script: $AGENT_RUN_SCRIPT"
  print_info "Run agent manually:"
  printf "  cd %s && ./run-agent.sh\n" "$PROJECT_ROOT"
  if [[ "$install_systemd" == "yes" ]]; then
    print_info "Service status:"
    printf "  sudo systemctl status serverredus-agent-client\n"
  fi
}

main() {
  cd "$PROJECT_ROOT"
  validate_repo_layout

  printf "%bServerredus Installer%b\n" "$BOLD" "$RESET"
  print_info "Project root: $PROJECT_ROOT"
  print_info "Modes: local (polling), server (webhook + systemd), agent (heartbeat-only)"

  local package_manager
  package_manager="$(detect_package_manager)"
  if prompt_yes_no "Install/upgrade system dependencies (python3, venv, pip, curl)?" "y"; then
    install_system_packages "$package_manager"
  fi

  ensure_python
  ensure_env_file
  setup_venv_and_deps

  local mode
  mode="$(prompt "Install mode (local/server/agent)" "local")"
  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')"

  case "$mode" in
    local|server)
      run_local_or_server_install "$mode"
      ;;
    agent)
      run_agent_only_install
      ;;
    *)
      die "Invalid mode: $mode (expected local, server, or agent)"
      ;;
  esac
}

main "$@"
