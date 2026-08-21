"""Wiki registry — the list of isolated wikis and which is the default.

Each wiki is a separate SQLite file (``data/wikis/<slug>.db``). Physical
separation is what guarantees the isolation the app promises: a page in one
wiki can never link to or surface a page in another, because every lookup,
search, and ``[[wiki link]]`` resolves against a single wiki's database.

The registry itself is a small JSON file (``data/wikis.json``) read fresh on
each call so the web app and the (separate-process) MCP server always agree.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

from . import config, db, render

_lock = threading.Lock()


def _registry_path() -> Path:
    return config.DATA_DIR / "wikis.json"


def _wikis_dir() -> Path:
    config.WIKIS_DIR.mkdir(parents=True, exist_ok=True)
    return config.WIKIS_DIR


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (name or "").strip().lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-") or "wiki"


def _load() -> dict:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"wikis": [], "default": None}


def _save(reg: dict) -> None:
    """Replace the registry atomically.

    A plain ``write_text`` truncates first, so a crash mid-write leaves a torn
    JSON file — and ``_load`` treats unparseable as "no wikis", which makes every
    wiki the user created themselves disappear from the app while its ``.db``
    sits untouched on disk. Write beside it and rename: on the same filesystem
    that swap is atomic, so a reader sees the old registry or the new one.
    """
    path = _registry_path()
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(reg, indent=2))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)   # never leave a half-written twin behind


def list_wikis() -> list[dict]:
    return _load()["wikis"]


def exists(slug: str) -> bool:
    return any(w["slug"] == slug for w in list_wikis())


def name_of(slug: str) -> str:
    for w in list_wikis():
        if w["slug"] == slug:
            return w["name"]
    return slug


def default_slug() -> str | None:
    reg = _load()
    if reg.get("default") and exists(reg["default"]):
        return reg["default"]
    wl = reg["wikis"]
    return wl[0]["slug"] if wl else None


def db_path(slug: str) -> Path:
    return _wikis_dir() / f"{slug}.db"


def create_wiki(name: str) -> str:
    """Register a new wiki; returns its unique slug. The DB file is created
    lazily on first access (db.get_conn ensures the schema)."""
    base = slugify(name)
    with _lock:
        reg = _load()
        existing = {w["slug"] for w in reg["wikis"]}
        slug, n = base, 2
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        reg["wikis"].append({"slug": slug, "name": name.strip() or slug})
        if not reg.get("default"):
            reg["default"] = slug
        _save(reg)
    return slug


def delete_wiki(slug: str) -> bool:
    with _lock:
        reg = _load()
        before = len(reg["wikis"])
        reg["wikis"] = [w for w in reg["wikis"] if w["slug"] != slug]
        if len(reg["wikis"]) == before:
            return False
        if reg.get("default") == slug:
            reg["default"] = reg["wikis"][0]["slug"] if reg["wikis"] else None
        _save(reg)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path(slug)) + suffix)
        if f.exists():
            f.unlink()
    return True


def disk_bytes(slug: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path(slug)) + suffix)
        if f.exists():
            total += f.stat().st_size
    return total


def stats(slug: str) -> dict:
    """Summary for a wiki: articles, internal links (resolved vs broken),
    trashed count, and size on disk."""
    token = db.current_wiki.set(slug)
    try:
        conn = db.get_conn()
        articles = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE deleted_at IS NULL").fetchone()["c"]
        trashed = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE deleted_at IS NOT NULL").fetchone()["c"]
        from . import store  # lazy: avoid import cycle at module load
        idx = store.link_index()  # resolves [[Title]] and [[slug]] alike
        active = conn.execute(
            "SELECT markdown FROM pages WHERE deleted_at IS NULL").fetchall()
        resolved = broken = 0
        for r in active:
            for key in render.extract_wikilinks(r["markdown"]):
                if key in idx:
                    resolved += 1
                else:
                    broken += 1
    finally:
        db.current_wiki.reset(token)
    return {
        "slug": slug,
        "articles": articles,
        "trashed": trashed,
        "links": resolved + broken,
        "links_resolved": resolved,
        "links_broken": broken,
        "bytes": disk_bytes(slug),
    }


def export_to(slug: str, dest_path: str) -> str:
    """Save a wiki to a .wiki file — a zip bundle of the SQLite DB, its media as
    files, and a manifest."""
    import json
    import tempfile
    import zipfile

    if not exists(slug):
        raise ValueError(f"no wiki '{slug}'")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    db.backup_db(str(db_path(slug)), tmp.name)  # consistent snapshot

    token = db.current_wiki.set(slug)
    try:
        media = db.get_conn().execute(
            "SELECT id, filename, data FROM images").fetchall()
    finally:
        db.current_wiki.reset(token)

    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp.name, "wiki.db")
        for row in media:
            z.writestr(f"media/{row['id']}-{row['filename']}", row["data"])
        z.writestr("manifest.json", json.dumps(
            {"format": "waikiki-zip-1", "name": name_of(slug), "slug": slug,
             "media": len(media)}, indent=2))
    Path(tmp.name).unlink(missing_ok=True)
    return dest_path


def _extract_db(src_path: str) -> str:
    """Return a path to the wiki.db — from a zip bundle or a raw .db file."""
    with open(src_path, "rb") as f:
        head = f.read(2)
    if head == b"PK":  # zip bundle
        import tempfile
        import zipfile

        with zipfile.ZipFile(src_path) as z:
            name = "wiki.db" if "wiki.db" in z.namelist() else next(
                (n for n in z.namelist() if n.endswith(".db")), None)
            if not name:
                raise ValueError("Zip bundle has no wiki.db")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
            tmp.write(z.read(name))
            tmp.close()
            return tmp.name
    return src_path  # assume raw SQLite


def import_from(src_path: str, name: str | None = None) -> str:
    """Open an external wiki file (zip bundle or raw .db): validate it, copy it
    into the managed wikis dir under a fresh slug, and register it."""
    display = (name or Path(src_path).stem or "Imported").strip()
    src_path = _extract_db(src_path)  # zip bundle -> temp wiki.db, or raw .db
    if not db.is_wiki_db(src_path):
        raise ValueError("Not a Waikiki wiki file (missing pages/settings tables)")
    base = slugify(display)
    with _lock:
        reg = _load()
        existing = {w["slug"] for w in reg["wikis"]}
        slug, n = base, 2
        while slug in existing:
            slug, n = f"{base}-{n}", n + 1
        db.backup_db(src_path, str(db_path(slug)))  # consistent copy into the app
        reg["wikis"].append({"slug": slug, "name": display})
        if not reg.get("default"):
            reg["default"] = slug
        _save(reg)
    return slug


def export_markdown(slug: str, dest_dir: str) -> int:
    """Write every page of a wiki to `dest_dir` as `<slug>.md` (round-trip to a
    repo's docs/). Returns the number of files written."""
    import os

    from . import store

    token = db.current_wiki.set(slug)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        n = 0
        for p in store.list_pages(include_children=True):
            page = store.get_page(p["slug"])
            with open(os.path.join(dest_dir, f"{p['slug']}.md"), "w") as f:
                f.write(page["markdown"])
            n += 1
    finally:
        db.current_wiki.reset(token)
    return n


def markdown_zip(slug: str) -> bytes:
    """All pages of a wiki as a zip of `<slug>.md` files (for download)."""
    import io
    import zipfile

    from . import store

    token = db.current_wiki.set(slug)
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in store.list_pages(include_children=True):
                page = store.get_page(p["slug"])
                z.writestr(f"{p['slug']}.md", page["markdown"])
        return buf.getvalue()
    finally:
        db.current_wiki.reset(token)


def ensure_help_wiki() -> None:
    """Register the built-in Help wiki if missing. Idempotent; safe from both the
    web app and the (separate-process) MCP server."""
    slug = config.HELP_WIKI
    with _lock:
        reg = _load()
        if any(w["slug"] == slug for w in reg["wikis"]):
            return
        reg["wikis"].append({"slug": slug, "name": "Help"})
        if not reg.get("default"):
            reg["default"] = slug
        _save(reg)


def ensure_initialized() -> None:
    """First-run setup: migrate the legacy single DB into a 'main' wiki and
    seed the named wikis. Idempotent."""
    if _load()["wikis"]:
        return
    with _lock:
        reg = _load()
        if reg["wikis"]:
            return
        wdir = _wikis_dir()
        wikis: list[dict] = []

        # Migrate the pre-multi-wiki database into "main", if present.
        legacy = config.DB_PATH
        if legacy.exists():
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(legacy) + suffix)
                if src.exists():
                    shutil.move(str(src), str(wdir / ("main.db" + suffix)))
            wikis.append({"slug": "main", "name": "Main"})
        else:
            wikis.append({"slug": "main", "name": "Main"})

        for name in config.SEED_WIKIS:
            slug = slugify(name)
            if not any(w["slug"] == slug for w in wikis):
                wikis.append({"slug": slug, "name": name})

        _save({"wikis": wikis, "default": "main"})
