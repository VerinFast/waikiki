"""Updater tests.

No network and no real bundle swap: the swap happens in a detached helper that
outlives the test process, so exercising it here would be swapping the app out
from under pytest. What is covered is everything that decides *whether* to swap
-- version comparison, signature verification, and the refusal paths -- because
that is the part whose failure mode is running attacker-supplied code.
"""
import subprocess

import pytest
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519

from waikiki import appconfig, updater


@pytest.fixture
def signing_key(monkeypatch):
    """A throwaway keypair, pinned as the build's update key."""
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    monkeypatch.setenv("WAIKIKI_UPDATE_PUBKEY", pub.hex())
    return priv


# --- Version comparison -------------------------------------------------------

@pytest.mark.parametrize("remote,local,expected", [
    ("v0.13.1", "0.13.0", True),
    ("0.14.0", "0.13.9", True),
    ("v1.0.0", "0.99.99", True),
    ("0.14", "0.13.0", True),           # short remote still compares
    ("0.13.0", "0.13.0", False),        # same version is not an update
    ("0.12.9", "0.13.0", False),        # older is not an update
    ("0.13", "0.13.0", False),          # 0.13 == 0.13.0, not newer
])
def test_version_comparison(remote, local, expected):
    assert updater.is_newer(remote, local) is expected


@pytest.mark.parametrize("junk", ["", "garbage", "v", "latest", "..."])
def test_unparseable_remote_version_is_never_an_update(junk):
    """A malformed tag must not read as newer, or a bad release triggers rollout."""
    assert updater.is_newer(junk, "0.13.0") is False


def test_string_comparison_would_be_wrong():
    """Guards the reason we parse instead of comparing strings: "0.9" > "0.10"."""
    assert updater.is_newer("0.10.0", "0.9.0") is True


# --- Signature verification ---------------------------------------------------

def test_valid_signature_passes(signing_key):
    payload = b"pretend release zip"
    assert updater.verify_signature(payload, signing_key.sign(payload)) is True


def test_tampered_payload_is_refused(signing_key):
    payload = b"pretend release zip"
    sig = signing_key.sign(payload)
    assert updater.verify_signature(payload + b"evil", sig) is False


def test_signature_from_another_key_is_refused(signing_key):
    """The whole point of pinning: someone else's valid signature is still invalid."""
    payload = b"pretend release zip"
    attacker = ed25519.Ed25519PrivateKey.generate()
    assert updater.verify_signature(payload, attacker.sign(payload)) is False


@pytest.mark.parametrize("sig", [b"", b"\x00" * 64, b"short"])
def test_malformed_signatures_are_refused(signing_key, sig):
    assert updater.verify_signature(b"payload", sig) is False


@pytest.mark.parametrize("key", ["", "nothex", "aa" * 31, "aa" * 33])
def test_missing_or_malformed_pinned_key_disables_verification(monkeypatch, key):
    """Fail closed: no usable key means nothing verifies, not everything does."""
    monkeypatch.setenv("WAIKIKI_UPDATE_PUBKEY", key)
    priv = ed25519.Ed25519PrivateKey.generate()
    assert updater.verify_signature(b"payload", priv.sign(b"payload")) is False


def test_signature_file_accepts_raw_or_hex(signing_key):
    payload = b"pretend release zip"
    sig = signing_key.sign(payload)
    assert updater.verify_signature(payload, updater._parse_signature(sig)) is True
    assert updater.verify_signature(
        payload, updater._parse_signature(sig.hex().encode())) is True


# --- Refusal paths ------------------------------------------------------------

def test_unpinned_build_cannot_update(monkeypatch):
    monkeypatch.setenv("WAIKIKI_UPDATE_PUBKEY", "")
    ok, why = updater.can_update()
    assert ok is False and "signing key" in why


def test_source_checkout_cannot_update(signing_key):
    """Running from a checkout has no bundle to swap; refuse rather than guess."""
    ok, why = updater.can_update()
    assert ok is False and ".app" in why


def test_download_and_apply_refuses_without_a_key(monkeypatch, wiki):
    """The orchestrator must bail before any network call when unpinned."""
    monkeypatch.setenv("WAIKIKI_UPDATE_PUBKEY", "")

    def explode(*a, **k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(updater, "check", explode)
    monkeypatch.setattr(updater, "_download", explode)
    res = updater.download_and_apply()
    assert res["ok"] is False and "signing key" in res["error"]


def test_apply_update_refuses_a_missing_staged_bundle(signing_key, wiki, monkeypatch):
    fake = updater._updates_dir() / "Nope.app"
    monkeypatch.setattr(updater, "can_update", lambda: (True, ""))
    monkeypatch.setattr(updater, "bundle_path",
                        lambda: updater._updates_dir() / "Waikiki.app")
    res = updater.apply_update(fake)
    assert res["ok"] is False and "staged bundle missing" in res["error"]


# --- Staging ------------------------------------------------------------------

def _make_bundle(root, name="Waikiki.app", version="0.13.0"):
    """A minimal but structurally real .app."""
    app = root / name
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "MacOS" / "Waikiki").write_text("#!/bin/bash\ntrue\n")
    import plistlib
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": "Waikiki",
        "CFBundleShortVersionString": version,
    }))
    return app


def _zip(app, dest):
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent",
                    str(app), str(dest)], check=True, capture_output=True)
    return dest


def test_stage_expands_and_accepts_a_matching_bundle(wiki, tmp_path):
    src = _make_bundle(tmp_path / "src", version="0.14.0")
    z = _zip(src, tmp_path / "Waikiki-0.14.0.zip")
    staged = updater.stage(z, expect_version="v0.14.0")
    assert staged.is_dir() and (staged / "Contents" / "MacOS" / "Waikiki").exists()


def test_stage_rejects_a_version_mismatch(wiki, tmp_path):
    """A zip whose Info.plist disagrees with the release tag is not installed."""
    src = _make_bundle(tmp_path / "src", version="0.9.0")
    z = _zip(src, tmp_path / "Waikiki-0.14.0.zip")
    with pytest.raises(RuntimeError, match="does not match release"):
        updater.stage(z, expect_version="0.14.0")


def test_stage_rejects_an_archive_with_no_app(wiki, tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "README.txt").write_text("not an app")
    z = _zip(junk, tmp_path / "junk.zip")
    with pytest.raises(RuntimeError, match="no .app bundle"):
        updater.stage(z)


# --- Scheduling ---------------------------------------------------------------

def test_auto_check_defaults_on_and_can_be_disabled(wiki):
    assert updater.auto_check_enabled() is True
    appconfig.set("update_auto_check", False)
    assert updater.auto_check_enabled() is False
    assert updater.due() is False           # disabled means never due


def test_due_respects_the_interval(wiki):
    import time
    assert updater.due() is True            # never checked
    appconfig.set("update_last_check", time.time())
    assert updater.due() is False
    appconfig.set("update_last_check", time.time() - 25 * 3600)
    assert updater.due() is True


def test_maybe_check_is_a_noop_when_not_due(wiki, monkeypatch):
    import time
    appconfig.set("update_last_check", time.time())
    monkeypatch.setattr(updater, "check",
                        lambda: (_ for _ in ()).throw(AssertionError("checked")))
    assert updater.maybe_check() is None


def test_status_reports_an_unpinned_build(wiki, monkeypatch):
    monkeypatch.setenv("WAIKIKI_UPDATE_PUBKEY", "")
    st = updater.status()
    assert st["signing_key_pinned"] is False and st["updatable"] is False
    assert st["current"]


# --- Streamed verification (signature covers the digest, not the bytes) --------

def test_verify_file_accepts_a_correctly_signed_file(signing_key, tmp_path):
    blob = tmp_path / "Waikiki-macos.zip"
    blob.write_bytes(b"x" * (3 << 20))          # spans multiple read chunks
    sig = signing_key.sign(updater.file_digest(blob))
    assert updater.verify_file(blob, sig) is True


def test_verify_file_refuses_a_modified_file(signing_key, tmp_path):
    blob = tmp_path / "Waikiki-macos.zip"
    blob.write_bytes(b"x" * 1024)
    sig = signing_key.sign(updater.file_digest(blob))
    blob.write_bytes(b"x" * 1023 + b"y")        # same length, one byte changed
    assert updater.verify_file(blob, sig) is False


def test_verify_file_refuses_a_signature_over_the_raw_bytes(signing_key, tmp_path):
    """Guards the release.sh <-> updater contract.

    If the signer ever goes back to signing archive bytes while the client
    verifies the digest, every release would be refused. Pin the mismatch.
    """
    blob = tmp_path / "Waikiki-macos.zip"
    blob.write_bytes(b"payload bytes")
    assert updater.verify_file(blob, signing_key.sign(blob.read_bytes())) is False


def test_file_digest_matches_hashlib(tmp_path):
    import hashlib
    blob = tmp_path / "f.bin"
    blob.write_bytes(b"abc" * 100000)
    assert updater.file_digest(blob) == hashlib.sha256(blob.read_bytes()).digest()


def test_verify_file_refuses_a_missing_file(signing_key, tmp_path):
    assert updater.verify_file(tmp_path / "nope.zip", b"\x00" * 64) is False


# --- The install offer --------------------------------------------------------

def _fake_release(tag, assets):
    return {"tag_name": tag, "body": "notes",
            "assets": [{"name": n, "browser_download_url": f"https://x/{n}"}
                       for n in assets]}


def _run_check(monkeypatch, tag, assets):
    monkeypatch.setattr(updater, "_get_json",
                        lambda *a, **k: _fake_release(tag, assets))
    return updater.check()


def test_a_newer_signed_release_is_offered(wiki, monkeypatch):
    res = _run_check(monkeypatch, "v99.0.0",
                     ["Waikiki-macos.zip", "Waikiki-macos.zip.sig"])
    assert res["available"] and res["installable"]
    assert updater.status()["last_available"] == "v99.0.0"


def test_a_newer_release_without_a_signature_is_not_offered(wiki, monkeypatch):
    """Seen, but not installable — Settings must not offer a doomed install."""
    res = _run_check(monkeypatch, "v99.0.0", ["Waikiki-macos.zip"])
    assert res["available"] is True and res["installable"] is False
    assert "no signed .zip asset" in res["error"]
    st = updater.status()
    assert st["last_seen"] == "v99.0.0"
    assert st["last_available"] is None


def test_an_older_release_is_not_offered(wiki, monkeypatch):
    """A local build newer than the latest release must not offer a downgrade."""
    res = _run_check(monkeypatch, "v0.0.1",
                     ["Waikiki-macos.zip", "Waikiki-macos.zip.sig"])
    assert res["available"] is False and res["installable"] is False
    st = updater.status()
    assert st["last_seen"] == "v0.0.1"          # we did see it...
    assert st["last_available"] is None          # ...but it is not an upgrade


def test_last_available_is_cleared_when_a_release_is_pulled(wiki, monkeypatch):
    """A stale offer must not survive a later check that finds nothing."""
    _run_check(monkeypatch, "v99.0.0",
               ["Waikiki-macos.zip", "Waikiki-macos.zip.sig"])
    assert updater.status()["last_available"] == "v99.0.0"
    _run_check(monkeypatch, "v0.0.1",
               ["Waikiki-macos.zip", "Waikiki-macos.zip.sig"])
    assert updater.status()["last_available"] is None


# --- Swap helper --------------------------------------------------------------

def test_the_backup_stays_on_the_bundles_own_volume(signing_key, wiki, tmp_path,
                                                    monkeypatch):
    """`mv` across filesystems fails with EXDEV, aborting a valid update.

    The backup must be a sibling of the bundle, not under DATA_DIR (which can be
    on a different volume when the app lives on an external drive).
    """
    target = tmp_path / "Applications" / "Waikiki.app"
    (target / "Contents" / "MacOS").mkdir(parents=True)
    staged = updater._updates_dir() / "Waikiki.app"
    (staged / "Contents" / "MacOS").mkdir(parents=True)

    monkeypatch.setattr(updater, "can_update", lambda: (True, ""))
    monkeypatch.setattr(updater, "bundle_path", lambda: target)
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: None)      # don't actually detach
    res = updater.apply_update(staged)
    assert res["ok"], res

    script = (updater._updates_dir() / "swap.sh").read_text()
    bak_line = next(ln for ln in script.splitlines() if ln.startswith("BAK="))
    assert str(target.parent) in bak_line, \
        f"backup is not beside the bundle: {bak_line}"
    assert str(updater._updates_dir()) not in bak_line, \
        f"backup lives under DATA_DIR and can cross filesystems: {bak_line}"


def test_an_empty_env_override_disables_a_pinned_build(monkeypatch):
    """Setting WAIKIKI_UPDATE_PUBKEY="" must disable updating, not fall back.

    Once a real key is pinned in the source, an override that resolved to the
    constant when empty would make it impossible to run a build with updates
    off — and would silently re-enable trust the operator meant to remove.
    """
    assert updater.PUBLIC_KEY_HEX, "this test is meaningless without a pinned key"
    monkeypatch.setenv("WAIKIKI_UPDATE_PUBKEY", "")
    assert updater._pinned_key() is None
    ok, why = updater.can_update()
    assert ok is False and "signing key" in why


def test_the_pinned_key_is_used_when_no_override_is_set(monkeypatch):
    monkeypatch.delenv("WAIKIKI_UPDATE_PUBKEY", raising=False)
    assert updater._pinned_key() == bytes.fromhex(updater.PUBLIC_KEY_HEX)
