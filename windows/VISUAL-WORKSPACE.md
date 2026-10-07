# Native music and assistant workspace

The 0.21.2 workspace keeps the existing native audio, command validation,
confirmation, updater, pairing and microphone lifecycle boundaries.

## Use

- Open **Музыка → Открыть файлы** and choose WAV, MP3, FLAC or OGG files (up to 40).
  **Очередь** switches between local files and the connected server library.
  Enter or double-click starts the selected track. The transport supports pause,
  resume, previous/next, reset, position and volume; sliders work with the keyboard.
- A wide window shows square artwork and transport on the left and lyrics on the
  right. Narrow windows put artwork/metadata above the queue/history/lyrics tabs,
  with transport fixed below. Only supplied track metadata is displayed. Timed
  lyrics follow the actual playback position; the follow button can be disabled.
  The history is the most recent 50 observed track starts in this XASS session.
- **Помощник → Компактный вид** hides navigation and resizes the same window.
  Type a supported command and choose **Проверить текст**. Review the action before
  **Выполнить**, or cancel it. Example buttons only fill the text field. **Сказать**
  explicitly starts a six-second recording through the existing local recognizer.
  A local faster-whisper model is required for speech. Text commands do not require
  it; the optional existing Ollama provider remains configurable.
- Appearance changes preview immediately; **Сохранить** persists the theme and
  accent. Artwork colors tint the music background; high contrast uses the system
  window color. Busy avatar animation respects the Windows animation setting.
- **Музыка → Discord** exposes the existing opt-in tokenless presence adapter.
  No public XASS Application ID was present in the authorized source/configuration
  checked for this task. Sharing therefore stays disabled until a valid public
  Application ID is supplied. It does not use an account token or join voice calls.
  Assistant voice-channel joining separately requires an authorized OAuth/RPC
  connector; it remains unavailable when that connector is not configured.

## Build

Run `windows/build_installer.ps1` with trusted Python 3.12 x64, .NET 8 and Inno Setup 6.
The script creates a fresh isolated build environment, runs boundary tests, freezes
the companion, verifies it, and stages a personal-data-free installer. Use
`-Distribution native -Revision local-build` for an uncommitted local candidate.
The build never bundles microphone recordings, account configuration or a model.

Installer registration/uninstall and upgrade/rollback smoke tests are intentionally
restricted by `test_installer.ps1` to an ephemeral GitHub Actions runner. Release
packages should use the exact reviewed commit SHA and pass those tests before
installation. Exit through **Команды → Выйти из XASS**, then run the verified
installer in the existing per-user installation directory to preserve settings.

## Return to stable 0.21.1

Exit XASS through **Команды → Выйти из XASS**. Run the retained official 0.21.1
`XASS-Native-Setup.exe`, revision `9b26976d2eccb7bfe707ad37348aa12e1096ab55`,
using the same existing per-user installation directory. Its SHA-256 is
`68615f596e91b4201a1f433f44f05b02ae915c45ea9d79b01ddd66cb0e28eb9e`.
Do not uninstall/delete the XASS data directories to roll back. Preserve the
native-updater backup and verify the restored executable and agent revision.
