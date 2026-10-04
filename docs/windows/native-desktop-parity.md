# Native desktop migration: implementation and acceptance

This is a source-level migration to native WinUI controls and typed local services. No native page opens the Tk UI. This document distinguishes implemented paths from actual Windows acceptance; it is not a claim that real Windows installation, microphone, audio, pairing, or rollback has passed.

## Runtime contract

- `agent-bridge` remains a bounded one-request process. Its `desktop_*` schemas have no arbitrary method, shell or unrestricted filesystem operation.
- `background-agent` is directly owned by `DesktopHostClient`, supervises an existing validated agent or its own `--agent-child`, and is independent of the one-shot request kill-tree. The old backend command, telemetry, archive, transcription and music protocols remain in the existing headless agent.
- Runtime/data roots are absolute and bound before legacy module imports. `XASS_DATA_ROOT` is applied consistently by client_update/runtime_state. Settings and pairing use a cross-process OS lock, validated field allowlist, existing encryption and atomic backups. Invalid encrypted data is never converted into an empty-key replacement.
- Music has its own directly owned process and explicit cross-process audio ownership. See `native-music-parity.md` for M01–M16 and audio-specific contracts.
- A current-user-only named activation pipe and mutex provide one native window with `.xass` activation. Tray close hides the window and stops microphones; deliberate Quit closes backend/audio owners. Minimize preserves an explicitly enabled microphone.

## Feature matrix

“Implemented” below means source + applicable synthetic tests, with live Windows acceptance still pending.

| Audit IDs | Native path / implementation | Status and evidence |
|---|---|---|
| A01 | Overview, PC, Connection, Files, Music, Archive, Journal, Updates, Settings and Commands; Appearance/Assistant are added by their workstreams | Implemented native pages; Windows navigation/DPI/Narrator pending |
| A02 | Time/owner greeting, connection summary, CPU/RAM/disk, initial/reloaded recent events and quick actions | Implemented; metrics and diagnostics read off dispatcher. Continuous event streaming is replaced by bounded refresh |
| A03 | Mini App discovery via canonical `/api/pwa/config` fields and credential-free browser URL | Implemented; strict scheme/authority/port checks |
| A04–A06 | Connection picker, drop, clipboard JSON, profile expiry preview, manual pairing, sealed key save, agent restart | Implemented; expired/oversized profile fixtures and atomic configuration tests. Real pairing pending |
| A07 | Native activation of `.xass` through the single-instance pipe | App handler implemented; installer association and double-click acceptance depend on combined packaging |
| A08–A10 | Start/reuse/restart/stop, PID creation-time checks, crash backoff, five-failure stop, update coordination | Implemented host; live Windows lifecycle and long-running recovery pending |
| A11–A12 | Native tray Restore/Check/Microphone stop/Quit, close-to-tray fallback, `--minimized`, single instance | Implemented. Explorer restart re-adds tray icon. Interactive Windows acceptance pending |
| A13 | Freshness validates PID, creation time, future timestamps, configured interval | Implemented; stale/reused/future process fixtures pass |
| A14–A18 | Machine/user/OS/time/uptime, metrics/processes, server/agent version/latency/status, reachability check | Implemented; bounded process rows and finite network timeout. Real host/network pending |
| A19–A21 | Local multi-screen JPEG, local clipboard preview, confirmed Windows lock | Implemented native command dialogs; no actual capture/clipboard/lock executed during development |
| A22 | Saved legacy auto-update preference preserved; separate explicit native-auto opt-in | Implemented migration behavior, avoiding implicit activation of a new updater |
| A23 | Song transcription enable/consent, setup stage/status and retry through agent restart | Existing worker reused; download/runtime/model behavior not exercised live |
| A24–A27 | Owner/telemetry/archive limits and retention, folder picker, protection/E2E diagnostics, persistent runtime paths | Implemented; finite-number, minimum interval, host/key preservation, failed-write and locking fixtures |
| B01–B03 | Allowed roots, nested browse/Up/root reset/refresh/retry, Unicode, explicit 250-row warning | Implemented; traversal/symlink/nesting/truncation fixtures and latest-navigation generation guard |
| B04–B06 | Archive overview, last 500 rows with all markers, message text dialog, open folder | Implemented; schema projection tests. Large archive performance acceptance pending |
| B07–B08 | Empty target folder, optional copy, free-space/write probe, progress/cancel, SQLite backup/path rewrite, source retained | Implemented; successful/cancelled/nonempty/overlap/write-failure fixtures. Busy-writer/full-disk Windows acceptance pending |
| B09 | Irreversible media cleanup confirmation, agent stopped, root-safe DB paths, text retained, freed counts | Implemented; unconfirmed and out-of-root cleanup rejected by fixtures |
| B10–B12 | Read-only diagnostics, redacted bounded log tail, JSON Save picker, recent-event view | Implemented. Paths, archive state, runtime/agent/update versions and encryption status included; raw credentials excluded |
| B13–B15 | Native-test update discovery, progress, verified installer, explicit/manual or separately opted-in background application, backup/health/rollback state machine | Source and failure-injection tests implemented; real cross-version installer rollback remains a required Windows gate |
| B16 | One per-user native test installer with companion runtime | Owned by installer workstream; fresh install/upgrade/uninstall/source migration acceptance belongs to combined release |
| M01–M16 | Local queue/server library/player/artwork/lyrics/cast arbitration/reveal/keyboard | Implemented by music workstream; see separate detailed matrix |
| C01–C04 | Appearance, TTS, background microphone, original assistant actions | Owned by theme/voice workstreams and combined acceptance |
| C05 | Actual Discord voice join | Still requires authorized Discord integration. Navigation must not be labelled a successful join |
| C06 | Single ready-to-run test installer | Combined packaging/release gate; bundled speech-model presence must be reported separately and honestly |

## Native update and rollback design

Only official `lucifervalter-a11y/XASS` prereleases with the exact `native-test-` tag prefix, `native-test-update.json`, and `XASS-Native-Test-Setup.exe` are considered. Host redirects are allowlisted; no agent key is transmitted. Version/revision/size/SHA-256 and installed native AppId are checked. The legacy stable/Tk update feed cannot replace the native runtime.

The UI copies its private helper runtime outside the installation, starts the `native-updater` role, and waits for a verified full-program backup before exiting. The updater validates the previous payload inventory, waits for the exact UI process identity to exit, stops only executables in the owned install root, runs the installer at the original directory, validates the new complete payload and companion health JSON, then requires an acknowledgment from an actually loaded native window and responsive host protocol. Dispatch/process creation alone is not success.

On failure after installation begins, it rechecks the backup hashes, copies a restore tree, quarantines the failed installation without deleting it, swaps the prior tree back, restores the bounded per-user uninstall metadata, and health-checks/relaunches the old revision. Agent data and native preferences are never in the replaced root. State is durably written to `XASS.Native/updates/last-result.json`; a failed rollback says so and retains the verified backup. Cancellation is accepted before installation. No unrequested rollback claim is made after a process/OS crash; an interrupted updater that cannot run needs recovery review.

Automatic native updates default off independently of any old-agent value. The user must explicitly enable them. Checks occur every 30 minutes and installation is deferred until the native window is hidden in the tray, music is inactive, pairing is idle and no archive job is active. Closing into the tray already stops microphones. There is no automatic Windows reboot.

## Verification performed

- Native aggregate before updater: 140 tests passed, 6 WinMM/Windows skips.
- Native aggregate including updater: 160 tests passed, 6 WinMM/Windows skips.
- New updater tests: 20 passing, including backup/cancel/checksum/symlink/schema/installer/health/rollback failure injection, preserved/quarantined runtime, PID reuse and update lock.
- Existing secret store: 2 passed; client update: 11 passed; runtime: 4 passed; archive: 3 passed; Mini App URL: 10 passed; music bridge: 7 passed.
- Existing connection-file suite was not runnable in this container because the unrelated server QR import requires unavailable `segno`; equivalent native profile validation tests passed.
- Integration compiled the non-WinUI C# services, including coordinator/preferences, under .NET 8 with zero warnings/errors.

No test above ran a real installer, paired a real account, changed system settings, captured a live screen/microphone, rebooted a machine, or published a release.

## Release gates still required

1. Full C#/XAML compilation and package inventory at the final combined commit.
2. Fresh Windows x64 install without developer Python/source; `.xass` association, tray/single-instance/autostart, uninstall data retention.
3. Live paired agent lifecycle, files/archive, local and cast music, transcription setup, TTS/mic interactions.
4. Real cross-version update success plus intentionally bad installer/health rollback, registry and shortcut identity, enough-space/locked-file/cancel and interrupted-process recovery.
5. Native keyboard/Narrator, 100/150/200% DPI, narrow/large windows, light/dark/high-contrast; long-running resource measurements.
6. Exact release commit, final CI and downloaded installer/manifest checksum read-back. Source fixture passes alone do not close these gates.
