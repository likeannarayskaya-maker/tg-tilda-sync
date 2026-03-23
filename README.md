# tg-tilda-sync

Автоматическая публикация постов из Telegram-канала в раздел «Потоки» (Feeds) на Tilda. Скрипт периодически опрашивает Telegram Bot API, обрабатывает новые посты (конвертирует форматирование в HTML, сжимает изображения), публикует их через Playwright в панели управления Tilda и отправляет уведомления администратору.

## Требования

- **Python 3.11+**
- **VPS** с 2 GB RAM (Playwright + Chromium)
- **Тариф Tilda** с поддержкой «Потоков» (Feeds)
- **Telegram-бот**, добавленный администратором в канал

## Установка

```bash
# 1. Клонировать репозиторий
git clone https://github.com/your-user/tg-tilda-sync.git
cd tg-tilda-sync

# 2. Создать виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Установить браузер для Playwright
playwright install chromium

# 5. Скопировать и заполнить конфигурацию
cp .env.example .env
nano .env
```

## Настройка (.env)

### Telegram

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather). Бот должен быть добавлен администратором в канал. |
| `TELEGRAM_CHANNEL_ID` | ID канала (числовой, например `-1001234567890`). Можно узнать через [@userinfobot](https://t.me/userinfobot) или переслав сообщение из канала. |
| `ADMIN_CHAT_ID` | Chat ID администратора для уведомлений. Узнать: написать боту `/start`, затем вызвать `getUpdates`. |

### Tilda

| Переменная | Описание |
|---|---|
| `TILDA_EMAIL` | Email аккаунта Tilda |
| `TILDA_PASSWORD` | Пароль аккаунта Tilda |
| `TILDA_PROJECT_ID` | ID проекта. **Как найти:** откройте нужный проект в Tilda — URL будет вида `https://tilda.ru/projects/?projectid=XXXXXXX` — `XXXXXXX` это и есть ID. |
| `TILDA_FEED_NAME` | Название потока в точности как оно отображается в панели управления (например, `Мысли о бизнесе, жизни и стартапах.`) |

### Настройки поведения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `600` | Интервал опроса (используется systemd-таймером) |
| `PUBLISH_DELAY_MINUTES` | `0` | Задержка перед публикацией |
| `FILTER_MIN_LENGTH` | `0` | Минимальная длина текста поста (0 = без фильтра) |
| `FILTER_REQUIRED_HASHTAG` | _(пусто)_ | Публиковать только посты с этим хештегом (пусто = все) |
| `FILTER_EXCLUDE_FORWARDED` | `true` | Пропускать пересланные сообщения |
| `TITLE_STRATEGY` | `first_line` | Как формировать заголовок: `first_line` — первая строка, `first_n_words` — первые N слов |
| `TITLE_MAX_WORDS` | `10` | Количество слов для стратегии `first_n_words` |

## Первый запуск

```bash
source venv/bin/activate
python main.py
```

Проверьте логи в `logs/app.log` и скриншоты в `screenshots/`.

> **Рекомендация:** при первом запуске можно временно установить `headless=False` в файле `tilda_publisher.py` (строка `self._browser = await self._playwright.chromium.launch(headless=True)`) чтобы визуально наблюдать за действиями Playwright в браузере.

## Автозапуск (systemd)

```bash
# Создать пользователя
sudo useradd -r -s /bin/false tgsync

# Скопировать проект
sudo cp -r . /opt/tg-tilda-sync
sudo chown -R tgsync:tgsync /opt/tg-tilda-sync

# Установить юниты
sudo cp systemd/tg-tilda-sync.service /etc/systemd/system/
sudo cp systemd/tg-tilda-sync.timer /etc/systemd/system/

# Включить и запустить таймер
sudo systemctl daemon-reload
sudo systemctl enable tg-tilda-sync.timer
sudo systemctl start tg-tilda-sync.timer

# Проверить статус
systemctl status tg-tilda-sync.timer
systemctl list-timers | grep tg-tilda
```

## Устранение неполадок

### Сессия Tilda истекла

Скрипт автоматически переавторизуется. Если не удаётся — удалите `storage_state.json` и запустите повторно:

```bash
rm /opt/tg-tilda-sync/storage_state.json
```

Проверьте правильность `TILDA_EMAIL` и `TILDA_PASSWORD`. Если на аккаунте включена двухфакторная аутентификация — она не поддерживается.

### Бот не видит посты канала

1. Убедитесь, что бот добавлен **администратором** канала (Settings → Administrators).
2. Проверьте `TELEGRAM_CHANNEL_ID` — он должен начинаться с `-100`.
3. Опубликуйте тестовый пост и вызовите `getUpdates` вручную:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates?allowed_updates=[\"channel_post\"]"
```

### Ошибки Playwright

- **Browser not found** — запустите `playwright install chromium`.
- **Timeout** — Tilda может быть медленной, попробуйте увеличить таймауты в `tilda_publisher.py`.
- **На VPS нет GUI** — убедитесь, что используется `headless=True` (по умолчанию) и установлены зависимости:

```bash
# Ubuntu/Debian
sudo apt install -y libgbm1 libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libatspi2.0-0
```

### Изменение вёрстки панели Tilda

Если Tilda обновила интерфейс и селекторы перестали работать:

1. Запустите с `headless=False` и наблюдайте за ошибкой.
2. Посмотрите скриншоты в `screenshots/` — они делаются автоматически при ошибках.
3. Обновите CSS-селекторы в `tilda_publisher.py` в методах `_navigate_to_feeds()` и `_create_post()`.
