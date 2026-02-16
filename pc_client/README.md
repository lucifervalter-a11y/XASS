# PC Client (Windows)

Эту папку можно копировать на любой ПК отдельно от сервера.

## Быстрый запуск (рекомендуется)

1. Установите Python 3.11+.
2. В Telegram-боте откройте `/agents` и нажмите `🔑 Код подключения`.
3. На ПК запустите `run_agent.bat`.
4. На первом запуске введите:
   - URL/IP сервера (например `http://1.2.3.4:8001`)
   - код привязки из бота
   - имя ПК (можно оставить по умолчанию)

После этого клиент сам получит персональный ключ и сохранит его в `config.json`.

## Запуск без диалога (параметрами)

```bat
run_agent.bat --server-url http://1.2.3.4:8001 --pair-code XXXX-YYYY
```

Дополнительно:

```bat
run_agent.bat --server-url http://1.2.3.4:8001 --pair-code XXXX-YYYY --source-name OFFICE-PC --interval-sec 20
```

## Старый режим (ручной ключ)

Если нужно, можно использовать общий `AGENT_API_KEY`:

```bat
run_agent.bat --server-url http://1.2.3.4:8001 --api-key YOUR_AGENT_API_KEY
```

## Автозагрузка

- Установить: `install_autostart.bat`
- Удалить: `uninstall_autostart.bat`

## Что отправляет агент

- CPU/RAM/DISK/NET/uptime
- now playing (если доступно в ОС)
- активное окно/приложение (на Windows)
- top-процессы (по умолчанию включено)
