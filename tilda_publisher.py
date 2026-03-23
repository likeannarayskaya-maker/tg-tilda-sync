"""Модуль публикации постов в «Потоки» на Tilda через Playwright."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from content_processor import ProcessedPost

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
STORAGE_STATE_FILE = Path(__file__).parent / "storage_state.json"


class TildaPublishError(Exception):
    """Ошибка при публикации поста на Tilda."""


class TildaPublisher:
    """Автоматизирует публикацию постов в Tilda Feeds через Playwright."""

    def __init__(
        self,
        email: str,
        password: str,
        project_id: str,
        feed_name: str,
    ) -> None:
        self._email = email
        self._password = password
        self._project_id = project_id
        self._feed_name = feed_name
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._playwright = None

        SCREENSHOTS_DIR.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    async def publish(self, post: ProcessedPost, attempt: int = 1) -> None:
        """Публикует пост в поток Tilda. Retry до 2 раз."""
        max_attempts = 2
        try:
            await self._ensure_browser()
            await self._ensure_auth()
            await self._navigate_to_feeds()
            await self._create_post(post)
            logger.info(
                "Пост '%s' успешно опубликован в Tilda", post.title
            )
        except Exception as exc:
            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
            await self._safe_screenshot(f"error_{ts}.png")

            if attempt < max_attempts:
                logger.warning(
                    "Ошибка публикации (попытка %d/%d): %s — повтор...",
                    attempt, max_attempts, exc,
                )
                # НЕ сбрасываем storage_state — капча при повторном логине
                # Просто создаём новый контекст с существующей сессией
                if self._context:
                    try:
                        await self._context.close()
                    except Exception:
                        pass
                self._context = await self._browser.new_context(
                    storage_state=str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None,
                    viewport={"width": 1280, "height": 800},
                    locale="ru-RU",
                )
                self._page = await self._context.new_page()
                await self.publish(post, attempt + 1)
            else:
                raise TildaPublishError(
                    f"Не удалось опубликовать после {max_attempts} попыток: {exc}"
                ) from exc

    async def close(self) -> None:
        """Закрывает браузер и освобождает ресурсы."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Браузер
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """Инициализирует Playwright и браузер если ещё не запущены."""
        if self._page:
            return

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

        # Пытаемся восстановить сессию
        storage = None
        if STORAGE_STATE_FILE.exists():
            try:
                storage = str(STORAGE_STATE_FILE)
            except Exception:
                pass

        self._context = await self._browser.new_context(
            storage_state=storage,
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        self._page = await self._context.new_page()

    async def _reset_context(self) -> None:
        """Сбрасывает контекст браузера (при ошибке авторизации)."""
        if STORAGE_STATE_FILE.exists():
            STORAGE_STATE_FILE.unlink()
            logger.info("storage_state.json удалён для переавторизации")
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        self._page = await self._context.new_page()

    # ------------------------------------------------------------------
    # Авторизация
    # ------------------------------------------------------------------

    async def _ensure_auth(self) -> None:
        """Проверяет авторизацию на Tilda, при необходимости логинится."""
        page = self._page
        assert page is not None

        # Проверяем текущую сессию
        await page.goto("https://tilda.ru/projects/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        current_url = page.url
        if "/projects" in current_url and "/login" not in current_url:
            logger.info("Сессия Tilda активна")
            return

        # Нужна авторизация
        logger.info("Сессия не активна, выполняю авторизацию...")
        await self._login()

    async def _login(self) -> None:
        """Выполняет полную авторизацию на Tilda."""
        page = self._page
        assert page is not None

        await page.goto("https://tilda.ru/login/", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)

        # Email
        email_input = await page.wait_for_selector(
            'input[name="email"], input[type="email"]', timeout=10000
        )
        assert email_input is not None
        await email_input.fill(self._email)
        await page.wait_for_timeout(500)

        # Кнопка «Далее» / «Войти»
        submit_btn = await page.query_selector(
            'button[type="submit"], input[type="submit"], '
            '[data-test="login-btn"], .t-submit'
        )
        if submit_btn:
            await submit_btn.click()
        else:
            await email_input.press("Enter")
        await page.wait_for_timeout(2000)

        # Password
        password_input = await page.wait_for_selector(
            'input[name="password"], input[type="password"]', timeout=10000
        )
        assert password_input is not None
        await password_input.fill(self._password)
        await page.wait_for_timeout(500)

        # Кнопка входа
        submit_btn = await page.query_selector(
            'button[type="submit"], input[type="submit"]'
        )
        if submit_btn:
            await submit_btn.click()
        else:
            await password_input.press("Enter")

        # Ждём редирект на dashboard (60с — на случай капчи вручную)
        try:
            await page.wait_for_url("**/projects/**", timeout=60000)
        except Exception:
            await self._safe_screenshot("login_failed.png")
            raise TildaPublishError(
                "Авторизация не удалась: не произошёл редирект на /projects/"
            )

        logger.info("Авторизация на tilda.ru прошла успешно")

        # Сохраняем сессию
        await self._context.storage_state(path=str(STORAGE_STATE_FILE))

        # Проверяем/устанавливаем cookies для feeds.tilda.ru
        await self._ensure_feeds_auth()

    async def _ensure_feeds_auth(self) -> None:
        """Проверяет авторизацию на feeds.tilda.ru."""
        page = self._page
        assert page is not None

        feeds_url = f"https://feeds.tilda.ru/?projectid={self._project_id}"
        await page.goto(feeds_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Если редирект на логин — пробуем ещё раз через основной домен
        if "/login" in page.url:
            logger.warning(
                "Cookies tilda.ru не работают на feeds.tilda.ru, "
                "пробую авторизацию на поддомене..."
            )
            # Переходим на feeds через основной сайт (cookies обычно шарятся)
            await page.goto("https://tilda.ru/projects/", wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            await page.goto(feeds_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            if "/login" in page.url:
                raise TildaPublishError(
                    "Не удалось авторизоваться на feeds.tilda.ru"
                )

        logger.info("Авторизация на feeds.tilda.ru подтверждена")
        await self._context.storage_state(path=str(STORAGE_STATE_FILE))

    # ------------------------------------------------------------------
    # Навигация по потокам
    # ------------------------------------------------------------------

    async def _navigate_to_feeds(self) -> None:
        """Переходит в нужный поток на Tilda Feeds."""
        page = self._page
        assert page is not None

        feeds_url = f"https://feeds.tilda.ru/?projectid={self._project_id}"
        await page.goto(feeds_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Ищем строку с нужным потоком
        feed_link = await page.wait_for_selector(
            f'text="{self._feed_name}"', timeout=15000
        )
        if not feed_link:
            await self._safe_screenshot("feeds_not_found.png")
            raise TildaPublishError(
                f"Поток '{self._feed_name}' не найден на странице"
            )

        await feed_link.click()
        await page.wait_for_timeout(2000)
        logger.info("Открыт поток: %s", self._feed_name)

    # ------------------------------------------------------------------
    # Создание поста
    # ------------------------------------------------------------------

    async def _create_post(self, post: ProcessedPost) -> None:
        """Создаёт новый пост в открытом потоке и заполняет редактор.

        Алгоритм:
        1. Кнопка «+ Добавить пост» → модальное окно
        2. Заполнить «НАЗВАНИЕ» → нажать «Добавить» → открывается редактор
        3. В редакторе: краткое описание, обложка, текст
        4. Нажать «Сохранить и закрыть»
        """
        page = self._page
        assert page is not None

        # === Шаг 1: кнопка «+ Добавить пост» (коралловая, справа вверху) ===
        add_post_btn = page.locator("td.td-button-ico__title", has_text="\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u043e\u0441\u0442")
        if await add_post_btn.count() == 0:
            # Fallback на мобильную кнопку
            add_post_btn = page.locator("#addpost-btn-mobile")
        await add_post_btn.first.click()
        await page.wait_for_timeout(2000)
        await self._safe_screenshot("step1_modal_opened.png")
        logger.info("Шаг 1: модальное окно открыто")

        # === Шаг 2: заполнить НАЗВАНИЕ и нажать «Добавить» ===
        title_input = page.locator('input[type="text"]').last
        await title_input.fill(post.title)
        await page.wait_for_timeout(500)

        # Кнопка «Добавить» — коралловая в модалке (DIV.btn_addpost)
        add_confirm = page.locator("div.btn_addpost")
        if await add_confirm.count() > 0:
            await add_confirm.first.click()
            logger.info("Кнопка 'Добавить' нажата (div.btn_addpost)")
        else:
            # Fallback: ищем по тексту, кликаем с force
            confirm_btns = page.get_by_text(
                "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c", exact=True
            )
            if await confirm_btns.count() > 0:
                await confirm_btns.last.click(force=True)
                logger.info("Кнопка 'Добавить' нажата (по тексту)")
            else:
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                logger.info("Использован Tab+Tab+Enter")

        await page.wait_for_timeout(3000)
        await self._safe_screenshot("step2_after_add.png")
        logger.info("Шаг 2: пост создан, редактор должен быть открыт")

        # === Шаг 3: заполнить поля редактора ===
        await self._fill_editor(post)

        # === Шаг 4: сохранить и закрыть ===
        await self._save_and_close(post)

    async def _fill_editor(self, post: ProcessedPost) -> None:
        """Заполняет поля в редакторе поста.

        Реальные селекторы Tilda Feeds:
        - textarea[name="descr"] (pe-textarea, hidden) — краткое описание
        - input.tu-hidden-input[type="file"] — загрузка обложки
        - div.ql-editor (Quill.js) — текст поста
        """
        page = self._page
        assert page is not None

        # --- Краткое описание ---
        # textarea[name="descr"] скрыт, заполняем через JS
        await page.evaluate(
            """(text) => {
                const ta = document.querySelector('textarea[name="descr"]');
                if (ta) {
                    ta.value = text;
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            post.description,
        )
        logger.info("Краткое описание заполнено через JS")

        # --- Обложка (input.tu-hidden-input[type="file"]) ---
        if post.image_path:
            # Первый tu-hidden-input — это обложка поста (ИЗОБРАЖЕНИЕ)
            file_inputs = await page.query_selector_all('input.tu-hidden-input[type="file"]')
            if file_inputs:
                await file_inputs[0].set_input_files(post.image_path)
                await page.wait_for_timeout(3000)
                logger.info("Обложка загружена: %s", post.image_path)
            else:
                logger.warning("input.tu-hidden-input не найден")

        await self._safe_screenshot("step3_after_fields.png")

        # --- Текст поста (Quill.js) ---
        # Используем ql-editor с плейсхолдером "Введите текст..." (правая панель)
        text_editor = page.locator(
            'div.ql-editor[data-placeholder*="\u0412\u0432\u0435\u0434\u0438\u0442\u0435"]'
        )
        if await text_editor.count() == 0:
            # Fallback: последний ql-editor на странице
            text_editor = page.locator("div.ql-editor").last

        await text_editor.click()
        await page.wait_for_timeout(300)

        # Вставляем HTML через innerHTML + событие
        await page.evaluate(
            """(html) => {
                const editor = document.querySelector(
                    'div.ql-editor[data-placeholder*="\\u0412\\u0432\\u0435\\u0434\\u0438\\u0442\\u0435"]'
                ) || document.querySelectorAll('div.ql-editor')[1];
                if (editor) {
                    editor.innerHTML = html;
                    editor.dispatchEvent(new Event('input', {bubbles: true}));
                }
            }""",
            post.html_body,
        )
        await page.wait_for_timeout(500)
        logger.info("Текст поста вставлен в Quill-редактор")

        await self._safe_screenshot("step3_after_text.png")

    async def _save_and_close(self, post: ProcessedPost) -> None:
        """Нажимает 'Сохранить и закрыть' в редакторе."""
        page = self._page
        assert page is not None

        # Кнопка «Сохранить и закрыть» — коралловая
        save_close = page.get_by_text(
            "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0438 \u0437\u0430\u043a\u0440\u044b\u0442\u044c"
        )
        if await save_close.count() > 0:
            await save_close.first.click()
            await page.wait_for_timeout(3000)
            logger.info("Нажата 'Сохранить и закрыть'")
        else:
            # Fallback: просто «Сохранить»
            save_btn = page.get_by_text(
                "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c", exact=True
            )
            if await save_btn.count() > 0:
                await save_btn.first.click()
                await page.wait_for_timeout(3000)
                logger.info("Нажата 'Сохранить'")
            else:
                await page.keyboard.press("Control+s")
                await page.wait_for_timeout(3000)
                logger.info("Сохранено через Ctrl+S")

        await self._safe_screenshot(f"published_{post.original_message_id}.png")

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    async def _safe_screenshot(self, filename: str) -> None:
        """Делает скриншот, игнорируя ошибки."""
        try:
            if self._page:
                path = SCREENSHOTS_DIR / filename
                await self._page.screenshot(path=str(path))
                logger.debug("Скриншот сохранён: %s", path)
        except Exception as exc:
            logger.debug("Не удалось сделать скриншот: %s", exc)
