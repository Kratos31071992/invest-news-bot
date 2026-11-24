"""Простое хранилище подписок пользователей на SQLite (без зависимостей)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

# На Amvera постоянное хранилище примонтировано в /data — кладём базу туда,
# чтобы подписки и кэш переводов не стирались при перезапуске приложения.
_DATA_DIR = Path("/data") if Path("/data").is_dir() else Path(__file__).parent
DB_PATH = _DATA_DIR / "bot_data.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch (
    chat_id INTEGER NOT NULL,
    query   TEXT NOT NULL,
    UNIQUE(chat_id, query)
);
CREATE TABLE IF NOT EXISTS seen (
    link TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS translations (
    source_text TEXT NOT NULL,
    langpair    TEXT NOT NULL,
    translated  TEXT NOT NULL,
    PRIMARY KEY (source_text, langpair)
);
CREATE TABLE IF NOT EXISTS digest (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def add_watch(chat_id: int, query: str) -> bool:
    """True, если подписка новая; False, если уже существовала."""
    with _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO watch(chat_id, query) VALUES(?, ?)",
            (chat_id, query.strip().lower()),
        )
        return cur.rowcount > 0


def remove_watch(chat_id: int, query: str) -> bool:
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM watch WHERE chat_id = ? AND query = ?",
            (chat_id, query.strip().lower()),
        )
        return cur.rowcount > 0


def list_watch(chat_id: int) -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute(
            "SELECT query FROM watch WHERE chat_id = ? ORDER BY query", (chat_id,)
        )]


def all_watchers() -> dict[int, list[str]]:
    with _conn() as c:
        rows = c.execute("SELECT chat_id, query FROM watch").fetchall()
    out: dict[int, list[str]] = {}
    for chat_id, q in rows:
        out.setdefault(chat_id, []).append(q)
    return out


def is_seen(link: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE link = ?", (link,)).fetchone() is not None


def mark_seen(links: list[str]) -> None:
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO seen(link) VALUES(?)", [(l,) for l in links])
        # Храним не больше 5000 последних ссылок
        c.execute(
            "DELETE FROM seen WHERE link NOT IN "
            "(SELECT link FROM seen ORDER BY rowid DESC LIMIT 5000)"
        )


def get_translation(source_text: str, langpair: str) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT translated FROM translations WHERE source_text = ? AND langpair = ?",
            (source_text, langpair),
        ).fetchone()
    return row[0] if row else None


def set_translation(source_text: str, langpair: str, translated: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO translations(source_text, langpair, translated) VALUES(?, ?, ?)",
            (source_text, langpair, translated),
        )
        # Кэш не раздуваем: не больше 5000 записей
        c.execute(
            "DELETE FROM translations WHERE rowid NOT IN "
            "(SELECT rowid FROM translations ORDER BY rowid DESC LIMIT 5000)"
        )


def set_digest(chat_id: int, enabled: bool) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO digest(chat_id, enabled) VALUES(?, ?)",
            (chat_id, 1 if enabled else 0),
        )


def digest_enabled(chat_id: int) -> bool:
    with _conn() as c:
        row = c.execute("SELECT enabled FROM digest WHERE chat_id = ?", (chat_id,)).fetchone()
    return bool(row and row[0])


def digest_users() -> list[int]:
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT chat_id FROM digest WHERE enabled = 1")]
