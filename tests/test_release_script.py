"""Where `scripts/release.sh` gets the signing key, and what it does with it.

The private key that signs updates lives in Google Secret Manager as `waikikey`.
These tests run the script's `--check-key` mode, which fetches the key exactly as
a release does and proves it against the pinned public keys — without building
or uploading anything, so it is safe to run here.

`gcloud` is replaced by a stand-in on `PATH` that serves a throwaway key, and
`WAIKIKI_UPDATE_PUBKEY` pins that key's public half, so the success path is a
real Ed25519 match rather than a mocked one.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release.sh"


def _keypair() -> tuple[str, str]:
    """(PEM private key, hex public key) — a key no real install trusts."""
    priv = ed25519.Ed25519PrivateKey.generate()
    pem = priv.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                             ser.NoEncryption()).decode()
    pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return pem, pub.hex()


@pytest.fixture
def gcloud(tmp_path):
    """A stand-in `gcloud` that serves a secret and records how it was asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    secret, log = tmp_path / "secret.pem", tmp_path / "gcloud.log"
    fake = bin_dir / "gcloud"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_GCLOUD_LOG"\n'
        'if [ -n "$FAKE_GCLOUD_FAIL" ]; then echo "$FAKE_GCLOUD_FAIL" >&2; exit 1; fi\n'
        'cat "$FAKE_GCLOUD_SECRET"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    class Gcloud:
        def serve(self, pem: str) -> None:
            secret.write_text(pem)

        @property
        def calls(self) -> list[str]:
            return log.read_text().splitlines() if log.exists() else []

    g = Gcloud()
    g.bin, g.log, g.secret = bin_dir, log, secret
    return g


def _run(tmp_path, gcloud=None, path=None, **env):
    """Run `release.sh --check-key` in a controlled environment."""
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("WAIKIKI_", "FAKE_GCLOUD"))}
    base.update(PYTHON=sys.executable, TMPDIR=str(scratch))
    if gcloud is not None:
        base.update(PATH=f"{gcloud.bin}{os.pathsep}{base['PATH']}",
                    FAKE_GCLOUD_LOG=str(gcloud.log),
                    FAKE_GCLOUD_SECRET=str(gcloud.secret))
    if path is not None:
        base["PATH"] = path
    base.update(env)
    return subprocess.run(["bash", str(SCRIPT), "--check-key"], env=base,
                          capture_output=True, text=True, timeout=120)


# --- The default: Google Secret Manager --------------------------------------

def test_the_key_comes_from_secret_manager_by_default(tmp_path, gcloud):
    pem, pub = _keypair()
    gcloud.serve(pem)

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub)

    assert r.returncode == 0, r.stderr
    assert "Google Secret Manager: waikikey (project startupos-328814)" in r.stdout
    assert "matches pinned key #0" in r.stdout
    # The project is named explicitly, never left to gcloud's per-machine default.
    assert gcloud.calls == [
        "secrets versions access latest --secret=waikikey --project=startupos-328814"]


def test_the_secret_name_and_project_can_be_chosen(tmp_path, gcloud):
    pem, pub = _keypair()
    gcloud.serve(pem)

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub,
             WAIKIKI_UPDATE_SECRET="other-key", WAIKIKI_GCP_PROJECT="verinfast-prod")

    assert r.returncode == 0, r.stderr
    assert gcloud.calls == [
        "secrets versions access latest --secret=other-key --project=verinfast-prod"]
    assert "(project verinfast-prod)" in r.stdout


def test_a_key_no_client_trusts_is_refused_before_anything_is_built(tmp_path, gcloud):
    """The secret holding the wrong key is caught in seconds, not after a build."""
    pem, _ = _keypair()
    _, someone_elses = _keypair()
    gcloud.serve(pem)

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=someone_elses)

    assert r.returncode != 0
    assert "NOT one of the 1 key(s) pinned" in (r.stdout + r.stderr)


def test_a_key_that_is_not_ed25519_is_named_for_what_it_is(tmp_path, gcloud):
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048) \
        .private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                       ser.NoEncryption()).decode()
    gcloud.serve(rsa_pem)
    _, pub = _keypair()

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub)

    assert r.returncode != 0
    assert "not an Ed25519 key" in (r.stdout + r.stderr)


# --- When Secret Manager can't be reached ------------------------------------

def test_gclouds_own_explanation_reaches_the_person(tmp_path, gcloud):
    """Logged out, wrong project, no permission — gcloud says which; so do we."""
    gcloud.serve("unused")

    r = _run(tmp_path, gcloud,
             FAKE_GCLOUD_FAIL="ERROR: (gcloud.secrets.versions.access) PERMISSION_DENIED")

    assert r.returncode != 0
    assert "could not read 'waikikey'" in r.stderr
    assert "PERMISSION_DENIED" in r.stderr


def test_no_gcloud_says_how_to_get_it_and_how_to_do_without(tmp_path):
    r = _run(tmp_path, path="/usr/bin:/bin")       # nowhere gcloud could be

    assert r.returncode != 0
    assert "gcloud is not installed" in r.stderr
    assert "WAIKIKI_UPDATE_KEY" in r.stderr         # the other way to sign


def test_an_empty_secret_is_refused(tmp_path, gcloud):
    gcloud.serve("")
    _, pub = _keypair()

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub)

    assert r.returncode != 0
    assert "is empty" in r.stderr


# --- The explicit local file -------------------------------------------------

def test_an_explicit_key_file_wins_and_secret_manager_is_not_asked(tmp_path, gcloud):
    pem, pub = _keypair()
    key = tmp_path / "keys" / "update-key.pem"
    key.parent.mkdir()
    key.write_text(pem)

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub, WAIKIKI_UPDATE_KEY=str(key))

    assert r.returncode == 0, r.stderr
    assert f"the file {key}" in r.stdout
    assert gcloud.calls == []                       # never consulted


def test_a_key_file_inside_the_repo_is_still_refused(tmp_path, gcloud):
    """Where it could be committed by accident — the rule this script had already."""
    pem, pub = _keypair()
    inside = REPO / "build" / "test-update-key.pem"
    inside.parent.mkdir(exist_ok=True)
    inside.write_text(pem)
    try:
        r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub,
                 WAIKIKI_UPDATE_KEY=str(inside))
    finally:
        inside.unlink()

    assert r.returncode != 0
    assert "inside the repo" in r.stderr


def test_a_key_file_that_does_not_exist_says_so(tmp_path):
    r = _run(tmp_path, WAIKIKI_UPDATE_KEY=str(tmp_path / "nope.pem"))

    assert r.returncode != 0
    assert "does not exist" in r.stderr


# --- The key is never written down -------------------------------------------

def test_the_fetched_key_never_touches_disk(tmp_path, gcloud):
    """The point of keeping it in Secret Manager is that there is no copy lying around.

    The script makes exactly one temporary file — gcloud's stderr, so a failure
    can be explained — and it must be gone afterwards. Nothing anywhere in the
    temp dir or the working tree may hold the key.
    """
    pem, pub = _keypair()
    gcloud.serve(pem)
    body = pem.split("-----")[2].strip().splitlines()[0]    # a line of the key itself
    before = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                            capture_output=True, text=True).stdout

    r = _run(tmp_path, gcloud, WAIKIKI_UPDATE_PUBKEY=pub)

    assert r.returncode == 0, r.stderr
    leftovers = list((tmp_path / "tmp").iterdir())
    assert leftovers == [], f"temp files left behind: {leftovers}"
    after = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                           capture_output=True, text=True).stdout
    assert after == before, "the check wrote into the working tree"
    assert body not in r.stdout and body not in r.stderr, "the key was printed"
