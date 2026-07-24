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

import threading
from typing import Optional

from . import config

_local = threading.local()
VEC_AVAILABLE = False  # set True once sqlite-vec loads in this process

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
    """One connection per thread (FastAPI handlers run across a threadpool)."""
    conn = getattr(_local, "conn", None)
    # Reconnect if the configured DB path changed (e.g. between tests).
    if conn is not None and getattr(_local, "path", None) != str(config.DB_PATH):
        conn = None
    if conn is None:
        if _HAS_APSW:
            conn = _ApswConn(str(config.DB_PATH))
        else:
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        _load_sqlite_vec(conn)
        _local.conn = conn
        _local.path = str(config.DB_PATH)
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
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


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
