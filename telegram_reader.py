"""Модуль чтения постов из Telegram-канала через Bot API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_FILE = "https://api.telegram.org/file/bot{token}/{path}"

IMAGES_DIR = Path("/tmp/tg_images")


@dataclass
class TelegramPost:
    """Пост из Telegram-канала."""

    message_id: int
    update_id: int
    date: datetime
    text: str
    entities: list[dict] = field(default_factory=list)
    photo_file_id: str | None = None
    is_forwarded: bool = False


class TelegramReader:
    """Получает новые посты из Telegram-канала через Bot API."""

    def __init__(self, bot_token: str, max_retries: int = 3) -> None:
        self._token = bot_token
        self._max_retries = max_retries
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def get_new_posts(self, offset: int = 0) -> list[TelegramPost]:
        """Получает новые обновления из канала начиная с указанного offset."""
        data = self._call("getUpdates", {
            "offset": offset,
            "timeout": 10,
            "allowed_updates": ["channel_post"],
        })
        if not data or not data.get("ok"):
            logger.warning("getUpdates вернул ошибку: %s", data)
            return []

        posts: list[TelegramPost] = []
        for update in data.get("result", []):
            msg = update.get("channel_post")
            if not msg:
                continue
            post = self._parse_message(msg, update["update_id"])
            if post:
                posts.append(post)

        logger.info("Получено %d новых постов (offset=%d)", len(posts), offset)
        return posts

    def download_photo(self, file_id: str) -> str:
        """Скачивает фото по file_id и возвращает локальный путь."""
        file_info = self._call("getFile", {"file_id": file_id})
        if not file_info or not file_info.get("ok"):
            raise RuntimeError(f"Не удалось получить информацию о файле {file_id}")

        file_path = file_info["result"]["file_path"]
        url = TG_FILE.format(token=self._token, path=file_path)

        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        local_path = IMAGES_DIR / f"{file_id}.jpg"

        resp = self._request_with_retry("GET", url)
        local_path.write_bytes(resp.content)
        logger.info("Фото сохранено: %s", local_path)
        return str(local_path)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _call(self, method: str, params: dict | None = None) -> dict | None:
        """Вызывает метод Telegram Bot API."""
        url = TG_API.format(token=self._token, method=method)
        resp = self._request_with_retry("POST", url, json=params)
        return resp.json()

    def _request_with_retry(
        self, method: str, url: str, **kwargs
    ) -> requests.Response:
        """HTTP-запрос с retry и exponential backoff."""
        delays = [2, 4, 8]
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
                resp.raise_for_status()
                return resp
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    wait = delays[attempt]
                    logger.warning(
                        "Сетевая ошибка (попытка %d/%d), повтор через %dс: %s",
                        attempt + 1, self._max_retries, wait, exc,
                    )
                    time.sleep(wait)
        raise RuntimeError(
            f"Не удалось выполнить запрос после {self._max_retries} попыток"
        ) from last_exc

    @staticmethod
    def _parse_message(msg: dict, update_id: int) -> TelegramPost | None:
        """Парсит сообщение Telegram в TelegramPost."""
        text = msg.get("text") or msg.get("caption") or ""
        entities = msg.get("entities") or msg.get("caption_entities") or []

        photo_file_id: str | None = None
        photos = msg.get("photo")
        if photos:
            photo_file_id = photos[-1]["file_id"]

        is_forwarded = (
            "forward_from" in msg
            or "forward_from_chat" in msg
            or "forward_date" in msg
            or "forward_origin" in msg
        )

        return TelegramPost(
            message_id=msg["message_id"],
            update_id=update_id,
            date=datetime.fromtimestamp(msg["date"], tz=timezone.utc),
            text=text,
            entities=entities,
            photo_file_id=photo_file_id,
            is_forwarded=is_forwarded,
        )
