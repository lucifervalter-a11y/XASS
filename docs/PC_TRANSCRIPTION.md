# Расшифровка текста песен на компьютерах владельца

Сервер маленький (2 vCPU, 2 ГБ, без GPU), поэтому он **ничего не распознаёт сам**.
Он хранит очередь задач и раздаёт их Windows-ПК, на которых включена настройка
«Использовать этот ПК для расшифровки текста».

## Порядок источников текста

1. Встроенный LRC в файле / LRCLIB (каталог) — всегда первым.
2. Расшифровка на ПК (`source = "pc_transcription"`, `automatic = true`,
   `label = "Автоматически, может быть с ошибками"`) — только если в каталоге нет
   синхронизированного текста. Результат важнее старой расшифровки на iPhone.
3. Расшифровка на iPhone (Apple Speech) остаётся скрытым запасным вариантом.
4. Простой (несинхронизированный) текст каталога.

## Очередь (сервер)

Таблицы `transcription_jobs` и `transcription_workers` создаются автоматически
при старте (`Base.metadata.create_all`), ALTER-миграций нет.

* Одна задача на трек (`UNIQUE(track_id)`): повторное нажатие присоединяется к
  существующей задаче, готовый результат общий для всех и **никогда не
  пересчитывается**. Неудачную задачу владелец может запустить снова.
* Состояния: `queued → assigned → running → done | failed`.
* Планировщик отдаёт задачу самому мощному **свободному** ПК: сначала VRAM GPU,
  затем число ядер CPU, затем RAM. Занят = загрузка CPU или GPU > 70 %, либо
  ПК уже выполняет задачу (максимум одна задача на ПК), либо ПК не в сети
  (опрос старше 75 с), выключен или ещё ставит компоненты.
* Аренда: предложение (`assigned`) живёт 90 с — ПК забирает его следующим
  опросом; выполнение (`running`) продлевается каждые 60 с, аренда 5 мин,
  жёсткий дедлайн `max(30 мин, 12 × длительность)`. При истечении, ошибке или
  перезапуске ПК задача возвращается в очередь, другие ПК получают приоритет;
  после 3 неудачных запусков — `failed`.

## API

Владелец (Mini App / iOS):

* `POST /api/mini/music/tracks/{id}/transcription` `{"language": "ru"|"en"|"auto", "force": false}` —
  создать или присоединиться. Если в каталоге уже есть синхронизированный текст,
  вернёт `status: "catalog_available"` и задачу не создаст (`force: true` — создать всё равно).
* `GET /api/mini/music/tracks/{id}/transcription` — статус:
  `none | waiting_for_pc | queued | running | done | failed | catalog_available`,
  `estimate_minutes`, `message` (готовая русская подпись, например
  «Расшифруем, когда включится компьютер»).
* `GET /api/mini/music/tracks/{id}/timed-lyrics` — после `done` возвращает строки
  `[{start, end, text}]` с `source: "pc_transcription"`.

ПК (заголовок `X-Api-Key` — персональный ключ агента, тот же, что для heartbeat):

* `POST /agent/transcription/poll` — возможности (GPU, VRAM, ядра, RAM), загрузка,
  `running_job_id`; в ответ — задача с короткоживущей ссылкой
  `/agent/music/tracks/{id}/stream?ticket=…` (15 мин, привязана к ключу этого ПК).
* `POST /agent/transcription/jobs/{id}/progress | complete | fail`.
  `409` означает, что аренда потеряна — ПК бросает задачу.

## Работа на ПК

`pc_client/transcription_worker.py` (в агенте) и `pc_client/transcribe_runner.py`
(отдельный процесс в отдельном venv):

1. скачивание трека по билету;
2. Demucs `htdemucs`, стем `vocals`;
3. faster-whisper `large-v3`: CUDA `float16` при наличии NVIDIA GPU, иначе CPU `int8`;
   язык из задачи (по умолчанию `ru`), `word_timestamps`, VAD; при ошибке CUDA —
   автоматический откат на CPU;
4. строки режутся по паузам > 0.9 с / длине 48 символов, фильтруются типичные
   галлюцинации Whisper («Субтитры сделал…», петли повторов).

Процесс запускается с приоритетом `BELOW_NORMAL` (или `IDLE`, если в
`config.json` указать `"transcription_priority": "idle"`), без окна, с потоками
`min(4, физические ядра / 2)` (`OMP_NUM_THREADS` и т. п.), и сам завершается,
если агент XASS закрыт.

### Установка компонентов (при первом включении, автоматически)

Модели и библиотеки в репозиторий и установщик **не входят**. При первом
включении агент сам:

1. ищет Python 3.10–3.12 (`py -3.12/-3.11/-3.10`, либо путь из
   `"transcription_python"` в `config.json`);
2. создаёт venv `%LOCALAPPDATA%\XASS\transcription\env`;
3. ставит `torch==2.5.1` (CUDA 12.1-сборку при наличии NVIDIA GPU, иначе CPU),
   `demucs==4.1.0`, `faster-whisper==1.2.1`;
4. заранее скачивает модели htdemucs (~80 МБ) и Whisper large-v3 (~3 ГБ) в
   `%LOCALAPPDATA%\XASS\transcription\models`.

Всего ~5 ГБ (с CUDA) или ~2,5 ГБ (CPU). Журнал установки:
`%LOCALAPPDATA%\XASS\transcription\install.log`. Пока идёт установка, ПК
сообщает серверу `installing` и задач не получает.

Ручная установка (если автоматическая не удалась), PowerShell:

```powershell
winget install -e --id Python.Python.3.11
$R = "$env:LOCALAPPDATA\XASS\transcription"
py -3.11 -m venv "$R\env"
& "$R\env\Scripts\python.exe" -m pip install --upgrade pip
# с NVIDIA GPU:
& "$R\env\Scripts\python.exe" -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
# без GPU:  ... --index-url https://download.pytorch.org/whl/cpu
& "$R\env\Scripts\python.exe" -m pip install demucs==4.1.0 faster-whisper==1.2.1
'{"manual": true}' | Set-Content "$R\ready.json"
```

Затем перезапустить агент XASS. Модели скачаются при первой задаче.
