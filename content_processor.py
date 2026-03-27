"""Модуль обработки контента — конвертация Telegram-постов в HTML для Tilda."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic
from PIL import Image

from config import Config
from telegram_reader import TelegramPost

logger = logging.getLogger(__name__)

MAX_IMAGE_WIDTH = 1200
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


@dataclass
class ProcessedPost:
    """Пост, подготовленный к публикации на Tilda."""

    title: str
    description: str
    html_body: str
    image_path: str | None
    original_message_id: int
    original_date: datetime


class ContentProcessor:
    """Обрабатывает TelegramPost: entities → HTML, заголовок, изображение."""

    def __init__(self, config: Config) -> None:
        self._title_strategy = config.title_strategy
        self._title_max_words = config.title_max_words
        self._anthropic_api_key = config.anthropic_api_key

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def process(self, post: TelegramPost) -> ProcessedPost:
        """Конвертирует TelegramPost в ProcessedPost, готовый к публикации."""
        html_body = self._entities_to_html(post.text, post.entities)
        html_body = html_body.replace("\n", "<br>")

        # Постобработка: убираем лишние <br> вокруг ссылок
        html_body = re.sub(r"<br><a ", " <a ", html_body)
        html_body = re.sub(r"</a><br>", "</a> ", html_body)
        html_body = re.sub(r"(<br>){3,}", "<br><br>", html_body)

        title = self.extract_title(
            post.text, self._title_strategy, self._title_max_words
        )

        plain = re.sub(r"<[^>]+>", "", html_body).replace("<br>", " ")
        description = self._generate_description(post.text, plain)

        image_path: str | None = None
        if post.photo_file_id:
            # Путь к скачанному файлу (скачивание уже произошло в main.py)
            candidate = Path(f"/tmp/tg_images/{post.photo_file_id}.jpg")
            if candidate.exists():
                image_path = self.prepare_image(str(candidate))

        return ProcessedPost(
            title=title,
            description=description,
            html_body=html_body,
            image_path=image_path,
            original_message_id=post.message_id,
            original_date=post.date,
        )

    @staticmethod
    def extract_title(text: str, strategy: str, max_words: int = 10) -> str:
        """Извлекает заголовок из текста поста.

        Стратегии:
        - first_line: текст до первого переноса строки, не более 120 символов.
        - first_n_words: первые N слов из текста.
        """
        clean = text.strip()
        if not clean:
            return "Без заголовка"

        if strategy == "first_line":
            line = clean.split("\n", 1)[0].strip()
            return line[:120] if len(line) > 120 else line

        # first_n_words
        words = clean.split()[:max_words]
        title = " ".join(words)
        return title[:120] if len(title) > 120 else title

    @staticmethod
    def prepare_image(path: str) -> str:
        """Сжимает и ресайзит изображение при необходимости."""
        img_path = Path(path)
        img = Image.open(img_path)

        resized = False
        if img.width > MAX_IMAGE_WIDTH:
            ratio = MAX_IMAGE_WIDTH / img.width
            new_height = int(img.height * ratio)
            img = img.resize((MAX_IMAGE_WIDTH, new_height), Image.LANCZOS)
            resized = True

        output_path = img_path.parent / f"processed_{img_path.name}"

        if resized or img_path.stat().st_size > MAX_IMAGE_SIZE_BYTES:
            img.save(output_path, "JPEG", quality=85, optimize=True)
            logger.info("Изображение обработано: %s", output_path)
            return str(output_path)

        return path

    def _generate_description(self, original_text: str, plain_text: str) -> str:
        """Генерирует краткое описание поста через Claude API.

        Если ANTHROPIC_API_KEY не задан или API недоступен —
        fallback на механическую обрезку первых 240 символов.
        """
        if not self._anthropic_api_key:
            logger.debug("ANTHROPIC_API_KEY не задан, используется обрезка текста")
            return plain_text[:240].strip()

        try:
            client = anthropic.Anthropic(api_key=self._anthropic_api_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=200,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Напиши краткое описание этого поста для превью "
                            "в блоге. Максимум 250 символов. Только текст "
                            "описания, без кавычек и пояснений.\n\n"
                            f"{original_text}"
                        ),
                    }
                ],
            )
            description = message.content[0].text.strip()
            # Гарантируем лимит Tilda
            if len(description) > 250:
                description = description[:247] + "..."
            logger.info("Описание сгенерировано через Claude API")
            return description

        except Exception as exc:
            logger.warning(
                "Ошибка Claude API, fallback на обрезку: %s", exc
            )
            return plain_text[:240].strip()

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    def _entities_to_html(text: str, entities: list[dict]) -> str:
        """Конвертирует Telegram entities в HTML-разметку."""
        if not entities:
            return html.escape(text)

        # Telegram считает offset/length в UTF-16 code units
        encoded = text.encode("utf-16-le")

        # Собираем вставки тегов (позиция в байтах UTF-16)
        insertions: list[tuple[int, int, str]] = []  # (byte_pos, order, tag)

        TAG_MAP = {
            "bold": ("<b>", "</b>"),
            "italic": ("<i>", "</i>"),
            "code": ("<code>", "</code>"),
            "pre": ("<pre>", "</pre>"),
            "strikethrough": ("<s>", "</s>"),
            "underline": ("<u>", "</u>"),
        }

        for i, ent in enumerate(entities):
            etype = ent["type"]
            offset_bytes = ent["offset"] * 2  # UTF-16: 2 bytes per code unit
            length_bytes = ent["length"] * 2
            end_bytes = offset_bytes + length_bytes

            if etype in TAG_MAP:
                open_tag, close_tag = TAG_MAP[etype]
                insertions.append((offset_bytes, i, "open", open_tag))
                insertions.append((end_bytes, i, "close", close_tag))

            elif etype == "text_link":
                url = ent.get("url", "")
                insertions.append(
                    (offset_bytes, i, "open", f'<a href="{html.escape(url)}">')
                )
                insertions.append((end_bytes, i, "close", "</a>"))

            elif etype == "url":
                # Извлекаем сам URL из текста
                url_text = encoded[offset_bytes:end_bytes].decode("utf-16-le")
                insertions.append(
                    (offset_bytes, i, "open", f'<a href="{html.escape(url_text)}">')
                )
                insertions.append((end_bytes, i, "close", "</a>"))

        if not insertions:
            return html.escape(text)

        # Сортируем: по позиции, close перед open на одной позиции (для вложенности)
        def sort_key(item):
            pos, idx, kind, _tag = item
            # close=0, open=1 — close идут первыми на одной позиции
            # Для close — обратный порядок idx (LIFO)
            if kind == "close":
                return (pos, 0, -idx)
            return (pos, 1, idx)

        insertions.sort(key=sort_key)

        # Собираем результат
        result_parts: list[str] = []
        prev_pos = 0

        for pos, _idx, _kind, tag in insertions:
            # Текст между тегами — экранируем HTML
            chunk = encoded[prev_pos:pos].decode("utf-16-le")
            result_parts.append(html.escape(chunk))
            result_parts.append(tag)
            prev_pos = pos

        # Хвост текста
        chunk = encoded[prev_pos:].decode("utf-16-le")
        result_parts.append(html.escape(chunk))

        return "".join(result_parts)
