"""Модуль конфигурации — загрузка и валидация переменных окружения."""

from pathlib import Path
from dotenv import load_dotenv
import os


class ConfigError(Exception):
    """Ошибка конфигурации: отсутствует обязательная переменная."""


class Config:
    """Загружает настройки из .env и предоставляет их как атрибуты."""

    _REQUIRED = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNEL_ID",
        "TILDA_EMAIL",
        "TILDA_PASSWORD",
        "TILDA_PROJECT_ID",
    ]

    def __init__(self, env_path: Path | None = None) -> None:
        """Загружает .env и валидирует обязательные переменные."""
        env_file = env_path or Path(__file__).parent / ".env"
        load_dotenv(env_file)
        self._validate()

        # Telegram
        self.telegram_bot_token: str = os.environ["TELEGRAM_BOT_TOKEN"]
        self.telegram_channel_id: str = os.environ["TELEGRAM_CHANNEL_ID"]
        self.admin_chat_id: str = os.environ.get("ADMIN_CHAT_ID", "")

        # Tilda
        self.tilda_email: str = os.environ["TILDA_EMAIL"]
        self.tilda_password: str = os.environ["TILDA_PASSWORD"]
        self.tilda_project_id: str = os.environ["TILDA_PROJECT_ID"]
        self.tilda_feed_name: str = os.environ.get(
            "TILDA_FEED_NAME", "Мысли о бизнесе, жизни и стартапах."
        )

        # Настройки
        self.poll_interval_seconds: int = int(
            os.environ.get("POLL_INTERVAL_SECONDS", "600")
        )
        self.publish_delay_minutes: int = int(
            os.environ.get("PUBLISH_DELAY_MINUTES", "0")
        )
        self.filter_min_length: int = int(
            os.environ.get("FILTER_MIN_LENGTH", "0")
        )
        self.filter_required_hashtag: str = os.environ.get(
            "FILTER_REQUIRED_HASHTAG", ""
        )
        self.filter_exclude_forwarded: bool = (
            os.environ.get("FILTER_EXCLUDE_FORWARDED", "true").lower() == "true"
        )

        # Anthropic API
        self.anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")

        # Webhook
        self.webhook_url: str = os.environ.get("WEBHOOK_URL", "")

        # Заголовок
        self.title_strategy: str = os.environ.get("TITLE_STRATEGY", "first_line")
        self.title_max_words: int = int(os.environ.get("TITLE_MAX_WORDS", "10"))

    def _validate(self) -> None:
        """Проверяет наличие всех обязательных переменных."""
        missing = [var for var in self._REQUIRED if not os.environ.get(var)]
        if missing:
            raise ConfigError(
                f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}. "
                f"Скопируйте .env.example в .env и заполните значения."
            )
