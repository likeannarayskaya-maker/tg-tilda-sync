"""Точка входа — синхронизация постов из Telegram-канала в Tilda Feeds."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web

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


# ======================================================================
# Обработка одного поста (общая логика для polling и webhook)
# ======================================================================

async def process_post(
    raw_post,
    config: Config,
    db: Database,
    reader: TelegramReader,
    processor: ContentProcessor,
    publisher: TildaPublisher,
    notifier: Notifier,
) -> None:
    """Фильтрует, обрабатывает и публикует один пост."""
    logger = logging.getLogger(__name__)

    # Фильтрация
    if config.filter_exclude_forwarded and raw_post.is_forwarded:
        logger.debug("Пропущен пересланный пост #%d", raw_post.message_id)
        return
    if config.filter_min_length and len(raw_post.text) < config.filter_min_length:
        logger.debug(
            "Пропущен короткий пост #%d (%d символов)",
            raw_post.message_id, len(raw_post.text),
        )
        return
    if (
        config.filter_required_hashtag
        and config.filter_required_hashtag not in raw_post.text
    ):
        logger.debug(
            "Пропущен пост #%d без хештега '%s'",
            raw_post.message_id, config.filter_required_hashtag,
        )
        return

    # Дедупликация
    if db.is_published(raw_post.message_id):
        logger.debug("Пост #%d уже опубликован", raw_post.message_id)
        return

    try:
        if raw_post.photo_file_id:
            reader.download_photo(raw_post.photo_file_id)

        processed = processor.process(raw_post)
        await publisher.publish(processed)

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


# ======================================================================
# Polling (--loop и одноразовый запуск)
# ======================================================================

async def sync_once(
    config: Config,
    db: Database,
    reader: TelegramReader,
    processor: ContentProcessor,
    publisher: TildaPublisher,
    notifier: Notifier,
) -> None:
    """Одна итерация polling: получение постов → обработка → публикация."""
    logger = logging.getLogger(__name__)

    offset = db.get_offset()
    posts = reader.get_new_posts(offset)

    if not posts:
        logger.info("Новых постов нет")
        return

    for raw_post in posts:
        await process_post(
            raw_post, config, db, reader, processor, publisher, notifier,
        )

    last_update_id = posts[-1].update_id
    db.set_offset(last_update_id + 1)
    logger.info("Offset обновлён: %d", last_update_id + 1)


# ======================================================================
# Webhook-сервер (--webhook)
# ======================================================================

def create_webhook_app(
    config: Config,
    db: Database,
    reader: TelegramReader,
    processor: ContentProcessor,
    publisher: TildaPublisher,
    notifier: Notifier,
) -> web.Application:
    """Создаёт aiohttp-приложение с webhook-обработчиком."""
    logger = logging.getLogger(__name__)
    processing_ids: set[int] = set()

    async def handle_health(_request: web.Request) -> web.Response:
        """Health check для Railway."""
        return web.Response(text="ok")

    async def _process_post_background(raw_post) -> None:
        """Обёртка для фоновой обработки поста с защитой от дублей."""
        try:
            await process_post(
                raw_post, config, db, reader, processor, publisher, notifier,
            )
        finally:
            processing_ids.discard(raw_post.message_id)

    async def handle_webhook(request: web.Request) -> web.Response:
        """Обработчик входящих обновлений от Telegram."""
        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400, text="bad request")

        logger.info("Webhook update: update_id=%s", update.get("update_id"))

        raw_post = reader.parse_update(update)
        if raw_post:
            mid = raw_post.message_id
            if mid in processing_ids:
                logger.info("Пост #%d уже обрабатывается, пропуск", mid)
            elif db.is_published(mid):
                logger.debug("Пост #%d уже опубликован, пропуск", mid)
            else:
                processing_ids.add(mid)
                asyncio.create_task(_process_post_background(raw_post))

        # Сразу возвращаем 200 OK — обработка идёт в фоне
        return web.Response(text="ok")

    async def on_startup(_app: web.Application) -> None:
        """Регистрирует webhook при старте сервера."""
        webhook_path = "/webhook"
        full_url = config.webhook_url.rstrip("/") + webhook_path
        reader.set_webhook(full_url)
        logger.info("Webhook-сервер запущен, URL: %s", full_url)

    async def on_cleanup(_app: web.Application) -> None:
        """Закрывает ресурсы при остановке."""
        await publisher.close()
        db.close()

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_post("/webhook", handle_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


# ======================================================================
# Точка входа
# ======================================================================

async def main() -> None:
    """Точка входа: webhook (по умолчанию), --loop или одноразовый запуск."""
    parser = argparse.ArgumentParser(description="TG \u2192 Tilda Feeds sync")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--webhook", action="store_true", default=True,
        help="Запустить webhook-сервер (по умолчанию)",
    )
    mode.add_argument(
        "--loop", action="store_true",
        help="Режим polling с паузой POLL_INTERVAL_SECONDS",
    )
    mode.add_argument(
        "--once", action="store_true",
        help="Одноразовый запуск (для cron/systemd)",
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

    if args.once:
        # Одноразовый запуск
        try:
            await sync_once(config, db, reader, processor, publisher, notifier)
        finally:
            await publisher.close()
            db.close()

    elif args.loop:
        # Polling с бесконечным циклом
        logger.info(
            "Запуск в режиме loop (интервал: %dс)",
            config.poll_interval_seconds,
        )
        # Удаляем webhook если был — иначе getUpdates не работает
        reader.delete_webhook()
        try:
            while True:
                try:
                    await sync_once(
                        config, db, reader, processor, publisher, notifier,
                    )
                except Exception as exc:
                    logger.error("Ошибка в цикле sync: %s", exc, exc_info=True)

                logger.info(
                    "Ожидание %d секунд до следующей итерации...",
                    config.poll_interval_seconds,
                )
                await asyncio.sleep(config.poll_interval_seconds)
        finally:
            await publisher.close()
            db.close()

    else:
        # Webhook-сервер (по умолчанию)
        if not config.webhook_url:
            logger.critical(
                "WEBHOOK_URL не задан. Укажите его в .env или "
                "используйте --loop / --once"
            )
            sys.exit(1)

        port = int(os.environ.get("PORT", "8080"))
        app = create_webhook_app(
            config, db, reader, processor, publisher, notifier,
        )
        logger.info("Запуск webhook-сервера на порту %d", port)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        # Держим процесс живым
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
