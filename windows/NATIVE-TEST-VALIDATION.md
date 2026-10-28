# Native test installer verification

This test branch builds an unsigned per-user WinUI 3 installer, separately from
stable XASS. The downloadable installer includes its private Python 3.12 runtime,
CPU faster-whisper dependencies, .NET and Windows App SDK. An existing local
Whisper model is still required for speech recognition; the installer does not
download a model. Windows voice availability depends on installed system voices.

## Automated coverage

- Existing Python application/backend regressions, with their complete pinned
  dependencies; native bridge, desktop protocol, music ownership and voice tests.
- Real .NET voice child-process lifecycle tests using a synthetic Python peer,
  without audio devices. Appearance persistence, corruption recovery and 12,288
  accent/text contrast cases are also checked.
- WinUI publish, frozen-runtime health, complete companion inventory, x64 PE
  validation, per-file SHA-256 hashes and personal-configuration exclusions.
- On an ephemeral Windows Actions runner: fresh install, same-version reinstall,
  installed helper roles, WinUI process startup, uninstall and preserved data markers.
- Read-only Actions workflow. It uploads an artifact but does not publish a
  release, update `main`, or deploy a server.

Local Linux checks do not establish the Windows build or installer outcome.
Use the workflow for the exact commit as the authoritative Windows result.
An eight-second live process check establishes startup survival only, not visual
or interactive acceptance.

## Interactive Windows acceptance still required

1. Launch from Start menu; reopen an existing instance; close to tray and restore.
2. Pair/import a profile, repeat/cancel, verify settings persist and secrets stay
   out of visible diagnostics. Verify agent restart and connection recovery.
3. Browse files, archive and diagnostics; test archive copy/cleanup confirmation
   and interruptions using disposable fixtures.
4. Play local and server music, seek/volume, missing files, queue navigation,
   cover/lyrics, ownership handoff and quick repeated actions.
5. Apply/reset theme and accent, restart, enable Windows high contrast and check
   narrow-window keyboard navigation.
6. With a local speech model, exercise push-to-record and explicit background
   microphone enable/disable, local female voice, speech cancellation, stale
   recognition, minimize, close-to-tray and final quit. Ambient speech must never
   execute an action; execution requires its explicit button.

The native updater accepts only native-test installer manifests and verified
SHA-256 downloads. Explicitly opted-in automatic updates, verified backup and
health-checked rollback are implemented with failure-injection tests. Real
cross-version Windows update/rollback acceptance remains required. Discord
automatic voice-channel joining remains unavailable without its OAuth/RPC integration.
