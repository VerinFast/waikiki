"""SQLite layer: schema, FTS5 (BM25), and the sqlite-vec vector table.

Everything lives in one .db file so the whole wiki — pages, images, full-text
index, and vectors — is portable as a single artifact.

Backend selection: many stock CPython builds ship a `sqlite3` compiled WITHOUT
loadable-extension support, which sqlite-vec needs. So we prefer **apsw** (its
wheels bundle a modern SQLite with extensions enabled) and fall back to the
stdlib module. A tiny shim gives apsw the small slice of the sqlite3 DB-API that
the rest of the app uses (dict rows, `.fetchone/.fetchall`, `.lastrowid`,
`.commit`, `.execute*`), so `store.py`/`rag.py` never know the difference.
"""
from __future__ import annotations

import contextvars
import threading
from typing import Optional

from . import config

_local = threading.local()
VEC_AVAILABLE = False  # set True once sqlite-vec loads in this process

# The wiki whose database the current request/task operates on. Set per-request
# by the web app's middleware (from the X-Waikiki-Wiki header or cookie) and set
# explicitly by the MCP server / background flusher. Isolation depends on this.
current_wiki: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "current_wiki", default=None
)
_schema_ready: set[str] = set()  # wikis whose schema has been ensured this process


def active_wiki() -> str:
    """Resolve the active wiki slug, falling back to the registry default."""
    from . import wikis

    w = current_wiki.get()
    if w and wikis.exists(w):
        return w
    d = wikis.default_slug()
    if d is None:
        wikis.ensure_initialized()
        d = wikis.default_slug()
    return d

# --- Backend detection --------------------------------------------------------
try:
    import apsw  # type: ignore
    _HAS_APSW = True
except Exception:  # pragma: no cover
    apsw = None  # type: ignore
    _HAS_APSW = False

import sqlite3  # stdlib fallback (always importable)


class _Result:
    """Eagerly-materialized dict rows over an apsw cursor, DB-API-ish."""

    def __init__(self, apsw_conn, sql: str, params):
        cur = apsw_conn.cursor()
        cur.execute(sql, tuple(params) if params else ())
        try:
            cols = [d[0] for d in cur.getdescription()]
            self._rows = [dict(zip(cols, row)) for row in cur]
        except Exception:
            self._rows = []  # non-SELECT: no result columns
        self.lastrowid = apsw_conn.last_insert_rowid()

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _ApswConn:
    """Minimal sqlite3-compatible wrapper around an apsw.Connection."""

    def __init__(self, path: str):
        self._c = apsw.Connection(path)
        self._c.cursor().execute("PRAGMA journal_mode=WAL")
        self._c.cursor().execute("PRAGMA foreign_keys=ON")
        self._c.cursor().execute("PRAGMA busy_timeout=5000")

    def execute(self, sql: str, params=()):
        return _Result(self._c, sql, params)

    def executemany(self, sql: str, seq):
        self._c.cursor().executemany(sql, [tuple(p) for p in seq])
        return self

    def executescript(self, script: str):
        self._c.cursor().execute(script)  # apsw runs multi-statement scripts
        return self

    def commit(self):  # apsw autocommits outside explicit transactions
        pass

    # sqlite3-style extension API, so sqlite_vec.load() works unchanged.
    def enable_load_extension(self, flag: bool):
        self._c.enableloadextension(flag)

    def load_extension(self, path: str):
        self._c.loadextension(path)


def _load_sqlite_vec(conn) -> bool:
    global VEC_AVAILABLE
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        VEC_AVAILABLE = True
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        VEC_AVAILABLE = False
        print(f"[waikiki] sqlite-vec unavailable, vector search disabled: {exc}")
        return False


def get_conn():
    """Connection for the active wiki, cached per (thread, wiki)."""
    from . import wikis

    wiki = active_wiki()
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    conn = conns.get(wiki)
    if conn is None:
        path = str(wikis.db_path(wiki))
        if _HAS_APSW:
            conn = _ApswConn(path)
        else:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        _load_sqlite_vec(conn)
        conns[wiki] = conn
    if wiki not in _schema_ready:
        _ensure_schema(conn)
        _schema_ready.add(wiki)
    return conn


# --- Schema -------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL,
    markdown   TEXT NOT NULL DEFAULT '',
    html       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT                       -- NULL = active; timestamp = in trash
);

-- Lightweight history: one row per save. Not CRDT, but gives undo/audit.
CREATE TABLE IF NOT EXISTS page_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    markdown   TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT 'human',   -- 'human' | 'ai' | custom
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,
    mimetype   TEXT NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Page-level full-text index for the search box (BM25 via FTS5).
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title, markdown,
    content='pages', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, markdown) VALUES (new.id, new.title, new.markdown);
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, markdown) VALUES ('delete', old.id, old.title, old.markdown);
END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, markdown) VALUES ('delete', old.id, old.title, old.markdown);
    INSERT INTO pages_fts(rowid, title, markdown) VALUES (new.id, new.title, new.markdown);
END;

-- Chunk-level tables power hybrid RAG (BM25 + vector).
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


def _ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    # Migration: add soft-delete column to pre-existing wiki DBs.
    try:
        conn.execute("ALTER TABLE pages ADD COLUMN deleted_at TEXT")
    except Exception:
        pass  # already present
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


def init_db() -> None:
    """Ensure the wiki registry exists and the active/default wiki is schema-ready."""
    from . import wikis

    wikis.ensure_initialized()
    get_conn()  # lazily runs _ensure_schema for the active wiki


def backup_db(src_path: str, dest_path: str) -> None:
    """Write a consistent single-file copy of a SQLite DB (safe while it's in use).
    Used for Save/Export and Open/Import of wikis."""
    if _HAS_APSW:
        src = apsw.Connection(src_path)
        dest = apsw.Connection(dest_path)
        try:
            with dest.backup("main", src, "main") as b:
                b.step()  # copy all pages
        finally:
            dest.close()
            src.close()
    else:  # pragma: no cover
        s = sqlite3.connect(src_path)
        d = sqlite3.connect(dest_path)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()


def is_wiki_db(path: str) -> bool:
    """True if `path` looks like a Waikiki wiki file (has our core tables)."""
    try:
        conn = apsw.Connection(path) if _HAS_APSW else sqlite3.connect(path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        return {"pages", "settings"} <= tables
    except Exception:
        return False


def ensure_vec_table(dim: int) -> None:
    """Create (or recreate on dimension change) the sqlite-vec vector table.

    vec0 tables fix their dimension at creation, so if the active embedder
    changes to one with a different dim we drop and rebuild — callers then
    re-embed. `chunk_id` links a vector row back to the chunks table.
    """
    if not VEC_AVAILABLE:
        return
    conn = get_conn()
    current = get_setting("vec_dim")
    if current is not None and int(current) != dim:
        conn.execute("DROP TABLE IF EXISTS vec_chunks")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
    )
    set_setting("vec_dim", str(dim))
    conn.commit()


# --- Settings helpers ---------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def all_settings() -> dict[str, str]:
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
