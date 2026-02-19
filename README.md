# XASS: Telegram Business Bot + Backend + Agent


- Telegram webhook + `/panel` (кнопки: статус, сервер, ПК, логи, настройки, экспорт).
- Heartbeat от агентов (`PC_AGENT`/`SERVER_AGENT`) с offline/recovered-уведомлениями.
- Логирование сообщений/правок/удалений (что реально доступно через Bot API updates).
- `SAVE_MODE` режимы (`SAVE_OFF`, `SAVE_BASIC`, `SAVE_FULL`, `SAVE_PRIVATE_ONLY`, `SAVE_GROUPS_ONLY`).
- Сохранение медиа в файловое хранилище при `SAVE_FULL`.
- Экспорт логов в CSV.
- Мониторинг CPU/RAM/DISK/NET/uptime + статусы systemd-сервисов.
- Edit alerts are sent as formatted cards (old/new/diff + message link) to notify chat.

## Структура

- `app/main.py` - FastAPI backend.
- `app/telegram_handler.py` - обработка webhook updates, `/panel`, callbacks.
- `app/services/message_logging.py` - логирование сообщений/правок/удалений и медиа.
- `app/services/heartbeat.py` - heartbeat и определение offline.
- `agent/agent.py` - кроссплатформенный агент (Windows/Linux).

## Быстрый старт (локально)

1. Установить Python 3.11+.
2. Создать `.env`:
   - `copy .env.example .env` (Windows) или `cp .env.example .env` (Linux).
3. Установить зависимости:
   - `python -m venv .venv`
   - Windows: `.venv\\Scripts\\activate`
   - Linux: `source .venv/bin/activate`
   - `pip install -r requirements.txt`
4. Запустить backend:
   - `uvicorn app.main:app --host 0.0.0.0 --port 8000`

## Настройка webhook

После публикации backend наружу (HTTPS), выполнить:

```bash
curl -X POST "https://YOUR_HOST/telegram/setup-webhook" \
  -H "X-Api-Key: <SETUP_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"public_base_url":"https://YOUR_HOST"}'
```

Webhook будет установлен на:
`https://YOUR_HOST/telegram/webhook/<TELEGRAM_WEBHOOK_PATH>`

## Запуск агента

### Linux/Windows

```bash
python agent/agent.py \
  --server-url http://127.0.0.1:8000 \
  --api-key <AGENT_API_KEY> \
  --source-name my-pc \
  --source-type PC_AGENT \
  --interval-sec 30
```

Опции:
- `--include-processes` - top процессов в payload.
- `--disable-now-playing` - отключить попытку собрать now playing.
- По умолчанию агент не использует системный proxy (`trust_env=False`), чтобы localhost не уходил в прокси.
- Если нужен proxy, добавьте `--trust-env-proxy`.

## Локальный запуск бота без внешнего URL (Polling)

Если хотите, чтобы бот работал полностью локально (без webhook/ngrok), включите polling:

В `.env`:

```env
USE_POLLING=true
```

Запуск backend:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

В этом режиме backend сам забирает апдейты у Telegram через `getUpdates`.
`/telegram/setup-webhook` не нужен.

## Команды бота

- `/panel` - главное меню.
- `/status` - heartbeat статус источников.
- `/server` - метрики сервера.
- `/pc` - статус ПК-агентов.
- `/agents` - панель управления агентами (список, удаление, инструкция добавления).
- `/pairpc` - создать одноразовый код привязки для нового ПК-агента.
- `/agentzip` - отправить в чат ZIP-архив `pc_client` + одноразовый pair-code (только owner).
- `/logs` - последние логи с предыдущей/текущей версией при правках.
- `/export` - CSV экспорт.
- `/media <chat_id> <message_id>` - отправить сохранённые медиа по сообщению.
- `/setnotify` - выставить текущий чат как канал уведомлений.
- `/seturl <url | off>` - сохранить URL сервера для автогенерации готовых команд (без плейсхолдеров).
- `/setiphoneshortcut <icloud_url | off>` - сохранить iCloud-ссылку на готовый iPhone Shortcut (кнопка импорта в боте).
- `/quiettime <ЧЧ:ММ-ЧЧ:ММ>` - задать диапазон тихих часов вручную.
- `/profile_panel` - панель редактирования контента профиля сайта (только owner).
- `/projects` - панель управления проектами (добавить/редактировать/удалить/featured/фон страницы, только owner).
- `/weatherloc <Название | Широта | Долгота | Timezone>` - задать локацию автопогоды через бота (только owner).
- `/weatherrefresh` - принудительно обновить погоду сейчас (только owner).
- `/nowsource <pc|iphone|vk>` - выбрать источник now listening (только owner; без аргумента показывает кнопки переключения).
- `/iphonehook` - показать endpoint и ключ для iPhone webhook (только owner).
- `/connect_iphone` (`/addiphone`) - автонастройка iPhone hook: бот генерирует ключ и присылает готовую команду.
- `/iphoneshortcut` (`/shortcut_iphone`) - отправить готовую заготовку для iOS Shortcuts с автоподставленными endpoint/ключом.
- `/connect_vk` (`/addvk`, `/vksetup`) - инструкция и OAuth-ссылка для VK.
- `/vkset <vk_user_id> <vk_access_token>` - сохранить VK-данные без перезапуска.
- `/vkclear` - очистить VK-данные.

## Важные ограничения (честно)

- Онлайн/оффлайн Telegram-аккаунта владельца напрямую через Bot API не гарантируется.
  В проекте используется безопасный `heartbeat`.
- Удалённые/исчезающие сообщения нельзя гарантированно восстановить.
  Логируется только то, что система реально успела получить.

## Debian + systemd

Шаблоны:
- `deploy/systemd/serverredus-backend.service`
- `deploy/systemd/serverredus-agent.service`

Подправьте пути (`/opt/serverredus`), пользователя и переменные, затем:

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now serverredus-backend
sudo systemctl enable --now serverredus-agent
```

## Бэкапы

Минимальный скрипт:
- `deploy/backup.sh`

Пример:

```bash
chmod +x deploy/backup.sh
./deploy/backup.sh /opt/serverredus /opt/serverredus-backups
```

## Что заполнить для запуска

- `BOT_TOKEN`
- `OWNER_USER_ID`
- `AUTHORIZED_USER_IDS` (через запятую)
- `ADMIN_USER_IDS` (через запятую, опционально)
- `NOTIFY_CHAT_ID`
- `MONITORED_SERVICES`
- `AGENT_API_KEY`
- `AGENT_PAIR_CODE_TTL_MINUTES`, `AGENT_PAIR_CODE_LENGTH`
- `TELEGRAM_WEBHOOK_PATH`, `TELEGRAM_SECRET_TOKEN`, `SETUP_API_KEY`
- `PROFILE_JSON_PATH`, `PROFILE_BACKUPS_DIR`, `PROFILE_AUDIT_LOG_PATH`
- `PROFILE_AVATARS_DIR`
- `PROFILE_PUBLIC_URL` (опционально, для кнопки/ссылки в предпросмотре)
- `NOW_PLAYING_SOURCE_DEFAULT` (`pc_agent` / `iphone` / `vk`)
- `IPHONE_NOW_PLAYING_API_KEY`, `IPHONE_NOW_PLAYING_STALE_MINUTES`
- `VK_USER_ID`, `VK_APP_ID`, `VK_ACCESS_TOKEN`, `VK_API_VERSION`, `VK_NOW_PLAYING_REFRESH_MINUTES`

## Troubleshooting

- `409 Conflict` in polling (`getUpdates`):
  - This means more than one bot process is consuming updates.
  - Stop all duplicate `uvicorn` / bot instances and keep only one polling backend.
- `403 Forbidden` on `sendMessage`:
  - Bot cannot write to target chat/user.
  - Open a direct chat with the bot and run `/setnotify` in the chat where bot must post alerts.
- `400 Bad Request` on `copyMessage` / `deleteMessage`:
  - Telegram permissions differ by chat type and Business mode.
  - In some chats message deletion/copy is restricted by Telegram and may fail.
- `400 Bad Request` on `answerCallbackQuery`:
  - Callback is stale or already consumed; this is non-critical and handled gracefully.
- Deleted messages:
  - Bot API does not guarantee delete events for every chat type.
  - For supported delete updates, backend now stores a delete tombstone even if original message was not logged yet.
- Security:
  - If bot token appeared in logs, regenerate token in BotFather and update `.env`.

## Режим "Не в сети"

Добавлены команды:
- `/away on` - включить режим "Не в сети"
- `/away off` - выключить режим
- `/away` - показать подсказку и текущий статус
- `/awaytext <текст>` - задать автоответ пользователям
- `/awayfor <минуты>` - включить режим "не в сети" на ограниченное время
- `/awaytime <ЧЧ:ММ-ЧЧ:ММ | off>` - включить/обновить расписание режима "не в сети"
- `/awayallow <list|clear|add ID|remove ID>` - белый список пользователей, которым можно писать в режиме "не в сети"

Как работает режим:
- Когда режим включён, входящее сообщение от неавторизованного пользователя:
  - получает автоответ;
  - копируется в чат уведомлений (карточка + копия оригинала);
  - удаляется из исходного чата (если Telegram разрешает удаление в этом чате).
- Можно добавить исключения через `/panel` -> `Настройки` -> `Кому можно писать`:
  - `➕ Добавить контактом`
  - `➖ Удалить контактом`
  Контакт добавляется по `user_id`, отправлять нужно контакт Telegram-пользователя.

## Отдельная папка для ПК

Для установки на отдельный компьютер добавлена папка:
- `pc_client`

Что внутри:
- `pc_client/run_agent.bat` — запуск агента (с автоустановкой venv и зависимостей).
- `pc_client/install_autostart.bat` — добавить агент в автозагрузку Windows.
- `pc_client/uninstall_autostart.bat` — убрать из автозагрузки.
- `pc_client/client_agent.py` — клиент с первым интерактивным запуском (IP/URL сервера, код привязки, имя ПК).

Сценарий:
1. Скопировать папку `pc_client` на нужный ПК.
2. Запустить `run_agent.bat`.
3. Ввести IP/URL сервера, код привязки из `/agents` и имя ПК (или Enter).
4. Сервер зарегистрирует ПК и при первом подключении отправит подсказку с командой переименования:
   - `/pcname <старое_имя> <новое_имя>`

## Away + активность ПК

В режиме `/away on` автоответ теперь может дополняться текущей активностью ПК (через два переноса строки):
- что слушает (now playing),
- либо активное окно/приложение (например браузер/ChatGPT, если доступно).

## iPhone + VK для now listening

Можно выбрать источник now listening:
- `pc_agent` — стандартно с ПК-агента.
- `iphone` — внешний webhook (например, из Shortcuts на iPhone).
- `vk` — авто-чтение статуса музыки из VK API.

Команды:
- `/nowsource <pc|iphone|vk>` — переключить источник.
- `/iphonehook` — показать endpoint + ключ для iPhone.
- `/iphoneshortcut` — отправить «почти готовую» установку Shortcut (endpoint, ключ, кнопка открытия Shortcuts).
- `/setiphoneshortcut https://www.icloud.com/shortcuts/...` — добавить кнопку `📥 Импортировать Shortcut`.
- В `/agents` есть быстрые кнопки переключения источника (`ПК / iPhone / VK`).
- В `/agents` есть кнопки `🍎 Подключить iPhone`, `🧩 Установить Shortcut` и `🟦 Подключить VK`.
- Быстрая автогенерация iPhone ключа: `/connect_iphone`.
- Быстрое подключение VK токена: `/vkset <vk_user_id> <vk_access_token>`.
- Перед этим один раз задайте URL: `/seturl https://ваш-домен` (или `http://IP:PORT`).

iPhone webhook endpoint:
- `POST /profile/now-playing/external`
- Header: `X-Api-Key: <IPHONE_NOW_PLAYING_API_KEY>`
- JSON: `{"text":"Artist - Title","source":"iphone"}`
  или `{"artist":"Artist","title":"Title","source":"iphone"}`

Пример curl:

```bash
curl -X POST "https://YOUR_HOST/profile/now-playing/external" \
  -H "X-Api-Key: <IPHONE_NOW_PLAYING_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"artist":"Artist","title":"Title","source":"iphone"}'
```

Для VK:
- рекомендуемый способ: `/connect_vk` и затем `/vkset <vk_user_id> <vk_access_token>`;
- можно старым способом через `.env`: `VK_USER_ID` + `VK_ACCESS_TOKEN`;
- выберите режим `/nowsource vk`.
- (опционально) для готовой OAuth-ссылки задайте `VK_APP_ID` в `.env`.

## Редактор профиля сайта

Для безопасного редактирования контента страницы используется только JSON-файл профиля:
- `PROFILE_JSON_PATH` (по умолчанию `./data/profile.json`)
- никаких произвольных операций с файлами бот не делает
- ключевые авто-поля:
  - `now_listening_auto_enabled`, `now_listening_updated_at`
  - `weather_auto_enabled`, `weather_location_name`, `weather_latitude`, `weather_longitude`, `weather_timezone`, `weather_refresh_minutes`, `weather_updated_at`

Что умеет `/profile_panel`:
- ✏️ редактирование `name/title/bio/username/telegram_url/avatar_url`
- 🔗 управление `links[]` (добавить/удалить/переименовать)
- 🧩 управление `stack[]`
- 📝 изменение `quote`
- 🎵 `now_listening_text`
- 🌤 `weather_text`
- 🎧 авто-синхронизация `now_listening_text` с последнего `PC_AGENT` heartbeat
- 🌦 авто-погода (Open-Meteo) по полям `weather_location_name/weather_latitude/weather_longitude/weather_timezone`
- ⏱ авто-обновление погоды раз в `weather_refresh_minutes` (по умолчанию 60 минут)
- 👁 предпросмотр текущего профиля
- ♻️ откат к последней резервной копии

После каждого сохранения:
- создается бэкап `PROFILE_BACKUPS_DIR/profile_YYYYMMDD_HHMMSS.json`
- записывается аудит в `PROFILE_AUDIT_LOG_PATH`

Аватары:
- сначала откройте `/profile_panel` -> `🖼 Аватары`, затем отправьте фото/image-файл в этот же чат
- случайные фото/скриншоты из других чатов теперь игнорируются
- новый файл автоматически станет текущим `avatar_url` в профиле
- листание аватаров доступно в `/profile_panel` -> `🖼 Аватары`

## Projects page

- New web page: `/projects.php` (also `/projects/`).
- Data source: `data/projects.json` (or `PROJECTS_JSON_PATH` env override).
- Background source: `data/site_config.json` key `projects_background` (or `SITE_CONFIG_JSON_PATH` env override).
- Profile page now has a direct button to open Projects.

## One-file installer (Linux)

Fast setup from a clean server in one command:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lucifervalter-a11y/XASS/main/bootstrap-install.sh)
```

What this does:
- downloads/updates repo to selected folder;
- runs `install.sh` interactive wizard automatically;
- asks for required values (BOT token, owner/admin IDs, keys, mode).

To auto-run project after install:
1. In wizard select mode `server`.
2. Confirm `Install systemd services now?` -> `y`.
3. Services will be enabled and started (`serverredus-backend`, optional `serverredus-agent`).

Manual fallback (if you already have project files):

```bash
chmod +x install.sh
./install.sh
```

## Agent Pairing (Auto Key for PC)

New flow for PC agent onboarding:

1. In bot open `/agents`:
   - press `🔑 Код подключения` (or command `/pairpc`) for pair-code only;
   - or press `📦 Скачать ZIP для ПК` (or command `/agentzip`) to get ready archive + pair-code.
2. On PC run:

```bat
run_agent.bat --server-url http://<SERVER_IP>:8001 --pair-code <PAIR_CODE>
```

You can also run `run_agent.bat` without args and enter URL + code in wizard.

What changed:
- Server endpoint `POST /agent/pair/claim` issues a per-device key.
- Heartbeat accepts both keys:
  - global `AGENT_API_KEY` (backward compatibility)
  - issued per-agent key.
- Agent key is bound to source name and is updated on `/pcname` rename.

New ENV options:
- `AGENT_PAIR_CODE_TTL_MINUTES` (default `15`)
- `AGENT_PAIR_CODE_LENGTH` (default `8`)
