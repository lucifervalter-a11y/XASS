#!/usr/bin/env bash
set -Eeuo pipefail

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
  elif command_exists sudo; then
    sudo "$@"
  else
    die "Need root or sudo to install missing packages."
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

install_git_and_curl() {
  local pm="$1"
  case "$pm" in
    apt)
      run_root apt-get update
      run_root apt-get install -y git curl ca-certificates
      ;;
    dnf)
      run_root dnf install -y git curl ca-certificates
      ;;
    yum)
      run_root yum install -y git curl ca-certificates
      ;;
    pacman)
      run_root pacman -Sy --noconfirm git curl ca-certificates
      ;;
    *)
      die "Unsupported package manager. Install git + curl manually and rerun."
      ;;
  esac
}

main() {
  printf "%bXASS one-command bootstrap%b\n" "$BOLD" "$RESET"
  print_info "This script downloads/updates the project and launches install.sh"

  local pm
  pm="$(detect_package_manager)"

  if ! command_exists git || ! command_exists curl; then
    print_warn "git/curl not found. Trying to install..."
    install_git_and_curl "$pm"
  fi

  command_exists git || die "git not found"
  command_exists curl || die "curl not found"

  local default_repo="${XASS_REPO_URL:-https://github.com/lucifervalter-a11y/XASS.git}"
  local default_branch="${XASS_BRANCH:-main}"
  local default_dir="${XASS_DIR:-$HOME/serverredus}"

  local repo_url branch install_dir
  repo_url="$(prompt "Repository URL" "$default_repo")"
  branch="$(prompt "Branch" "$default_branch")"
  install_dir="$(prompt "Install directory" "$default_dir")"

  mkdir -p "$(dirname "$install_dir")"

  if [[ -d "$install_dir/.git" ]]; then
    print_info "Existing git repo found in $install_dir. Updating..."
    git -C "$install_dir" fetch --all --prune
    git -C "$install_dir" checkout "$branch"
    git -C "$install_dir" pull --ff-only origin "$branch"
  elif [[ -d "$install_dir" ]] && [[ -n "$(ls -A "$install_dir" 2>/dev/null || true)" ]]; then
    print_warn "Directory is not empty and not a git repo: $install_dir"
    if prompt_yes_no "Recreate directory and clone repository?" "n"; then
      rm -rf "$install_dir"
      git clone --branch "$branch" "$repo_url" "$install_dir"
    else
      die "Aborted by user."
    fi
  else
    print_info "Cloning repository..."
    git clone --branch "$branch" "$repo_url" "$install_dir"
  fi

  [[ -f "$install_dir/install.sh" ]] || die "install.sh not found in $install_dir"
  chmod +x "$install_dir/install.sh"

  print_info "Starting interactive installer..."
  print_info "Choose 'server' mode in the next step for production setup."

  cd "$install_dir"
  exec ./install.sh
}

main "$@"

