"""Модуль уведомлений — отправка статусов в Telegram."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

TG_SEND = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    """Отправляет уведомления администратору через Telegram Bot API."""

    def __init__(self, bot_token: str, admin_chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = admin_chat_id

    def notify_success(self, post_title: str, message_id: int) -> None:
        """Уведомление об успешной публикации."""
        text = f'✅ Опубликован пост: "{post_title}" (TG #{message_id})'
        self._send(text)

    def notify_error(self, error_text: str, message_id: int | None = None) -> None:
        """Уведомление об ошибке."""
        suffix = f" (TG #{message_id})" if message_id else ""
        text = f"❌ Ошибка{suffix}: {error_text}"
        self._send(text)

    def notify_session_expired(self) -> None:
        """Уведомление об истечении сессии Tilda."""
        self._send("⚠️ Сессия Tilda истекла. Требуется переавторизация.")

    def _send(self, text: str) -> None:
        """Отправляет сообщение через Bot API."""
        if not self._chat_id:
            logger.warning("ADMIN_CHAT_ID не задан, уведомление пропущено")
            return

        try:
            resp = requests.post(
                TG_SEND.format(token=self._token),
                json={"chat_id": self._chat_id, "text": text},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Ошибка отправки уведомления: %s", resp.text)
        except Exception as exc:
            logger.warning("Не удалось отправить уведомление: %s", exc)
