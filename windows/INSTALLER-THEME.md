# Native test installer and appearance

## Implemented

- **Appearance** page: Windows/light/dark mode and Windows/custom opaque accent.
  Apply/save and reset are explicit. Settings contain only schema, theme and
  accent in `%LOCALAPPDATA%\XASS.Native\appearance.json`; atomic replacement
  preserves the previous file if a write fails. Corrupt/oversized/future-format
  files fall back without overwriting the original on read.
- Custom accent affects the hero and primary buttons. Normal, hover and pressed
  text choose opaque black or white with at least 4.5:1 calculated sRGB contrast.
  Other controls, focus rings, semantic colors and disabled states remain WinUI.
  Windows contrast themes override custom accent using Highlight/HighlightText.
  Changes in system color/theme are dispatched to the UI thread and brush objects
  are updated without rebuilding the music list.
- A **single installer download**, `XASS-Native-Test-Setup.exe`, installs the
  entire native publish folder and a frozen Python companion. The installed app
  has adjacent runtime files; the native executable itself is not a single-file app.
- Separate per-user test identity, no elevation, no startup task or file
  association takeover, no stable installer/updater replacement. Upgrades clean
  only installer-owned runtime/source/license folders and replace program files.
  Agent data, pairing and native preferences survive upgrade and uninstall.
- Explicit source allowlist, symlink/private-file rejection, PE architecture
  checks, required-file checks, per-file SHA-256 payload manifest, installer hash,
  Python dependency inventory and collected license notices.

## Companion contract

Installed executable: `runtime/XASS.NativeHelper.exe`, console subsystem, always
started by WinUI with redirected pipes and `CreateNoWindow=true`.

| Role | Module | Protocol |
| --- | --- | --- |
| `--role agent-bridge` | `bridge.main` | one JSON request; `--source`, `--data` |
| `--role assistant` | `assistant_bridge.main` | one JSON request plus progress lines |
| `--role listener` | `background_voice_bridge.main` | listener stdin/stdout protocol |
| `--role background-agent` | `background_agent.main` | managed host protocol / agent-child flags |
| `--role desktop-music` | `desktop_music_service.main` | managed music JSONL protocol |
| `--health-check` | dependency imports and role presence | JSON status, no agent/model/microphone |

Roles use frozen imported code, never arbitrary caller-supplied script paths.
The native UI should prefer the bundled executable and retain trusted external
Python only as a development fallback. The managed background agent must disable
legacy source/installer auto-updates; native updates use the native test installer.

## Build on Windows

Prerequisites: Windows x64, trusted Python 3.12 x64, .NET 8 SDK and Windows build
SDK, Inno Setup 6. This script never downloads or executes a tool installer.
It installs the repository's pinned Python packages into a fresh build-only venv
from the configured Python package registry and freezes them with PyInstaller.

```powershell
.\windows\build_installer.ps1 -Revision <full-commit-sha>
```

Output: `artifacts/windows-installer/`. All build paths are new, build-only folders.
No source tree, model cache, agent configuration, key or user library is swept in.

The build runs native Python contracts and portable C# appearance tests, publishes
WinUI self-contained, freezes and health-checks the companion, stages only
verified files, then invokes Inno. It does not push, publish a release, or deploy.
The parent publication workflow should call this script with its tested SHA,
upload only the installer/checksum/metadata, then verify published bytes.

On an ephemeral Windows GitHub runner only:

```powershell
.\windows\test_installer.ps1 -Installer artifacts/windows-installer/XASS-Native-Test-Setup.exe -PayloadManifest artifacts/windows-installer/payload-manifest.json
```

This checks fresh installation, same-version reinstall/upgrade, every installed
file hash, installed runtime import health, uninstall and preservation of unique
nonsecret data markers. It never launches the GUI. Interactive acceptance remains
mandatory, including upgrades from an older published native build.

## Dependencies and limits

- Bundles Python 3.12 and installed `pc_client/requirements.txt`,
  `voice-requirements.txt`, `build-requirements.txt` runtime dependency closure.
  .NET and Windows App SDK are self-contained. No separate Python setup is needed
  for bundled roles.
- **Whisper model is not bundled or downloaded.** The existing local
  faster-whisper model folder is still required for speech. Text actions and the
  rest of the app do not need it. Ollama and its model remain optional external
  dependencies. Discord automatic voice joining requires separate integration.
- CTranslate2/faster-whisper CPU runtime can make the payload hundreds of MB;
  actual installer size is not known until the Windows build. CUDA/cuDNN are not
  bundled. No GPU availability or performance promise is made.
- Top-level dependencies follow existing exact repository pins; transitive
  versions are resolved at build time and recorded in `python-dependencies.json`.
  This is an inspectable dependency inventory, not a reproducibility claim.
- Python and discoverable dependency license files plus available NuGet notices
  are collected. `NOTICE-REVIEW.txt` lists missing notices. Redistribution terms,
  native-library notices, codec obligations and model licenses still need review
  before treating this as a stable public distribution. Collection is not legal
  approval or a complete SBOM audit.
- Installer remains unsigned until an authorized signing process is provided;
  no signing credential is generated or used. SmartScreen reputation and signing
  are not solved by a SHA-256 checksum.

## Verification status for this implementation

Linux/cloud source tests passed. WinUI compilation, the portable C# test program,
PyInstaller freezing, Inno compilation and Windows install/runtime/UI acceptance
cannot run here because no Windows SDK, dotnet, PowerShell or Inno is installed.
The combined build must include the voice, desktop and music workers' modules
listed by `native-payload.json` before invoking the installer script.

Manual Windows appearance checks: restart persistence, corrupt-file recovery,
read-only settings folder, repeated Apply/Reset, close during save, live Windows
light/dark/accent/contrast changes, Tab/Enter, 100/150/200% scale, narrow layout,
color-picker keyboard operation and button normal/hover/pressed/disabled colors.

References: [WinUI lightweight styling](https://learn.microsoft.com/windows/apps/design/style/xaml-styles),
[Windows contrast themes](https://learn.microsoft.com/windows/apps/design/accessibility/high-contrast-themes),
[Inno previous install directory](https://jrsoftware.org/ishelp/topic_setup_usepreviousappdir.htm),
[Inno application shutdown filter](https://jrsoftware.org/ishelp/topic_setup_closeapplicationsfilter.htm).
