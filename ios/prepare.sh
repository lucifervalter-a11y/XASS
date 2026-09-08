#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
# Resize only the existing XASS artwork; no replacement artwork or network fetch.
sips -s format png -z 1024 1024 ../assets/xass-app-icon-source.png --out Resources/Assets.xcassets/AppIcon.appiconset/Icon.png >/dev/null
xcodegen generate --spec project.yml
