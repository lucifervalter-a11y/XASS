#!/usr/bin/env bash
set -euo pipefail
# Apply the VK/proxy security wiring to app/main.py on a clean tree.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
patch -p1 -d "$ROOT" < "$ROOT/patches/2026-09-05-main-vk-proxy-security.patch"
echo "Applied patches/2026-09-05-main-vk-proxy-security.patch"
