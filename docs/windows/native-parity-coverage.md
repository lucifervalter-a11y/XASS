# Native parity coverage and acceptance ledger

This ledger separates source coverage from verified behavior. It is not a release
approval, an installer acceptance report, or evidence that a live account, device,
microphone, audio endpoint, updater, or AI agent was exercised.

## Evidence snapshot

| Item | Evidence |
|---|---|
| Isolated worktree | `C:\Users\Lucif\Documents\Codex\2026-10-28\task-2\xass-native-parity` |
| Working branch | `codex/native-beta-parity-20261028` |
| Current-main baseline | `81601bb56190c564c1f25928f3c60c616ff9cdf4` |
| Native donor | `ac39edb0da2d7e56b49428e1fd893f7b75f9d7a7` (`native-stable-0.21.2-ac39edb`) |
| Native merge | `6f880a914286ac7962b18c213feba5338e406add` |
| Pairing correction | source `acd6cb069156d4e606027cfcdbad48299ccec0c3`, applied as `fd7ea0da937d0ed79421149fbe39cc9ec4fd33fe`; both have patch-id `e6fb3b7f7bf445e91bf330b9f1fbc07254c71099` |
| Isolated acceptance mode | `8733a32f5fa39e16d6bad07a305cdb2d1fd3e50c` |

The actual Windows technology is C#/.NET 8 with WinUI 3, Windows App SDK
`1.8.260921001`, Windows SDK Build Tools `10.0.26100.4654`, an unpackaged
self-contained application, a private Python 3.12 helper frozen with PyInstaller,
and an Inno Setup 6 per-user installer. The similarly named `81601bb` desktop
commit is still Tkinter/ttk plus DWM styling; it is not the native implementation.

Status terms in this document are deliberately narrow:

- **Implemented**: a bounded source path exists in the combined branch.
- **Tested (synthetic)**: executable tests exercised fixtures or source contracts;
  this does not prove live Windows behavior.
- **Blocked (acceptance)**: a required real build, device, service, credential,
  hardware, or interactive check has not passed.
- **Not implemented**: the real transport or behavior is absent. UI or mock data
  does not change that status.

## Verification on the combined worktree

The following checks passed with Python 3.12.10 and workspace-local temporary
directories:

- Complete `windows/tests` discovery: **306 passed, 430 subtests passed**.
- Native XASS AI source-contract checks inside that run: **10 passed**.
- Native five-theme contract checks inside that run: **8 passed**.
- The official desktop/backend discovery was executed as four disjoint groups to
  avoid a known cross-suite Tk teardown leak: **1,018 run, 1,017 passed, 1 skipped**
  (`274 + 341 + 217 + 186`). The 26 Telegram AI/branding tests affected by the
  final owner/session isolation change were rerun afterward and all passed.

The donor documentation records broader historical synthetic suites, including
native music, voice, updater, packaging, and portable C# boundary tests. Those
counts describe the donor run, not a fresh aggregate run of this combined branch.
The current machine has .NET 8 runtimes and MSBuild 18, but `dotnet --info`
reports **no SDKs installed**. Therefore C#/XAML restore, the portable C# suites,
WinUI publish, and installer creation remain blocked. Python 3.12.10 and Inno
Setup 6 are present; Python 3.14 must not be used for the frozen native helper.

## XASS Setup parity

This table compares the recovered native client with the established Windows
Setup/agent capabilities. Detailed donor evidence remains in
[`native-desktop-parity.md`](native-desktop-parity.md) and
[`native-music-parity.md`](native-music-parity.md).

| Capability | Implementation | Automated evidence | Remaining acceptance/blocker |
|---|---|---|---|
| Native shell, overview, navigation and quick actions | **Implemented** in WinUI pages | Donor source contracts exist | **Blocked:** build/launch, keyboard, Narrator, 100/150/200% DPI, narrow/wide layout |
| Manual pairing and `.xass`/clipboard profile import | **Implemented**; manual and confirmed profile paths are separated by `fd7ea0d` | 35 bridge tests passed; `PairingFlow.Tests` contains executable interruption, retry and race checks | **Blocked:** C# suite needs .NET SDK; no live account pairing has been attempted |
| Agent start/reuse/restart/stop, heartbeat and crash recovery | **Implemented** through the owned background host | Donor synthetic lifecycle and freshness tests exist | **Blocked:** long-running recovery against a separately installed test agent |
| Files, allowed roots, clipboard, screenshots, lock and bounded commands | **Implemented** with explicit action schemas and confirmations | Bridge allowlist/projection tests passed | **Blocked:** live multi-monitor, clipboard, filesystem and lock checks were intentionally not run |
| Archive, journal, diagnostics, export, media cleanup and migration | **Implemented** | Donor failure-injection and path-boundary tests exist | **Blocked:** busy database, full disk, locked file, large archive and cancellation on Windows |
| Local and server music, queue, artwork, lyrics and cast ownership | **Implemented** from the 0.21.2 donor | Donor music suites exist; not yet rerun as the final aggregate | **Blocked:** real codecs/audio endpoint, server paging, iPhone handoff and prolonged playback |
| Transcription, local TTS and explicitly enabled microphone flows | **Implemented** from the donor | Donor voice/model-discovery tests exist | **Blocked:** real microphone, system voice, local model and cancellation/close behavior |
| Updates, verified download, backup, health check and rollback | **Implemented** for the native channel, with auto-update default off | Donor updater failure-injection suites exist | **Blocked:** real cross-version install, failed-health rollback, locked files and interrupted updater |
| Appearance, accent, light/dark/high-contrast resources | **Implemented**, including exact Classic/Cyber/Tokyo/Nordic/Matrix palettes | Eight current Python source-contract tests plus portable C# appearance tests are present | **Blocked:** .NET SDK and interactive high-contrast/accessibility checks |
| Tray, close-to-tray, single instance and `.xass` activation | **Implemented** | Test-mode isolation contracts passed | **Blocked:** Explorer restart, activation, shortcut, association and final-quit acceptance |
| Security boundaries and secret-free bridge projection | **Implemented** | 35 current bridge tests passed | **Blocked:** compare real config/key files before and after an isolated live run |
| Complete test installer and uninstall/preservation flow | Source and packaging scripts are **implemented** | Historical native installer CI exists only for the donor | **Blocked:** no .NET SDK, no current combined build, no fresh-install/upgrade/uninstall acceptance |

## Isolated test mode

Setting `XASS_NATIVE_TEST_ROOT` to a safe absolute directory places data and
preferences beneath that directory, assigns a root-specific activation identity,
and disables background services and updates. The implementation rejects volume
roots, the user profile, Local AppData, and the installed XASS data/preferences
trees. This is the only permitted mode for local acceptance until a separate test
installer has passed. It does not authorize reading or changing the currently
installed XASS profile.

## Union parity across desktop, backend, iOS and Telegram

“Union parity” means the native client can coexist with and expose the real
capabilities present across the current main branch without inventing success.

| Area | Current combined status | Evidence/limits |
|---|---|---|
| Devices and PC commands | **Implemented, partially tested** | Native connection/device/command pages and existing backend/agent protocols are present. Live multi-device behavior remains blocked. |
| Files, clipboard, screenshots and archives | **Implemented, synthetic-tested** | Bounded native bridge exists and 35 bridge tests passed. iOS/Telegram/backend live round trips remain acceptance work. |
| Music and now-playing | **Implemented, donor-tested** | Native local/server player plus current backend/iOS/Telegram music code are present. Cross-surface real playback/handoff is blocked. |
| Settings, themes and accessibility | **Implemented at source-contract level** | Native presets match all 26 canonical fields and aliases from `pc_client/themes.py`; system/light/dark, custom accent and High Contrast paths remain explicit. Cross-surface live preference sync and accessibility acceptance are still blocked. |
| Updates and migration | **Implemented, donor-tested** | Native channel is isolated from the legacy Tk channel. Combined installer/update/rollback is blocked by toolchain and live acceptance. |
| Security and pairing | **Implemented, synthetic-tested** | Confirmed profile import is isolated from manual pairing; secret-free bridge projections pass. Live pairing and before/after key inspection remain blocked. |
| Telegram bot and Mini App | **Existing current-main functionality; integration not live-tested** | No bot migration or deployment was performed. AI-facing endpoints are not proof of a real AI transport. |
| Native iOS application | **Implemented for established non-AI flows; not live-tested here** | The live entry is `XASSApp` → `NativeShell`, with Home/Music/Site/Tools/Weather, device commands, PC workspace, files/clipboard, storage and music. No native updater flow, Windows-hosted Swift build, signing or device run is claimed. |
| XASS AI on Windows | **Partial, source-tested** | Native code validates exact token-auth `/api/health`, uses loopback `/api/ws`, performs `session.create` → `prompt.submit`, filters delta/complete by session, and keeps Send disabled until an owned runtime is bound. Ten source-contract checks pass. No C# build or live runtime exchange has occurred. |
| XASS AI on iOS/Telegram/Mini App | **Not implemented end-to-end; fail-closed** | Health and interactive capability are separate; all controls remain disabled without explicit `interactive_available: true`. Mutating routes require owner auth plus action proof, Telegram publishes `/ai*`, and fabricated answers/sessions/tools were removed. There is still no approved phone-to-PC transport. |
| Discord voice join | **Not implemented** | Presence/navigation helpers are not an authorized Discord voice integration and must not be labelled as a successful join. |

## XASS AI / Hermes 0.22.2 contract

The installed external runtime identifies itself as Hermes `0.22.2`, commit
`d923a5eb487a17c0a68801bba9ef0f56c81040a8`. In product UI it should be named
**XASS AI**. Hermes remains the internal runtime name and must retain its MIT
attribution; see [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).

The verified runtime contract is:

- `GET /api/health` returns `ok`, `version`, `displayVersion`, `commit`, and
  `auth_required`.
- Chat uses WebSocket JSON-RPC at `/api/ws`, not guessed REST/OpenAI endpoints.
- A loopback token is supplied as `?token=...` and must match the process token.
- `session.create` creates a session; `prompt.submit` sends `{session_id, text}`;
  `message.delta` streams output; `message.complete` is terminal.
- The typical standalone port is 9119. Desktop-owned startup should prefer port
  `0` and discover the actual endpoint through `HERMES_DESKTOP_READY_FILE`.

Known false-positive paths in the original current-main snapshot included fabricated
status/session/chat/skill/tool results in `app/services/hermes.py`, unconditional
success wrapping in `app/hermes_api.py`, online detection for any HTTP status below
500 plus guessed endpoints in `pc_client/hermes_client.py`, and demo/offline success
in iOS and Mini App code. The combined working tree removes those paths, requires
exact version/commit plus `auth_required: true`, bounds health bodies to 64 KiB,
and fails closed. A 401, 404, absent token, missing ready file, protocol mismatch,
disconnect, or incomplete terminal event produces an unavailable/error state.

No paid API fallback is permitted. No new credentials may be created, the bot is
not moved, and no network/security setting is changed. A phone transport has not
been agreed or implemented and must remain explicitly blocked.

The current native working-tree implementation adds `XassAiProtocol`,
`XassAiRuntime`, and a main Navigation page. It accepts only loopback, requires
`ok: true` and `auth_required: true` from the exact health response, keeps the token in memory, filters
stream events to the exact session, and buffers UI deltas for 75 ms. The runtime
binder currently accepts a ready-file port and private token handoff; it does not
yet launch/discover the installed runtime or attach to an arbitrary PID. Only a
`Process` instance launched and owned by XASS may be terminated. This is a
source-tested partial implementation, not runtime verification.

## Performance acceptance gates

The native architecture moves bounded process/network work off the UI dispatcher
and virtualizes the music list, but this is not measured performance evidence.
Before describing the app as responsive, record on the target Windows machine:

1. Cold start and time-to-first-interaction over at least ten runs (median and p95).
2. Navigation and button input latency while a bridge/network request is delayed.
3. Music-list scrolling frame time at 100%, 150% and 200% scale with more than 100 rows.
4. Rapid repeated search/transport actions, proving only the newest generation is rendered.
5. Idle, active, minimized and tray CPU/memory over a prolonged run.
6. Close-to-tray and final-quit latency while child requests are active.
7. Failure/retry behavior for stopped agent, revoked key, offline server and malformed responses.
8. Side-by-side comparison with the existing Setup client using the same data fixture.

Record raw measurements and hardware/build identity. Source design, screenshots,
or a successful unit suite alone cannot close these gates.

## Release blockers

- Install or otherwise make available a compatible .NET 8 SDK; the present machine
  has runtimes only. This requires a separate approved environment change.
- Restore/build/publish the exact combined commit and run every portable C# suite.
- Build a separate test installer and inspect its payload plus generated licenses.
- Run only in isolated test mode or a separate test installation; do not replace
  the current installed XASS.
- Complete the live Windows, iOS/Telegram/backend, performance, update/rollback,
  and XASS AI acceptance listed above.
- Do not publish, merge, deploy, or change production credentials/network settings
  without a separate approval.
