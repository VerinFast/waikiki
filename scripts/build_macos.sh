#!/usr/bin/env bash
# Build Waikiki.app (macOS) with PyInstaller, then zip it for release.
set -euo pipefail
cd "$(dirname "$0")/.."

# Build in a venv of its own, never the dev `.venv`. Claude Desktop launches
# Waikiki's MCP server from `.venv` in the source tree, so pip-installing here
# used to mutate a live dependency set: `pip install` briefly uninstalls a
# package before replacing it, and a launch landing in that window fails to
# import and the server never starts. Cutting a release must not be able to
# break the editor you are cutting it from.
BUILD_VENV=".venv-build"
python3 -m venv "$BUILD_VENV" 2>/dev/null || true
source "$BUILD_VENV/bin/activate"
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
    echo "    (This disconnects you. It happens because the app you are USING is"
    echo "     the build output. Install a copy and it stops happening for good:"
    echo "       brew install --cask waikiki --no-quarantine"
    echo "     Your wikis live in ~/Library/Application Support/Waikiki and are"
    echo "     shared, so an installed copy opens everything you already have,"
    echo "     keeps running through every release, and self-updates.)"
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

# Prove the thing we just built actually RUNS before it can become a release
# asset. v0.21.0 shipped a signed, verified bundle that could not be opened: the
# signature proved the bytes were ours, nothing proved they started. Boot it
# headless (server only, no window) on a scratch data dir and a port nothing else
# uses, then check it serves its own version.
SMOKE_PORT=8899
SMOKE_DATA="$(mktemp -d)"
echo "==> smoke test: booting the built app headless on :$SMOKE_PORT"
WAIKIKI_HEADLESS=1 WAIKIKI_DATA="$SMOKE_DATA" WAIKIKI_PORT="$SMOKE_PORT" \
    dist/Waikiki.app/Contents/MacOS/Waikiki > "$SMOKE_DATA/smoke.log" 2>&1 &
SMOKE_PID=$!
# Wait for it to actually exit before removing the dir: kill is asynchronous,
# and a still-writing process makes rm fail — which, under `set -e`, would
# abort the build over nothing but cleanup.
smoke_cleanup() {
    kill "$SMOKE_PID" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$SMOKE_PID" 2>/dev/null || break; sleep 0.25; done
    rm -rf "$SMOKE_DATA" 2>/dev/null || true
}
SMOKE_OK=""
# Generous: a cold first run may fetch the local embedding model.
for _ in $(seq 1 120); do
    kill -0 "$SMOKE_PID" 2>/dev/null || break          # died: stop waiting
    if curl -sf -m 2 -o /dev/null "http://127.0.0.1:$SMOKE_PORT/"; then SMOKE_OK=1; break; fi
    sleep 1
done
if [ -z "$SMOKE_OK" ]; then
    echo "ERROR: the built app did not serve on :$SMOKE_PORT — refusing to ship it." >&2
    echo "--- last 30 lines of its output ---" >&2
    tail -30 "$SMOKE_DATA/smoke.log" >&2 || true
    smoke_cleanup
    exit 1
fi
# There is no /api/version route; the shell renders the version beside the logo,
# which is the same string a user reads, so assert on that.
SMOKE_VER="$(curl -sf -m 5 "http://127.0.0.1:$SMOKE_PORT/" 2>/dev/null \
    | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d v)"
smoke_cleanup
if [ "$SMOKE_VER" != "$(python3 -c \
        "import re,pathlib;print(re.search(r'__version__ = \"([^\"]+)\"',pathlib.Path('waikiki/__init__.py').read_text()).group(1))")" ]; then
    echo "ERROR: the built app serves version $SMOKE_VER, not the source version." >&2
    echo "       dist/ is stale or half-replaced. Refusing to ship it." >&2
    exit 1
fi
echo "==> smoke test passed${SMOKE_VER:+ (serving $SMOKE_VER)}"

# Zip the .app for a GitHub release asset (preserves the bundle structure).
cd dist
ditto -c -k --sequesterRsrc --keepParent "Waikiki.app" "Waikiki-macos.zip"
echo "Built: dist/Waikiki.app"
echo "Asset: dist/Waikiki-macos.zip"
du -sh Waikiki.app Waikiki-macos.zip
