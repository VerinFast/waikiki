#!/usr/bin/env bash
# Build Waikiki.app (macOS) with PyInstaller, then zip it for release.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
pip install -q pywebview pyinstaller

# A running instance must be quit BEFORE the bundle is deleted below. Rebuilding
# over a live app leaves that process executing a deleted inode, and LaunchServices
# then treats Waikiki as "already running" — so clicking the icon silently
# activates the zombie instead of launching the new build, which looks exactly
# like the app refusing to open. Quit it gracefully (never SIGKILL): the shutdown
# path flushes live CRDT edits, so an in-progress edit is saved rather than lost.
if pgrep -f "$PWD/dist/Waikiki.app/Contents/MacOS/Waikiki" >/dev/null 2>&1; then
    echo "==> Waikiki is running from dist/ — quitting it before the rebuild"
    osascript -e 'quit app "Waikiki"' >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
        pgrep -f "$PWD/dist/Waikiki.app/Contents/MacOS/Waikiki" >/dev/null 2>&1 || break
        sleep 0.25
    done
    if pgrep -f "$PWD/dist/Waikiki.app/Contents/MacOS/Waikiki" >/dev/null 2>&1; then
        echo "ERROR: Waikiki is still running and would be rebuilt out from under" >&2
        echo "       itself. Quit it and re-run. (Not killing it — that would drop" >&2
        echo "       unsaved edits.)" >&2
        exit 1
    fi
fi

rm -rf build dist
pyinstaller --noconfirm --clean waikiki.spec

# Ad-hoc sign (no Apple Developer cert available for real notarization). This
# quiets some macOS security checks but does NOT bypass Gatekeeper for a
# downloaded app — first launch still needs right-click -> Open.
codesign --force --deep --sign - dist/Waikiki.app 2>/dev/null || true

# Zip the .app for a GitHub release asset (preserves the bundle structure).
cd dist
ditto -c -k --sequesterRsrc --keepParent "Waikiki.app" "Waikiki-macos.zip"
echo "Built: dist/Waikiki.app"
echo "Asset: dist/Waikiki-macos.zip"
du -sh Waikiki.app Waikiki-macos.zip
