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
