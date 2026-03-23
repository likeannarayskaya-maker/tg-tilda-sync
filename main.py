"""Точка входа — синхронизация постов из Telegram-канала в Tilda Feeds."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import Config
from content_processor import ContentProcessor
from database import Database
from notifier import Notifier
from telegram_reader import TelegramReader
from tilda_publisher import TildaPublisher

LOGS_DIR = Path(__file__).parent / "logs"


def setup_logging() -> None:
    """Настраивает логирование в файл и stdout."""
    LOGS_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        LOGS_DIR / "app.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


async def sync_once(
    config: Config,
    db: Database,
    reader: TelegramReader,
    processor: ContentProcessor,
    publisher: TildaPublisher,
    notifier: Notifier,
) -> None:
    """Одна итерация синхронизации: получение постов → обработка → публикация."""
    logger = logging.getLogger(__name__)

    offset = db.get_offset()
    posts = reader.get_new_posts(offset)

    if not posts:
        logger.info("Новых постов нет")
        return

    for raw_post in posts:
        # Фильтрация
        if config.filter_exclude_forwarded and raw_post.is_forwarded:
            logger.debug("Пропущен пересланный пост #%d", raw_post.message_id)
            continue
        if config.filter_min_length and len(raw_post.text) < config.filter_min_length:
            logger.debug(
                "Пропущен короткий пост #%d (%d символов)",
                raw_post.message_id, len(raw_post.text),
            )
            continue
        if (
            config.filter_required_hashtag
            and config.filter_required_hashtag not in raw_post.text
        ):
            logger.debug(
                "Пропущен пост #%d без хештега '%s'",
                raw_post.message_id, config.filter_required_hashtag,
            )
            continue

        # Дедупликация
        if db.is_published(raw_post.message_id):
            logger.debug("Пост #%d уже опубликован", raw_post.message_id)
            continue

        try:
            # Скачать фото если есть
            if raw_post.photo_file_id:
                reader.download_photo(raw_post.photo_file_id)

            # Обработать
            processed = processor.process(raw_post)

            # Опубликовать
            await publisher.publish(processed)

            # Записать успех
            db.save_result(
                raw_post.message_id, raw_post.date,
                processed.title, "success",
            )
            notifier.notify_success(processed.title, raw_post.message_id)

        except Exception as exc:
            db.save_result(
                raw_post.message_id, raw_post.date,
                "", "failed", str(exc),
            )
            notifier.notify_error(str(exc), raw_post.message_id)
            logger.error(
                "Ошибка публикации поста #%d: %s",
                raw_post.message_id, exc, exc_info=True,
            )

    # Обновить offset
    last_update_id = posts[-1].update_id
    db.set_offset(last_update_id + 1)
    logger.info("Offset обновлён: %d", last_update_id + 1)


async def main() -> None:
    """Точка входа: одноразовый запуск или бесконечный цикл (--loop)."""
    parser = argparse.ArgumentParser(description="TG → Tilda Feeds sync")
    parser.add_argument(
        "--loop", action="store_true",
        help="Запустить в режиме бесконечного цикла с паузой POLL_INTERVAL_SECONDS",
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger(__name__)

    try:
        config = Config()
    except Exception as exc:
        logging.critical("Ошибка конфигурации: %s", exc)
        sys.exit(1)

    db = Database()
    reader = TelegramReader(config.telegram_bot_token)
    processor = ContentProcessor(config)
    publisher = TildaPublisher(
        config.tilda_email,
        config.tilda_password,
        config.tilda_project_id,
        config.tilda_feed_name,
    )
    notifier = Notifier(config.telegram_bot_token, config.admin_chat_id)

    try:
        if args.loop:
            logger.info(
                "Запуск в режиме loop (интервал: %dс)",
                config.poll_interval_seconds,
            )
            while True:
                try:
                    await sync_once(config, db, reader, processor, publisher, notifier)
                except Exception as exc:
                    logger.error("Ошибка в цикле sync: %s", exc, exc_info=True)

                logger.info(
                    "Ожидание %d секунд до следующей итерации...",
                    config.poll_interval_seconds,
                )
                await asyncio.sleep(config.poll_interval_seconds)
        else:
            await sync_once(config, db, reader, processor, publisher, notifier)
    finally:
        await publisher.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
