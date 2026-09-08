# XASS iOS

SwiftUI оболочка для существующего интерфейса XASS, отдельный AVPlayer для фоновой музыки и локальных загрузок. iOS/iPadOS 17+. Версия 0.16.0.

## Сборка

На Mac с Xcode и выбранными command-line tools:

```sh
brew install xcodegen
bash ios/prepare.sh
open ios/XASS.xcodeproj
```

Выберите свой Team и bundle identifier для установки на устройство. CI `Build XASS iOS (unsigned)` запускает unit tests в симуляторе и собирает arm64 `.ipa` без подписи. Артефакт `XASS-iOS-UNSIGNED-requires-signing` содержит инструкцию и SHA-256. Подробнее: [UNSIGNED.md](UNSIGNED.md).

## Границы безопасности

- Настраивается только HTTPS origin, без userinfo. Native audio принимает сообщения только от основного WKWebView, main frame и exact origin (с портом).
- Origin и HttpOnly PWA-cookie сохраняются в Keychain `AfterFirstUnlockThisDeviceOnly`. Одноразовая ссылка не записывается на диск. WebKit хранит собственные website data, включая E2E owner key; выход очищает их.
- Face ID **или системный код-пароль** требуется при возвращении в приложение. При сворачивании window-level privacy shield закрывает также sheets. Музыка не останавливается.
- Native stream использует AVAssetResourceLoader и URLSession с проверкой каждого redirect; ticket не уйдёт на другой origin. Не используются ATS/TLS bypass или произвольные заголовки из JS.
- Загрузки находятся в sandbox Application Support, исключены из iCloud backup и защищены iOS FileProtection. Индекс содержит название/исполнителя, но не URL/ticket. Разные серверы имеют отдельные namespaces. Максимум 256 МиБ на трек. Загрузку лучше завершить до закрытия приложения; отдельного фонового download-service нет.
- «Удалить локальную копию» не удаляет трек на сервере. «Отключить сервер» сохраняет оффлайн-файлы; вернувшись к тому же origin и разблокировав приложение, можно снова их слушать.
- Звуковые форматы зависят от AVFoundation. Предпочтительны MP3/M4A/WAV; наличие FLAC/OGG в серверной библиотеке не гарантирует воспроизведение каждого кодека на iOS.

## Bridge

В доверенную страницу на `documentStart` добавляется `window.XASS_NATIVE_AUDIO = true`.

```js
window.webkit.messageHandlers.xassAudio.postMessage({
  action: 'play', trackId: 7, title: 'Track', artist: 'Artist',
  url: '/proxy.php?_binary=1&_media=1&_p=' + encodeURIComponent('/api/music/tracks/7/stream?ticket=...'),
  position: 0, volume: 70,
  session: {session_key: 'browser-session-key', device: 'local', share_site: false, share_discord: false}
});
```

Actions: `play`, `pause`, `resume`, `stop`, `seek`, `volume`, `route`, `download`, `downloads`, `session`, `queue`, `next`, `previous`.
`url:''` допустим для `play` только если уже существует локальная копия этого trackId в текущем origin. `route` открывает native sheet с системным AVRoutePickerView; выбор маршрута требует нажатия владельца.

Событие `window` `xass:native-audio` содержит `detail:{state,position,duration,trackId,native:true,error?}`. States: loading/playing/paused/stopped/ended/error. Download: `{action:'download',trackId,downloaded,error?}`. Каталог: `{action:'downloads',downloads:[{trackId,title,artist,duration}]}`.

`play` / `queue` могут передать `queue:[{trackId,title,artist}]` (не более 200 разных треков) и `repeat:'off'|'all'|'one'`. Очередь уже должна быть в желаемом порядке shuffle. Переход после конца трека и next/previous с экрана блокировки выполняет native, без JavaScript; для следующего несохранённого трека native запрашивает свежий ticket с HttpOnly cookie. При потере сети доступна только сохранённая очередь.

Native обновляет переданный music session в фоне каждые 5 секунд воспроизведения и сразу при pause/stop/end с тем же session_key, без takeover. Cookie берётся из Keychain и не отправляется в JS. Ошибка сети не останавливает локальное воспроизведение; состояние сервера в отсутствие связи обновить невозможно.

API references: [AVAudioSession](https://developer.apple.com/documentation/avfaudio/avaudiosession), [AVAssetResourceLoaderDelegate](https://developer.apple.com/documentation/avfoundation/avassetresourceloaderdelegate), [LocalAuthentication](https://developer.apple.com/documentation/localauthentication/lapolicy/deviceownerauthentication), [WKScriptMessage.frameInfo](https://developer.apple.com/documentation/webkit/wkscriptmessage/frameinfo), [XcodeGen](https://github.com/yonaskolb/XcodeGen/blob/master/Docs/ProjectSpec.md).
