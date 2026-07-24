#!/usr/bin/env bash
# Build Waikiki.app (macOS) with PyInstaller, then zip it for release.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
pip install -q pywebview pyinstaller

rm -rf build dist
pyinstaller --noconfirm --clean waikiki.spec

# Zip the .app for a GitHub release asset (preserves the bundle structure).
cd dist
ditto -c -k --sequesterRsrc --keepParent "Waikiki.app" "Waikiki-macos.zip"
echo "Built: dist/Waikiki.app"
echo "Asset: dist/Waikiki-macos.zip"
du -sh Waikiki.app Waikiki-macos.zip
