# XASS Native for Windows · initial WinUI 3 milestone

A separate, real WinUI 3/C# shell. The existing Python/Tk application and agent
remain intact. This is **not feature parity, a replacement installer, or a
Windows-tested release**. A Windows build and runtime acceptance pass are
required before distributing it to users.

## Implemented scope

- Adaptive native NavigationView, system-theme controls, status/error/progress states.
- Existing paired **local** PC status, with expired reports shown offline.
- Authenticated server music library: search, 100-track pages, forward/back navigation.
- Play selected server track on this agent; pause/resume/stop/seek/volume through
  the agent's existing authenticated loopback player bridge.
- Agent-authoritative playback status refreshed every five seconds. No fake
  track state or optimistic claim that a queued request is already playing.
- Serialization of UI operations, finite request timeouts, process cancellation
  on window close, keyboard-accessible native controls, bounded bridge output.

### Visual and responsiveness work

The shell uses themed rounded cards, a clear accent hero, consistent type and
spacing, native glyphs, purposeful empty states and a compact player. Mica is
enabled only when supported; other systems keep an opaque theme background.
Native theme resources preserve light/dark/high-contrast choices. Page gutters,
navigation and the player viewport adapt to narrower windows; advanced player
controls are collapsed by default so the library retains space.

The library explicitly uses `ItemsStackPanel` virtualization inside a finite
star-height grid, never an outer scrolling stack. A page replaces its data source
once; status polling does not rebuild the library or reset its scroll position.
The complete process lifecycle, including filesystem probes and process launch,
runs off the UI dispatcher. At most one request runs at a time. Repeated searches
retain only the latest requested query and discard superseded catalog results;
the search field remains editable during requests. Repeated transport clicks
are disabled while their operation is outstanding.

Status polling uses a standard-library-only Python path, without loading audio,
HTTP or cryptography modules. It pauses while the window is inactive and refreshes
when activated. Closing still synchronously signals process termination before
the last window exits, prioritizing cleanup over an unverified asynchronous
exit change; Windows acceptance must measure close latency too.

These are architectural improvements and source-level checks, **not measured
FPS, input latency, startup-time claims, or proof of visual quality on Windows**.

The device screen deliberately shows only this PC: an agent key is not an owner
session and cannot enumerate or control other PCs. Use the existing Mini App or
iPhone app for remote devices. Pairing, updates, file browsing, archives, local
file import, artwork/lyrics and OS commands remain in the existing application.

## Architecture and security boundaries

`Xass.Native` starts the bundled `bridge.py` using the chosen trusted Python
interpreter with `-I -B`, a shell-free argument list and redirected pipes. Each
process accepts exactly one bounded JSON request then exits. The helper uses
`pc_client/secret_store.py`, `network_client.py` and `music_bridge.py`; it does
not start an agent, listener, authentication flow or a new credential store.

The UI accepts absolute Python/source/data paths in memory, never passwords or
API keys. Existing sealed configuration is decrypted only inside Python when
an authenticated library action requires it. Keys, media URLs, bridge tokens,
raw errors and arbitrary response fields are not returned to C#. There is no
config restoration, key provisioning or secret migration. Dependency imports
may create existing agent runtime directories; no new credential is written.

Only snapshot, catalog, play, pause, resume, stop, seek and volume are allowed.
Controls validate numeric bounds. Catalog auth uses the existing transport
policy and disables redirects. Player commands reuse the existing loopback
bridge instead of receiving its token. The adapter must be run only against
trusted XASS source code and an interpreter with the normal agent dependencies.

## Build on Windows

Requirements: Windows 10 1809+ / Windows 11, .NET 8 SDK, Visual Studio 2022 Build
Tools with Windows SDK/MSBuild support. The project targets Windows SDK
10.0.19041.0, minimum 10.0.17763.0. NuGet references are pinned:

- Microsoft.WindowsAppSDK `1.8.260921001` (1.8.12)
- Microsoft.Windows.SDK.BuildTools `10.0.26100.3916`

From the repository root:

```powershell
dotnet publish windows/Xass.Native/Xass.Native.csproj -c Release -r win-x64 -p:Platform=x64 -o artifacts/windows-native
```

The entire output folder is required, including `bridge.py`. This is unpackaged
and self-contained for .NET and Windows App SDK; it does **not** bundle Python,
the Python dependencies or the legacy agent. Run `artifacts/windows-native/Xass.Native.exe`.
ARM64 is declared but has not been built or tested; x64 is the first validation target.

## Connect an existing agent

1. Install/pair/start XASS using the existing app. Do not run another agent over it.
2. Have this repository's trusted `pc_client` source and its Python environment available.
3. In the native app's «Подключение» screen select absolute paths to that
   environment's `python.exe`, the `pc_client` source directory, and the agent's
   existing data directory.
4. Installed agents use `%LOCALAPPDATA%\XASS`; source-run agents use their
   `pc_client` directory. The source version must match the running agent.
5. Click «Использовать агент», then open «Музыка» and click «Найти».

No live account, agent, file library, audio device, or credentials were accessed
while developing this milestone. Connection and playback need validation on
the user's Windows test system before claiming readiness.

## Verification

Cross-platform isolated adapter/contract tests:

```bash
python -m unittest discover -s windows/tests -v
python -m compileall -q windows/bridge.py windows/tests
```

These use fixture data and mocks; they do not prove Windows compilation or UI
behavior. XML/static validation similarly does not invoke the WinUI XAML compiler.

Mandatory Windows acceptance checklist:

- Restore/build/publish succeeds; launch from the complete artifact directory.
- 100%, 150%, 200% scale; narrow/wide navigation; Tab/Enter controls; light/dark/high contrast.
- Unpaired/missing/broken config, absent Python dependency, stopped agent, network
  loss, revoked agent key, stale/future status reports show recoverable errors.
- Library pages >100 tracks, Unicode search, empty results, repeated clicks,
  failure then retry, page/back consistency, and switching pages while loading.
- Play/pause/resume/stop/seek/volume reflect the real agent; existing iPhone
  handoff still works; no competing audio owner is started.
- Close during a request cancels the child process; agent/audio remain running.
- Measure cold startup, scroll frame time, input latency during a delayed catalog
  response, idle/inactive CPU and memory, and close latency on target hardware.
  Repeat a Unicode search rapidly and verify only its newest result is rendered.
- Compare config/key files before/after; inspect process arguments and bridge
  responses to verify no credential copying/leakage.

## Official references

- [Microsoft: unpackaged WinUI 3 deployment](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/unpackage-winui-app)
- [Microsoft: NavigationView](https://learn.microsoft.com/en-us/windows/apps/design/controls/navigationview)
- [Microsoft Windows App SDK 1.8.12 release](https://github.com/microsoft/WindowsAppSDK/releases/tag/v1.8.12)
