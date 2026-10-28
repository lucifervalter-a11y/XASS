# XASS AI: интеграция с Hermes 0.22.2

## Статус

В пользовательском интерфейсе продукт называется **XASS AI**. Hermes Agent —
внутренний внешний runtime, а не пользовательский бренд. Проверенная локальная
установка сообщает версию `0.22.2` и commit
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`.

End-to-end интеграция пока **не завершена**. В baseline `81601bb` есть экраны и
команды, но старые backend, Windows, iOS и Mini App пути использовали
предполагаемые REST эндпоинты или демонстрационные ответы. Удаление этих ответов
делает ошибку честной, но само по себе не добавляет рабочий транспорт.

В текущем native working tree уже есть точный loopback WebSocket adapter и
честное отключённое состояние. Его статус — **partial / source-tested**: десять
source-contract checks прошли, но C# не собран и реальный runtime не запускался.
До подключения реального транспорта UI обязан показывать недоступность, а не
`online`, `ok: true`, фиктивные устройства, метрики, навыки или результаты команд.

## Фактический runtime-контракт

- `GET /api/health` возвращает `ok`, `version`, `displayVersion`, `commit` и
  `auth_required`.
- Диалог работает через WebSocket JSON-RPC на `/api/ws`, а не через
  `/v1/chat/completions`, `/api/chat`, `/chat` или другие угадываемые REST пути.
- Loopback WebSocket требует `?token=...`, совпадающий с токеном процесса.
- `session.create` создаёт сессию.
- `prompt.submit` получает `{session_id, text}`.
- `message.delta` передаёт поток результата; только `message.complete` завершает
  запрос успешно.
- Обычный standalone порт — 9119. Для desktop-owned процесса безопаснее порт
  `0` с обнаружением фактического адреса через `HERMES_DESKTOP_READY_FILE`.

401, 404, отсутствие токена/ready-файла, несовместимая версия протокола, разрыв
WebSocket или отсутствие terminal event означают ошибку. Они не являются
признаком `online` и не разрешают переход к demo success.

## Целевой адаптер XASS AI

1. XASS запускает или обнаруживает только локальный доверенный runtime, не создавая
   новые credentials и не меняя сетевые или security-настройки.
2. Endpoint и одноразовый/process token берутся из подтверждённого ready-файла;
   токен не логируется, не отправляется на backend/Telegram и не сохраняется в UI.
3. После успешного `/api/health` адаптер открывает token-authenticated `/api/ws`.
4. UI создаёт или выбирает сессию, отправляет prompt и показывает только реальные
   delta/complete события.
5. Tool execution остаётся в существующих XASS allowlist/confirmation границах.
   Ответ модели сам по себе не является разрешением на команду ПК.
6. При любой ошибке адаптер закрывает текущую операцию честным статусом
   `unavailable`/`error`; синтетический ответ запрещён.

Платный API fallback запрещён. Telegram-бот не переносится. Телефонный AI
transport пока не согласован и не реализован.

## Текущие поверхности

| Поверхность | Честный статус |
|---|---|
| Native Windows | Main Navigation page и точный loopback WebSocket adapter реализованы на уровне source. Send отключён, пока owned authenticated runtime не привязан. C# build/live exchange не проверены. |
| Python/Tk Windows | Угадываемые REST пути, ложный online и переход во внешний Web UI удалены. Клиент проверяет exact health, но честно блокирует prompt без owned token transport. |
| Backend/Telegram | Exact health работает fail-closed; status отдельно сообщает `interactive_available: false`. Все mutating AI routes требуют owner auth и одноразовый action proof. Публичные bot-команды называются `/ai*`; старые `/hermes*` принимаются только для совместимости. Telegram AI дополнительно ограничен `owner_user_id` и не принимает первую глобальную runtime-сессию: используются только сессии, созданные этой owner-only поверхностью. |
| Mini App | Экран не загружает сессии и не включает prompt/quick actions без явного `interactive_available: true`; demo response и фиктивный статус удалены. |
| iOS | `HermesService` различает проверенный health и интерактивный transport. Без явного `interactive_available: true` экран, создание сессий и отправка сообщений остаются заблокированы. Прямого phone-to-PC transport нет. |

Ни одна из этих поверхностей не должна публиковать chain-of-thought. Пользователю
можно показывать краткий проверяемый результат, статус tool call и диагностическую
ошибку без скрытых рассуждений модели.

Native adapter проверяет exact health `ok: true`, затем выполняет
`session.create` → `prompt.submit`, принимает только delta/complete своей сессии и
буферизует UI-обновления по 75 мс. Runtime binding пока получает port из
ready-файла и private in-memory token; launch/discovery/PID attach отсутствуют.
Завершать разрешено только `Process`, который запустил и которым владеет XASS.

## Acceptance gates

- Реальный 0.22.2 runtime запускается на ephemeral loopback port и выдаёт ready-файл.
- Health version/commit проверены, token не появляется в логах или process output.
- `session.create` → `prompt.submit` → delta → complete работает в Native Windows.
- 401/404/disconnect/timeout/malformed event дают honest error без cached/demo success.
- Повторный prompt, отмена, закрытие окна и завершение runtime не подвешивают UI.
- Tool call проходит существующее XASS подтверждение и не исполняется только по
  тексту модели.
- Отдельный design/authorization review определяет, нужен ли телефонный transport;
  до этого iOS/Telegram AI остаются blocked.
- Полная MIT attribution сохранена в
  [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
- Матрица общего покрытия: [native parity ledger](windows/native-parity-coverage.md).
