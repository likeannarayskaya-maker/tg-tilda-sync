"""Модуль работы с SQLite — дедупликация постов и хранение состояния."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent / "db"
DB_PATH = DB_DIR / "sync.db"


class Database:
    """SQLite-хранилище для отслеживания опубликованных постов."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        """Создаёт БД и таблицы если не существуют."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        """Инициализирует схему БД."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS published_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_message_id INTEGER UNIQUE NOT NULL,
                telegram_date TEXT NOT NULL,
                tilda_post_title TEXT,
                published_at TEXT NOT NULL,
                status TEXT DEFAULT 'success',
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def is_published(self, message_id: int) -> bool:
        """Проверяет, был ли пост уже успешно опубликован."""
        cursor = self._conn.execute(
            "SELECT 1 FROM published_posts "
            "WHERE telegram_message_id = ? AND status = 'success'",
            (message_id,),
        )
        return cursor.fetchone() is not None

    def save_result(
        self,
        message_id: int,
        date: datetime,
        title: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Сохраняет результат публикации поста."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO published_posts
               (telegram_message_id, telegram_date, tilda_post_title,
                published_at, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, date.isoformat(), title, now, status, error),
        )
        self._conn.commit()

    def get_offset(self) -> int:
        """Возвращает последний сохранённый offset для getUpdates."""
        cursor = self._conn.execute(
            "SELECT value FROM sync_state WHERE key = 'last_offset'"
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset: int) -> None:
        """Обновляет offset для getUpdates."""
        self._conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_offset', ?)",
            (str(offset),),
        )
        self._conn.commit()

    def close(self) -> None:
        """Закрывает соединение с БД."""
        self._conn.close()
