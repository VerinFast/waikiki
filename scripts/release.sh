#!/usr/bin/env bash
#
# Build, sign, and publish a Waikiki release.
#
#   ./scripts/release.sh --genkey        # one-time: make the update signing key
#   ./scripts/release.sh v0.14.0         # build + sign + upload assets to a tag
#   ./scripts/release.sh v0.14.0 --dry-run   # everything except the upload
#
# The updater refuses any download that isn't signed by the key pinned in
# waikiki/updater.py (PUBLIC_KEY_HEX), so a release without these assets is a
# release nobody can install. See docs/updates.md.
#
# The PRIVATE key never lives in this repo. It is read from WAIKIKI_UPDATE_KEY
# (default ~/.waikiki/update-key.pem) and this script refuses to use one stored
# inside the working tree, where it could be committed by accident.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_PATH="${WAIKIKI_UPDATE_KEY:-$HOME/.waikiki/update-key.pem}"
PY="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

# --- One-time key generation --------------------------------------------------
if [ "${1:-}" = "--genkey" ]; then
    [ -e "$KEY_PATH" ] && die "$KEY_PATH already exists — refusing to overwrite a signing key"
    mkdir -p "$(dirname "$KEY_PATH")"
    "$PY" - "$KEY_PATH" <<'PYCODE'
import sys, pathlib
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519

path = pathlib.Path(sys.argv[1])
priv = ed25519.Ed25519PrivateKey.generate()
path.write_bytes(priv.private_bytes(
    ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()))
path.chmod(0o600)
pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
print("\nprivate key written to:", path, "(mode 600 — never commit this)")
print("\nPaste this into waikiki/updater.py as PUBLIC_KEY_HEX:\n")
print(f'PUBLIC_KEY_HEX = "{pub.hex()}"\n')
PYCODE
    echo "Back up the private key somewhere safe. Losing it means you cannot"
    echo "ship an update that existing installs will accept."
    exit 0
fi

# --- Release ------------------------------------------------------------------
TAG="${1:-}"
DRY_RUN=""
[ "${2:-}" = "--dry-run" ] && DRY_RUN="1"
[ -n "$TAG" ] || die "usage: $(basename "$0") <tag> [--dry-run] | --genkey"

cd "$REPO_ROOT"
VERSION="${TAG#v}"

# The tag must match what the app reports, or the updater's staging check
# ("bundle version X does not match release Y") rejects our own build.
CODE_VERSION="$("$PY" -c 'import waikiki; print(waikiki.__version__)')"
[ "$CODE_VERSION" = "$VERSION" ] || \
    die "tag $TAG disagrees with waikiki.__version__ ($CODE_VERSION)"

[ -f "$KEY_PATH" ] || die "no signing key at $KEY_PATH — run: $(basename "$0") --genkey"
case "$(cd "$(dirname "$KEY_PATH")" && pwd)" in
    "$REPO_ROOT"|"$REPO_ROOT"/*)
        die "signing key lives inside the repo ($KEY_PATH) — move it outside" ;;
esac

# build_macos.sh owns the build: PyInstaller, the ad-hoc codesign, and the zip.
# Reuse it so a released bundle is byte-for-byte the thing `build_macos.sh`
# produces, rather than a second, subtly different build path.
ZIP="dist/Waikiki-macos.zip"
SIG="$ZIP.sig"

say "building via scripts/build_macos.sh"
rm -f "$SIG"
bash "$REPO_ROOT/scripts/build_macos.sh"
[ -d dist/Waikiki.app ] || die "build produced no dist/Waikiki.app"
[ -f "$ZIP" ] || die "build produced no $ZIP"

say "signing"
# Signs the zip's streamed SHA-256, NOT the archive bytes — updater.verify_file
# checks exactly that, so the two must stay in lockstep. Signing the digest lets
# a ~100MB download be verified in constant memory.
"$PY" - "$KEY_PATH" "$ZIP" "$SIG" <<'PYCODE'
import pathlib, sys
from cryptography.hazmat.primitives import serialization as ser

from waikiki import updater

key_path, zip_path, sig_path = (pathlib.Path(p) for p in sys.argv[1:4])
priv = ser.load_pem_private_key(key_path.read_bytes(), password=None)
digest = updater.file_digest(zip_path)          # the value the client verifies
sig = priv.sign(digest)
sig_path.write_text(sig.hex() + "\n")
print(f"    sha256:    {digest.hex()}")
print(f"    signature: {sig_path} ({len(sig)} bytes, hex-encoded)")
PYCODE

# Verify against the key actually pinned in this build, not just the private key
# we signed with. Catches the case where updater.py still holds an old public key
# (or none at all) — which would ship a release every client refuses.
say "verifying against the keys pinned in this build"
"$PY" - "$ZIP" "$SIG" <<'PYCODE'
import pathlib, sys
from waikiki import updater

zip_path, sig_path = (pathlib.Path(p) for p in sys.argv[1:3])
keys = updater._pinned_keys()
if not keys:
    sys.exit("PUBLIC_KEYS_HEX is empty in waikiki/updater.py — this build cannot\n"
             "verify updates. Paste the public half from --genkey and rebuild.")
sig = updater._parse_signature(sig_path.read_bytes())
signer = updater.verifying_key(updater.file_digest(zip_path), sig)
if signer is None:
    sys.exit(f"signature does NOT verify against any of the {len(keys)} key(s)\n"
             "pinned in this build. The private key used here matches none of\n"
             "PUBLIC_KEYS_HEX — clients would refuse this update. Not publishing.")
# Name the signer. During a rotation several keys are trusted at once, and which
# one actually signed is the thing you want to see go from old to new — not
# something to infer later from installs that stopped updating.
which = keys.index(signer)
print(f"    verified: signed by pinned key #{which} ({signer.hex()[:16]}…)")
print(f"    this build trusts {len(keys)} key(s); clients with it accept the download")
if which != 0:
    print("    NOTE: signed by a successor key, not the first. Copies that predate\n"
          "          it in PUBLIC_KEYS_HEX will refuse this release.")
PYCODE

ls -lh "$ZIP" | awk '{print "    " $9 " (" $5 ")"}'

if [ -n "$DRY_RUN" ]; then
    say "dry run — not uploading. Assets are in dist/"
    exit 0
fi

say "uploading to release $TAG"
gh release view "$TAG" >/dev/null 2>&1 || die "release $TAG does not exist — create it first"
gh release upload "$TAG" "$ZIP" "$SIG" --clobber

# Bump the Homebrew cask. A tap that still points at the previous release is
# worse than no tap: `brew install` succeeds and silently hands over an old
# build. Best-effort — a missing tap checkout must not fail a release that has
# already been uploaded.
CASK_TAP="${WAIKIKI_TAP:-$HOME/localdev/homebrew-waikiki}"
CASK="$CASK_TAP/Casks/waikiki.rb"
if [ -f "$CASK" ]; then
    say "bumping the Homebrew cask to $VERSION"
    SHA="$("$PY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$ZIP")"
    "$PY" - "$CASK" "$VERSION" "$SHA" <<'PYCODE'
import pathlib, re, sys
cask, version, sha = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = cask.read_text()
text = re.sub(r'version "[^"]+"', f'version "{version}"', text, count=1)
text = re.sub(r'sha256 "[^"]+"', f'sha256 "{sha}"', text, count=1)
cask.write_text(text)
print(f"    {cask.name}: version {version}, sha256 {sha[:16]}…")
PYCODE
    if git -C "$CASK_TAP" diff --quiet -- Casks/waikiki.rb; then
        say "cask already current"
    else
        git -C "$CASK_TAP" commit -q -am "Waikiki $VERSION" && \
        git -C "$CASK_TAP" push -q origin HEAD && \
        say "cask pushed"
    fi
else
    say "no cask checkout at $CASK_TAP — skipping the tap bump"
    echo "    (clone VerinFast/homebrew-waikiki there, or set WAIKIKI_TAP)" >&2
fi

say "done — $TAG now has a signed, installable asset"
