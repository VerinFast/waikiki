"""Automatic local backups of every wiki.

A wiki is usually the only copy of something irreplaceable, and until now the
safety net was "you remembered to export". This takes a consistent snapshot of
each wiki's database on a schedule and rotates old ones out.

Snapshots use ``db.backup_db`` (SQLite's online backup API), so they are taken
safely while the app is running and mid-write — a plain file copy of a live WAL
database is not safe. Images live in the ``images`` table as BLOBs, so a database
snapshot is a complete copy of the wiki's content.

Layout:  <data>/backups/<YYYY-MM-DD_HHMM>/<wiki-slug>.db
A snapshot directory is written in full or not at all — a partial run is removed
rather than left to look like a good backup.
"""
from __future__ import annotations

import datetime
import shutil
import sys
import threading

from . import appconfig, config, db, wikis

_lock = threading.Lock()

DEFAULT_KEEP = 7
DEFAULT_INTERVAL_HOURS = 24


def _root():
    d = config.DATA_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def enabled() -> bool:
    val = appconfig.get("backup_enabled")
    return True if val is None else bool(val)      # on by default


def keep() -> int:
    try:
        return max(1, int(appconfig.get("backup_keep", DEFAULT_KEEP)))
    except Exception:
        return DEFAULT_KEEP


def interval_hours() -> int:
    try:
        return max(1, int(appconfig.get("backup_interval_hours",
                                        DEFAULT_INTERVAL_HOURS)))
    except Exception:
        return DEFAULT_INTERVAL_HOURS


def list_backups() -> list[dict]:
    """Newest first: {name, when, wikis, bytes}."""
    out = []
    for d in sorted(_root().glob("*"), reverse=True):
        if not d.is_dir():
            continue
        files = list(d.glob("*.db"))
        if not files:
            continue
        out.append({
            "name": d.name,
            "when": d.name.replace("_", " "),
            "wikis": sorted(f.stem for f in files),
            "bytes": sum(f.stat().st_size for f in files),
            "path": str(d),
        })
    return out


def last_backup_at() -> datetime.datetime | None:
    items = list_backups()
    if not items:
        return None
    try:
        return datetime.datetime.strptime(items[0]["name"], "%Y-%m-%d_%H%M")
    except Exception:
        return None


def restore_hint() -> dict:
    """The restore route as data: where snapshots live and what the newest is.

    Settings explains restoring in prose, which is fine for someone whose app
    still works. A wiki that cannot be opened needs to say it on the spot, so the
    page that reports the damage builds its instructions from this (#71).
    """
    items = list_backups()
    return {
        "dir": str(_root()),
        "enabled": enabled(),
        "count": len(items),
        "latest": items[0] if items else None,
    }


def due() -> bool:
    if not enabled():
        return False
    last = last_backup_at()
    if last is None:
        return True
    age = datetime.datetime.now() - last
    return age >= datetime.timedelta(hours=interval_hours())


def run_backup() -> dict:
    """Snapshot every wiki. Returns {ok, name, wikis, skipped, error?}. Never raises.

    A wiki whose file SQLite cannot read is **skipped**, not fatal: this used to
    abort the whole run and delete the directory, so one damaged file meant no
    snapshot at all — for any wiki — on exactly the day one was needed (#71).
    Nothing is written for the damaged wiki, because a file that cannot be read
    cannot be copied, and half a database that looks like a good backup is worse
    than an absent one.
    """
    with _lock:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        dest = _root() / stamp
        try:
            dest.mkdir(parents=True, exist_ok=True)
            done, skipped = [], []
            for w in wikis.list_wikis():
                slug = w["slug"]
                src = wikis.db_path(slug)
                if not src.exists():
                    continue                      # wiki never opened; nothing to save
                try:
                    db.backup_db(str(src), str(dest / f"{slug}.db"))
                except Exception as exc:
                    if db.unreadable_reason(exc) is None:
                        raise
                    (dest / f"{slug}.db").unlink(missing_ok=True)
                    skipped.append(slug)
                    print(f"[waikiki] backup skipped unreadable wiki '{slug}': {exc}",
                          file=sys.stderr)
                    continue
                done.append(slug)
            if not done:
                shutil.rmtree(dest, ignore_errors=True)
                return {"ok": False, "error": "no wiki databases to back up",
                        "skipped": skipped}
            _rotate()
            return {"ok": True, "name": stamp, "wikis": done, "skipped": skipped}
        except Exception as exc:
            # Never leave a half-written snapshot looking like a good one.
            shutil.rmtree(dest, ignore_errors=True)
            print(f"[waikiki] backup failed: {exc}", file=sys.stderr)
            return {"ok": False, "error": str(exc)}


def _rotate() -> int:
    dirs = [d for d in sorted(_root().glob("*"), reverse=True) if d.is_dir()]
    removed = 0
    for d in dirs[keep():]:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def maybe_run() -> dict | None:
    """Run a backup if one is due (called from the hourly maintenance loop)."""
    if not due():
        return None
    return run_backup()
