"""Bonjour advertising. Kept hermetic — no real dns-sd process is spawned."""
from waikiki import bonjour


def test_available_is_boolean():
    assert isinstance(bonjour.available(), bool)


def test_start_is_a_noop_without_dns_sd(monkeypatch):
    monkeypatch.setattr(bonjour.shutil, "which", lambda _: None)
    assert bonjour.start(8787) is False
    assert bonjour.is_running() is False


def test_stop_is_safe_when_not_running():
    bonjour.stop()          # must not raise even with nothing to stop
    assert bonjour.is_running() is False


def test_start_is_idempotent(monkeypatch):
    """A second start() while advertising must not spawn a duplicate."""
    spawned = []

    class FakeProc:
        def poll(self):
            return None      # still alive

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(bonjour.shutil, "which", lambda _: "/usr/bin/dns-sd")
    monkeypatch.setattr(bonjour, "_reap_orphans", lambda name: None)
    monkeypatch.setattr(bonjour.subprocess, "Popen",
                        lambda *a, **k: (spawned.append(a), FakeProc())[1])
    try:
        assert bonjour.start(8787) is True
        assert bonjour.start(8787) is True      # already advertising
        assert len(spawned) == 1                # no duplicate advertiser
        assert bonjour.is_running() is True
    finally:
        bonjour.stop()
    assert bonjour.is_running() is False


def test_registration_argv_is_well_formed(monkeypatch):
    captured = {}

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(bonjour.shutil, "which", lambda _: "/usr/bin/dns-sd")
    monkeypatch.setattr(bonjour, "_reap_orphans", lambda name: None)
    monkeypatch.setattr(bonjour.subprocess, "Popen", fake_popen)
    try:
        bonjour.start(9999, name="TestWiki")
        assert captured["argv"][:5] == [
            "dns-sd", "-R", "TestWiki", "_http._tcp", "local."]
        assert "9999" in captured["argv"]
        # must stay in our process group so a group kill takes it down too
        assert not captured["kwargs"].get("start_new_session")
    finally:
        bonjour.stop()
