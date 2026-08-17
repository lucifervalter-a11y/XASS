<p align="center">
  <img src="./docs/assets/xass-readme-hero.png" alt="XASS — personal control center" width="100%">
</p>

<h1 align="center">XASS</h1>

<p align="center">
  <strong>Личный центр управления для Telegram, Windows, сервера, сайта и iPhone.</strong><br>
  Один backend, несколько интерфейсов и никакой россыпи отдельных панелей.
</p>

<p align="center">
  <a href="https://redvps.site">Сайт</a> ·
  <a href="https://redvps.site/miniapp.php">Mini App</a> ·
  <a href="https://github.com/lucifervalter-a11y/XASS/releases/download/agent-latest/XASS-Setup.exe">Скачать для Windows</a> ·
  <a href="./docs/OPERATIONS.md">Документация</a>
</p>

<p align="center">
  <img alt="Server 0.11.0" src="https://img.shields.io/badge/server-0.11.0-3b82f6?style=flat-square">
  <img alt="Windows agent 0.8.1" src="https://img.shields.io/badge/Windows_agent-0.8.1-2563eb?style=flat-square&logo=windows11&logoColor=white">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11+-111827?style=flat-square&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-async-059669?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="Telegram Mini App" src="https://img.shields.io/badge/Telegram-Mini_App-229ED9?style=flat-square&logo=telegram&logoColor=white">
</p>

## Что такое XASS

XASS объединяет персонального Telegram Business‑бота, мониторинг компьютеров и сервера, удалённые команды, управление публичным профилем, музыку, погоду и устанавливаемое iPhone‑веб‑приложение.

Проект задуман как единая личная система: компьютер сообщает состояние через heartbeat, backend хранит и обрабатывает данные, Mini App управляет системой, а публичный сайт показывает только то, что разрешено владельцем.

## Интерфейсы

<table>
  <tr>
    <td colspan="2" align="center">
      <img src="./docs/assets/xass-desktop-overview.png" alt="XASS Desktop Agent для Windows" width="100%">
      <br><sub><b>XASS Desktop Agent</b> — состояние подключения, локальные метрики, события и обновления.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src="./docs/assets/xass-site-preview.png" alt="Публичный профиль XASS" width="100%">
      <br><sub><b>Публичный профиль</b> — проекты, цитаты, контакты и живая активность.</sub>
    </td>
    <td width="50%" align="center">
      <img src="./docs/assets/xass-miniapp-preview.png" alt="Telegram Mini App XASS" width="360">
      <br><sub><b>Telegram Mini App</b> — мобильная панель владельца.</sub>
    </td>
  </tr>
</table>

## Возможности

| Поверхность | Что умеет |
|---|---|
| Telegram | Business‑бот, полные переписки, история правок и удалений, уведомления и команды владельца |
| Mini App | Метрики и подробная статистика агентов, переписки, настройки, локальные архивы, блокировка ПК и управление сайтом |
| Windows | Нативное приложение, автозапуск, CPU/RAM/Disk, локальный архив сообщений и медиа, однофайловая привязка и автообновления |
| iPhone / PWA | Вход и подтверждение опасных действий через Face ID / Passkey, установка на экран «Домой» |
| Публичный сайт | Профиль, проекты, цитаты, аватары, контакты, погода и музыка с раздельными зонами интерфейса |
| Backend | FastAPI, heartbeat, PostgreSQL/SQLite, очередь команд, экспорт, резервные копии и контроль состояния сервисов |

### Управление компьютерами

- Подключение ПК одноразовым кодом или файлом `xass-connect.xass`.
- Отдельный API‑ключ для каждого устройства.
- Удалённые команды: обновить агент, перезапустить, заблокировать Windows.
- Одинаковое подтверждение команд в Telegram и standalone PWA; при настроенном Passkey блокировка и перезапуск требуют биометрию устройства.
- Подписанные манифесты обновлений и проверка SHA‑256 перед установкой.
- Локальный статус соединения, не зависящий от буферизации журнала процесса.

### Переписки и локальный архив

- Mini App показывает диалоги, удалённые сообщения, правки и доступные превью медиа; в боте есть `/chats`, `/chat`, `/deleted` и `/archive`.
- При `SAVE_FULL` сервер хранит индекс и Telegram `file_id`, но не сохраняет байты медиа на диск VPS.
- В Mini App можно выбрать один или несколько ПК‑агентов для архива. Каждый выбранный агент сохраняет SQLite‑индекс и файлы в выбранную в Windows‑приложении папку.
- Telegram сообщает об удалении только для Business‑переписок, доступных подключённому бизнес‑боту. Обычный Bot API не присылает произвольные удаления из чужих чатов.

### Музыка и активность

- Источники now playing: Windows‑агент, iPhone Shortcuts или VK.
- Поиск трека и карточки со ссылками на музыкальные сервисы.
- Фильтрация браузерных вкладок и приложений, которые не являются музыкой.
- Передача текущей активности в профиль и автоответы только при включённой функции.

### Сайт и контент

- Редактор профиля, проектов, ссылок, стека и цитат из Mini App и Telegram.
- Галерея аватаров с загрузкой изображений владельцем.
- Автопогода через Open‑Meteo и отдельный блок контактов без смешивания данных.
- Бэкап и аудит каждого изменения контента.

## Как всё связано

```mermaid
flowchart LR
    TG[Telegram Bot] --> API[FastAPI backend]
    MINI[Telegram Mini App] --> API
    PWA[iPhone PWA] --> API
    WIN[Windows Agent] -->|heartbeat + results| API
    SRV[Server Agent] -->|metrics| API
    API --> DB[(PostgreSQL / SQLite)]
    API --> INDEX[(Message index)]
    API -->|media stream, no VPS copy| WIN
    API --> SITE[Public profile + projects]
    API -->|commands| WIN
    API -->|notifications| TG
```

## Быстрый старт

### Windows‑клиент

Скачайте актуальный установщик: **[XASS‑Setup.exe](https://github.com/lucifervalter-a11y/XASS/releases/download/agent-latest/XASS-Setup.exe)**.

После установки откройте в Mini App раздел `Агенты → Подключить ПК`, скачайте файл подключения и импортируйте его в XASS. Адрес сервера и одноразовый ключ уже находятся внутри файла.

### Сервер одной командой

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lucifervalter-a11y/XASS/main/bootstrap-install.sh)
```

Мастер создаст конфигурацию, установит зависимости и предложит включить systemd‑сервисы. Для публичного Mini App и iPhone PWA нужен HTTPS‑домен.

### Локальная разработка

```bash
git clone https://github.com/lucifervalter-a11y/XASS.git
cd XASS
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

На Windows достаточно запустить `run_server.bat`: он проверит Python, создаст окружение и установит зависимости автоматически.

## Минимальная конфигурация

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | Токен Telegram‑бота из BotFather |
| `OWNER_USER_ID` | Telegram ID владельца |
| `DATABASE_URL` | PostgreSQL для production или SQLite для локального запуска |
| `AGENT_API_KEY` | Резервный общий ключ heartbeat; новые ПК получают персональные ключи |
| `PROFILE_PUBLIC_URL` | Публичный HTTPS‑адрес сайта и Mini App |
| `USE_POLLING` | `true` для локального запуска без webhook |
| `PWA_COOKIE_SECURE` | `true` на публичном HTTPS‑домене |

Полный шаблон находится в [`.env.example`](./.env.example), а подробная установка — в [`docs/OPERATIONS.md`](./docs/OPERATIONS.md).

## Сценарии подключения

<details>
<summary><b>Подключить новый Windows‑компьютер</b></summary>

1. Откройте XASS Mini App.
2. Перейдите в `Агенты → Подключить ПК`.
3. Скачайте `xass-connect.xass` или скопируйте одноразовый код.
4. Импортируйте файл в Windows‑приложение.
5. После первого heartbeat компьютер появится в списке агентов.

</details>

<details>
<summary><b>Добавить XASS на экран «Домой» iPhone</b></summary>

1. В Mini App откройте `Инструменты → iPhone и веб‑приложение`.
2. Укажите публичный HTTPS‑адрес XASS.
3. Создайте одноразовую ссылку и откройте её в Safari.
4. Нажмите `Поделиться → На экран Домой`.

Одноразовый токен хранится в базе только как SHA‑256‑хэш, действует ограниченное время и аннулируется после первого использования.

</details>

<details>
<summary><b>Подключить музыку с iPhone или VK</b></summary>

- iPhone: создайте ключ через `/connect_iphone` и импортируйте подготовленный Shortcut.
- VK: используйте `/connect_vk`, затем сохраните `VK_USER_ID` и access token.
- Источник переключается командой `/nowsource <pc|iphone|vk>` или кнопками Mini App.

</details>

## Безопасность

- Telegram Mini App проверяет подписанный `initData` и права владельца.
- PWA использует подписанную `HttpOnly`‑сессию и одноразовый код во фрагменте URL, который не попадает в HTTP‑логи.
- Pair‑коды для агентов короткоживущие; после обмена устройство получает собственный ключ.
- Пакеты агента подписываются HMAC и проверяются по SHA‑256.
- Секреты и рабочие конфиги исключены из пакетов обновлений и Git‑репозитория.
- Серверные перезапуски и обновления выполняются после отправки HTTP‑ответа, чтобы UI не зависал.

## Структура проекта

```text
app/                    FastAPI backend и Telegram‑логика
app/services/           heartbeat, агенты, контент, музыка, погода, обновления
pc_client/              Windows‑приложение, агент, установщик и updater
agent/                  кроссплатформенный server/PC agent
miniapp.php             Telegram Mini App и iPhone PWA
profile.php             публичный профиль
projects.php            страница проектов
deploy/                 systemd, установка, обновление и бэкапы
tests/                  unit и integration‑проверки
docs/                   изображения и operational reference
```

## Разработка и проверки

```bash
python -m unittest discover -s tests -v
python -m compileall -q app pc_client tests
```

Тесты покрывают привязку агентов, подписанные обновления, PWA‑авторизацию, одноразовые ссылки, музыку, миграцию старого клиента, сайт и быстрый перезапуск сервиса.

GitHub Actions автоматически:

- разворачивает `main` на production‑сервер;
- собирает Windows‑установщик;
- публикует стабильный `XASS‑Setup.exe` в GitHub Releases;
- доставляет installer и metadata на сервер обновлений.

## Документация

- [Полная установка, команды и troubleshooting](./docs/OPERATIONS.md)
- [Windows Agent](./pc_client/README.md)
- [Шаблон конфигурации](./.env.example)
- [Последняя стабильная версия Windows](https://github.com/lucifervalter-a11y/XASS/releases/tag/agent-latest)

---

<p align="center">
  <sub>XASS — когда личная инфраструктура ощущается как один продукт.</sub>
</p>
