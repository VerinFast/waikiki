import datetime

from waikiki import appconfig, backups, db, store, wikis


def test_backup_snapshots_every_wiki(wiki):
    store.create_page("Keeper", "irreplaceable words")
    res = backups.run_backup()
    assert res["ok"] and "main" in res["wikis"]
    items = backups.list_backups()
    assert len(items) == 1 and items[0]["bytes"] > 0


def test_snapshot_is_a_usable_wiki_database(wiki):
    """A backup must be restorable, not just a file of the right size."""
    store.create_page("Keeper", "irreplaceable words")
    res = backups.run_backup()
    snap = backups._root() / res["name"] / "main.db"
    assert db.is_wiki_db(str(snap))
    # ...and the content is really in there
    slug = wikis.import_from(str(snap), name="Restored")
    tok = db.current_wiki.set(slug)
    try:
        assert "irreplaceable words" in store.get_page("keeper")["markdown"]
    finally:
        db.current_wiki.reset(tok)


def test_rotation_keeps_only_n(wiki):
    appconfig.set("backup_keep", 2)
    root = backups._root()
    for stamp in ("2026-01-01_0100", "2026-01-02_0100", "2026-01-03_0100"):
        (root / stamp).mkdir(parents=True, exist_ok=True)
        (root / stamp / "main.db").write_bytes(b"x")
    backups._rotate()
    names = [b["name"] for b in backups.list_backups()]
    assert names == ["2026-01-03_0100", "2026-01-02_0100"]   # newest kept


def test_due_respects_interval_and_toggle(wiki):
    appconfig.set("backup_enabled", True)
    assert backups.due() is True                    # nothing yet → due
    backups.run_backup()
    assert backups.due() is False                   # just ran
    appconfig.set("backup_interval_hours", 24)
    old = datetime.datetime.now() - datetime.timedelta(hours=48)
    root = backups._root()
    for d in root.glob("*"):
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    stamp = old.strftime("%Y-%m-%d_%H%M")
    (root / stamp).mkdir(parents=True)
    (root / stamp / "main.db").write_bytes(b"x")
    assert backups.due() is True                    # 48h old, 24h interval
    appconfig.set("backup_enabled", False)
    assert backups.due() is False and backups.maybe_run() is None


def test_failed_backup_leaves_no_partial_snapshot(wiki, monkeypatch):
    def boom(src, dest):
        raise RuntimeError("disk full")
    monkeypatch.setattr(backups.db, "backup_db", boom)
    res = backups.run_backup()
    assert res["ok"] is False
    # a half-written directory must not linger and look like a good backup
    assert backups.list_backups() == []
