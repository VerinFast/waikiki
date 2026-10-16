#!/usr/bin/env bash
#
# Build, sign, and publish a Jamgraf release.
#
#   ./scripts/release.sh --genkey        # one-time: make the update signing key
#   ./scripts/release.sh --check-key     # can this machine sign? (no build)
#   ./scripts/release.sh v0.14.0         # build + sign + upload assets to a tag
#   ./scripts/release.sh v0.14.0 --dry-run   # everything except the upload
#
# The updater refuses any download that isn't signed by the key pinned in
# jamgraf/updater.py (PUBLIC_KEY_HEX), so a release without these assets is a
# release nobody can install. See docs/updates.md.
#
# Where the PRIVATE key comes from, in order:
#
#   1. JAMGRAF_UPDATE_KEY, if set — a PEM file you name explicitly. Refused if it
#      lives inside the working tree, where it could be committed by accident.
#   2. Otherwise Google Secret Manager: the latest version of the secret
#      `waikikey` in the project `startupos-328814`, read with the gcloud CLI.
#      JAMGRAF_UPDATE_SECRET and JAMGRAF_GCP_PROJECT override either.
#
# There is deliberately no silent third option. A key that turns up from
# somewhere nobody chose is how a release ends up signed by the wrong one —
# and the key fetched from Secret Manager is never written to disk: it is held
# in a variable and handed to the one process that signs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Each input takes the JAMGRAF_ name, then the old WAIKIKI_ one (the rename).
KEY_FILE="${JAMGRAF_UPDATE_KEY:-${WAIKIKI_UPDATE_KEY:-}}"   # explicit file, or empty
KEY_PATH="${KEY_FILE:-$HOME/.jamgraf/update-key.pem}"   # --genkey's output
# The secret keeps its name across the rename: it is an identifier in Secret
# Manager, and renaming means creating a new secret and moving the key into it.
SECRET_NAME="${JAMGRAF_UPDATE_SECRET:-${WAIKIKI_UPDATE_SECRET:-waikikey}}"
# Named here rather than left to gcloud's configured default, which is per
# machine and often unset: a release should not depend on how somebody's laptop
# happens to be set up.
GCP_PROJECT="${JAMGRAF_GCP_PROJECT:-${WAIKIKI_GCP_PROJECT:-startupos-328814}}"
PY="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

# Nothing that can hold the key is let in from the caller's environment. bash
# keeps the export attribute when a script reassigns a variable it inherited, so
# a SIGNING_KEY_PEM the caller happened to export would — once this script put
# the real key in it — carry that key into build_macos.sh and every process the
# build starts. Unset first, and every assignment below is local to this shell.
unset SIGNING_KEY_PEM SIGNING_KEY_SOURCE JAMGRAF_UPDATE_KEY_PEM WAIKIKI_UPDATE_KEY_PEM

# xtrace prints assignments WITH their values, so `bash -x release.sh` — the
# first thing anyone reaches for when a release misbehaves — would put the PEM
# in the terminal, and in a CI log if this ever runs there. Every step that
# touches the key runs with tracing off and puts it back afterwards; the braces
# and 2>/dev/null keep even the `set +x` itself out of the trace.
quietly() {
    local was="$-" rc=0
    { set +x; } 2>/dev/null
    "$@" || rc=$?
    case "$was" in *x*) set -x ;; esac
    return "$rc"
}

# Puts the signing key's PEM in SIGNING_KEY_PEM and says where it came from in
# SIGNING_KEY_SOURCE. Called before anything slow happens, so a key that cannot
# be had fails the release in seconds rather than after a two-minute build.
load_signing_key() { quietly _load_signing_key; }
_load_signing_key() {
    if [ -n "$KEY_FILE" ]; then
        [ -f "$KEY_FILE" ] || die "JAMGRAF_UPDATE_KEY names $KEY_FILE, which does not exist"
        case "$(cd "$(dirname "$KEY_FILE")" && pwd)" in
            "$REPO_ROOT"|"$REPO_ROOT"/*)
                die "signing key lives inside the repo ($KEY_FILE) — move it outside" ;;
        esac
        SIGNING_KEY_PEM="$(cat "$KEY_FILE")"
        SIGNING_KEY_SOURCE="the file $KEY_FILE (JAMGRAF_UPDATE_KEY)"
        return
    fi

    command -v gcloud >/dev/null 2>&1 || die "the signing key is read from Google \
Secret Manager ('$SECRET_NAME') and gcloud is not installed.
       Install it: https://cloud.google.com/sdk/docs/install — then
       gcloud auth login
       Or sign with a local key file instead: JAMGRAF_UPDATE_KEY=/path/to/key.pem"

    local args=(secrets versions access latest --secret="$SECRET_NAME")
    [ -n "$GCP_PROJECT" ] && args+=(--project="$GCP_PROJECT")
    # gcloud's stderr is kept, because it is the only useful explanation when
    # this fails (not logged in, wrong project, no permission, no such secret).
    # It never contains the secret; stdout does, and stdout goes nowhere but the
    # variable.
    # An explicit template, because bare `mktemp` on macOS ignores $TMPDIR and
    # writes under /var/folders — so the one temporary file this makes would
    # land somewhere neither the operator nor a test is looking.
    local err
    err="$(mktemp "${TMPDIR:-/tmp}/jamgraf-release.XXXXXX")"
    if ! SIGNING_KEY_PEM="$(gcloud "${args[@]}" 2>"$err")"; then
        local why
        why="$(cat "$err")"
        rm -f "$err"
        die "could not read '$SECRET_NAME' from Google Secret Manager${GCP_PROJECT:+ (project $GCP_PROJECT)}:
       $why"
    fi
    rm -f "$err"
    [ -n "$SIGNING_KEY_PEM" ] || die "'$SECRET_NAME' in Google Secret Manager is empty"
    SIGNING_KEY_SOURCE="Google Secret Manager: $SECRET_NAME${GCP_PROJECT:+ (project $GCP_PROJECT)}"
}

# Runs Python with the signing key in ITS environment only — `VAR=x cmd` exports
# to that one command, never to the rest of this script. The program reads it
# from JAMGRAF_UPDATE_KEY_PEM. An environment variable is readable by the same
# user's other processes while this runs, which is a far smaller exposure than
# the file on disk it replaces: it lasts as long as one signature takes.
with_signing_key() { quietly _with_signing_key "$@"; }
_with_signing_key() {
    JAMGRAF_UPDATE_KEY_PEM="$SIGNING_KEY_PEM" "$@"
}

# Proves the loaded key is one a client would accept: a PEM, Ed25519, and one of
# the public keys compiled into this checkout. Run before the build as well as by
# --check-key — fetching it early only saved the build when the key was MISSING;
# a malformed or untrusted one sailed through to the post-build verification,
# after build_macos.sh had already wiped dist/.
verify_signing_key() {
    with_signing_key "$PY" - <<'PYCODE'
import os, sys
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519

from jamgraf import updater

try:
    priv = ser.load_pem_private_key(
        os.environ.pop("JAMGRAF_UPDATE_KEY_PEM").encode(), password=None)
except ValueError as exc:
    sys.exit(f"    that is not a PEM private key: {exc}")
if not isinstance(priv, ed25519.Ed25519PrivateKey):
    sys.exit(f"    that is a {type(priv).__name__}, not an Ed25519 key — the "
             "updater only verifies Ed25519 signatures")
pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
keys = updater._pinned_keys()
print(f"    public half: {pub.hex()[:16]}…")
if pub not in keys:
    sys.exit(f"    NOT one of the {len(keys)} key(s) pinned in jamgraf/updater.py —\n"
             "    a release signed with it would be refused by every client.")
print(f"    matches pinned key #{keys.index(pub)} of {len(keys)} — this machine can sign a release")
PYCODE
}

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
print("\nPaste this into jamgraf/updater.py as PUBLIC_KEY_HEX:\n")
print(f'PUBLIC_KEY_HEX = "{pub.hex()}"\n')
PYCODE
    echo "Back up the private key somewhere safe. Losing it means you cannot"
    echo "ship an update that existing installs will accept."
    echo
    echo "Releases read it from Google Secret Manager, as the 'latest' version"
    echo "of '$SECRET_NAME'. To put it there — the FIRST key ever (no secret yet):"
    echo "  gcloud secrets create $SECRET_NAME --replication-policy=automatic \\"
    echo "      --data-file=\"$KEY_PATH\"${GCP_PROJECT:+ --project=$GCP_PROJECT}"
    echo
    echo "ROTATING (the secret already exists — 'create' would refuse, and releases"
    echo "would go on signing with the old key). First ship a build that pins the"
    echo "new public key above ALONGSIDE the old one, then add it as a new version:"
    echo "  gcloud secrets versions add $SECRET_NAME \\"
    echo "      --data-file=\"$KEY_PATH\"${GCP_PROJECT:+ --project=$GCP_PROJECT}"
    echo
    echo "Either way, then delete $KEY_PATH so the only copy that signs is the"
    echo "managed one, and confirm with: $(basename "$0") --check-key"
    exit 0
fi

# --- Verification uses the keys this checkout ships, and nothing else --------
# JAMGRAF_UPDATE_PUBKEY replaces the pinned public keys at runtime; it exists so
# tests and private builds of the APP can trust a key of their own. Here it would
# be a trapdoor: both checks below — "this key is one clients trust" before the
# build, and "this signature verifies" after it — would quietly measure against
# whatever the environment said, and approve a release that no installed copy
# will accept. Refused rather than ignored, because whoever set it meant
# something by it and should hear that it cannot apply here. Set-but-empty
# counts: to the updater, an empty value is a deliberate override too.
# BOTH spellings. The app reads the old WAIKIKI_ name as a fallback (config.env),
# so refusing only the new one would leave the old one as a working trapdoor.
if [ -n "${JAMGRAF_UPDATE_PUBKEY+set}" ] || [ -n "${WAIKIKI_UPDATE_PUBKEY+set}" ]; then
    die "JAMGRAF_UPDATE_PUBKEY (or the old WAIKIKI_UPDATE_PUBKEY) is set. It overrides the public keys compiled into
       jamgraf/updater.py, so verifying against it proves nothing about what
       installed copies will accept. Unset it to release or to --check-key."
fi

# --- Can this machine sign? ---------------------------------------------------
# Fetches the key exactly as a release would and proves it against the public
# keys pinned in this checkout — without building or uploading anything. Worth
# running before a release: the build takes minutes, and finding out afterwards
# that gcloud was logged out, or that the secret holds a key no client trusts,
# is minutes wasted at best.
if [ "${1:-}" = "--check-key" ]; then
    cd "$REPO_ROOT"
    load_signing_key
    say "signing key from $SIGNING_KEY_SOURCE"
    verify_signing_key
    exit 0
fi

# --- Release ------------------------------------------------------------------
TAG="${1:-}"
DRY_RUN=""
[ "${2:-}" = "--dry-run" ] && DRY_RUN="1"
[ -n "$TAG" ] || die "usage: $(basename "$0") <tag> [--dry-run] | --check-key | --genkey"

cd "$REPO_ROOT"
VERSION="${TAG#v}"

# The tag must match what the app reports, or the updater's staging check
# ("bundle version X does not match release Y") rejects our own build.
CODE_VERSION="$("$PY" -c 'import jamgraf; print(jamgraf.__version__)')"
[ "$CODE_VERSION" = "$VERSION" ] || \
    die "tag $TAG disagrees with jamgraf.__version__ ($CODE_VERSION)"

# Before the build, not after it — and checked, not merely fetched. A logged-out
# gcloud, a missing secret, a malformed key or one no client trusts should each
# cost seconds; build_macos.sh starts by wiping dist/, so finding out afterwards
# throws away the previous build as well as the time.
load_signing_key
say "signing key from $SIGNING_KEY_SOURCE"
verify_signing_key

# build_macos.sh owns the build: PyInstaller, the ad-hoc codesign, and the zip.
# Reuse it so a released bundle is byte-for-byte the thing `build_macos.sh`
# produces, rather than a second, subtly different build path.
ZIP="dist/Jamgraf-macos.zip"
SIG="$ZIP.sig"

say "building via scripts/build_macos.sh"
rm -f "$SIG"
bash "$REPO_ROOT/scripts/build_macos.sh"
[ -d dist/Jamgraf.app ] || die "build produced no dist/Jamgraf.app"
[ -f "$ZIP" ] || die "build produced no $ZIP"

say "signing"
# Signs the zip's streamed SHA-256, NOT the archive bytes — updater.verify_file
# checks exactly that, so the two must stay in lockstep. Signing the digest lets
# a ~100MB download be verified in constant memory.
with_signing_key "$PY" - "$ZIP" "$SIG" <<'PYCODE'
import os, pathlib, sys
from cryptography.hazmat.primitives import serialization as ser

from jamgraf import updater

zip_path, sig_path = (pathlib.Path(p) for p in sys.argv[1:3])
priv = ser.load_pem_private_key(
    os.environ.pop("JAMGRAF_UPDATE_KEY_PEM").encode(), password=None)
digest = updater.file_digest(zip_path)          # the value the client verifies
sig = priv.sign(digest)
sig_path.write_text(sig.hex() + "\n")
print(f"    sha256:    {digest.hex()}")
print(f"    signature: {sig_path} ({len(sig)} bytes, hex-encoded)")
PYCODE
unset SIGNING_KEY_PEM

# Verify against the key actually pinned in this build, not just the private key
# we signed with. Catches the case where updater.py still holds an old public key
# (or none at all) — which would ship a release every client refuses.
say "verifying against the keys pinned in this build"
"$PY" - "$ZIP" "$SIG" <<'PYCODE'
import pathlib, sys
from jamgraf import updater

zip_path, sig_path = (pathlib.Path(p) for p in sys.argv[1:3])
keys = updater._pinned_keys()
if not keys:
    sys.exit("PUBLIC_KEYS_HEX is empty in jamgraf/updater.py — this build cannot\n"
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
CASK_TAP="${JAMGRAF_TAP:-${WAIKIKI_TAP:-$HOME/localdev/homebrew-jamgraf}}"
CASK="$CASK_TAP/Casks/jamgraf.rb"
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
    if git -C "$CASK_TAP" diff --quiet -- Casks/jamgraf.rb; then
        say "cask already current"
    else
        git -C "$CASK_TAP" commit -q -am "Jamgraf $VERSION" && \
        git -C "$CASK_TAP" push -q origin HEAD && \
        say "cask pushed"
    fi
else
    say "no cask checkout at $CASK_TAP — skipping the tap bump"
    echo "    (clone VerinFast/homebrew-jamgraf there, or set JAMGRAF_TAP)" >&2
fi

say "done — $TAG now has a signed, installable asset"
