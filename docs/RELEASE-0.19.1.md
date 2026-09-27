# XASS 0.19.1 — music handoff diagnostics

## Fixed

- A failed device transfer used to return HTTP 200 with `ok:false`. The native transport rejected that valid failure receipt as an unsupported server response, hiding the actual reason. Transfer resources now use `ok:true` for a successfully read resource and retain `status:failed`, its receipt ID and reason. Playback still requires `status:ready`; no ownership or stop acknowledgement is bypassed.
- The iOS decoder also understands the narrowly validated legacy failure receipt from 0.19 servers. A failed operation releases the busy state and can be retried; the selected device is not falsely marked connected.
- Safe, stable failure codes distinguish source acknowledgement, target startup, cancellation and timeout cases without recording raw agent messages.
- Abandoned local sessions (no update for five minutes) can be released with an explicit owner confirmation that the previous device's audio is stopped. The server checks the source key and revision again; live sessions, PC sessions and ongoing transfers cannot be cleared this way. Recovery does not start audio automatically or pretend to stop an offline device.

## Share diagnostics from iPhone

Under a music error, choose **Отправить диагностику**, or open **Инструменты → Журнал приложения**. Prepare the JSON file and share it through the system sheet. The last 200 structured events survive an app restart; they contain app version, timestamp, operation/step, HTTP/envelope status, response size, latency and an allowlisted error category. Recording can be disabled and history cleared. Nothing is uploaded automatically.

There are no cookies, keys, tickets, URLs, response bodies, device identifiers/names, track names, file paths or media in this report. Successful background session polling is omitted so it cannot bury the useful failure events.

## Verification boundary

Regression coverage includes real PHP envelopes through native URLSession and NativeStore (old and new servers), failed start/poll/cancel flows, a successful retry, browser refusal to play after failure, and diagnostics privacy/size/persistence/export. CI builds and simulator tests are not proof of physical iPhone-to-PC audio operation. iOS releases remain unsigned and require the user's signing method. Windows binaries are unchanged by this patch.
