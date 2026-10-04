# Native music implementation and acceptance

## Implemented visible flows

- **M01–M04:** Native multi-file picker (MP3/WAV/FLAC/OGG), first-track playback without pairing, shared legacy `local-music.json` history capped at 40, selectable missing-file feedback, previous/next wrap with invalid/missing tracks skipped, pause/resume/replay/stop/seek/volume.
- **M05–M10:** Separate server-library requests retain the existing paired-PC/default-output API; Unicode query bound 120, page size 100, latest pending search wins, automatic page-open load, manual refresh, 30-second failure retry and 120-second refresh. Duration/favorite/current-track presentation, queued-vs-playing distinction, authoritative cast-control acknowledgment, elapsed/duration readout and dirty seek/volume preservation.
- **M11–M13:** Verified and re-sanitized JPEG artwork (maximum 512 KiB), SHA-256 checked again by native UI, no-art fallback, bounded plain/LRC lyrics with seek-resynchronized highlight and scroll, sanitized actionable errors and Reset on errored cast.
- **M14–M16:** Half-second native polling continues while window is inactive/hidden; cast reveal selects Music, restores and flashes. Enter/Space row activation, transport Space, immediate keyboard slider commits. Local controls disable while cast owns audio. Cast reserves priority, waits for verified local silence, and only then acquires the audio lock; a slow/cancelled decoder cannot begin late playback.

## Process and security contract

`MusicClient` directly owns a dedicated `XASS.NativeHelper.exe --role desktop-music --source <installed pc_client> --data <data root>` process. Development uses trusted Python with `-I -B` and the same script. It is not descended from the one-request adapter; successful request completion leaves local audio alive. Final UI quit or pipe EOF closes the owner. The server catalog runs through the separate bounded one-shot adapter, so a network lookup cannot block local transport.

`desktop_music_service.py` accepts version-1 bounded JSONL over inherited pipes only. It opens no HTTP listener, grants no arbitrary URL/shell action, and projects no credentials/media URLs. Actions reject unknown/missing fields, invalid identifiers and nonfinite values. Selected paths must be absolute ordinary files with no symlink/reparse component; the existing player validates suffix, signature, nonempty data and maximum 256 MiB. Original audio is never uploaded/copied/deleted. Queue replacement is atomic. Existing agent loopback commands preserve their existing token, 127.0.0.1, redirect/proxy and request-size protections.

`native_music_ownership.py` uses OS-held audio, cast-priority and native-host lock files under the explicit data root. Process crashes release them automatically; no stale PID is treated as an audio lease. Updated agent bridges install an optional player gate before first playback. A non-native caller without that gate keeps its previous MusicPlayer behavior. An existing older/unpatched agent binary is outside the no-overlap guarantee; the combined installer must update both helper and agent modules together.

One local decoder worker and one replaceable pending request bound rapid next/previous operations. Cancellation is checked under the player condition before generation registration and again before playback. The cast may wait up to three seconds for silence; on failure it does not start overlapping audio. Native process request timeout is 25 seconds and stops only that UI's local player to recover the private JSONL protocol.

## Verified in the isolated Linux workcopy

- `python -m unittest discover -s windows/tests -p test_native_music.py`: 20 tests passed, including offline queue persistence, wrap/skip, malformed/signature/empty/oversize/symlink paths, paused seek/replay, volume boundaries, cast arbitration during playback and decode, rapid-next replacement, error/reset acknowledgment, cover mismatch, lyric parsing, process EOF and crash lock release.
- `python -m unittest discover -s tests -p test_pc_music.py`: 41 tests, passed with 4 Windows/audio-dependent skips.
- `python -m unittest discover -s tests -p test_music_bridge.py`: 7 tests passed.
- `python -m unittest discover -s tests -p test_desktop_music.py`: 20 tests, passed with 11 Tk/display-dependent skips.

For isolated test runs set `XASS_DATA_ROOT` to a disposable absolute directory before imports. No live PC, microphone, real audio device, account, or server was used.

## Required Windows acceptance, not claimed by fixture tests

Native C#/XAML compilation; packaged helper and audio dependency inclusion; actual MP3/WAV/FLAC/OGG output; file picker and EOF/quit behavior; Windows msvcrt cross-process locking; close-to-tray/new-cast reveal; Narrator and keyboard focus; narrow-window and 100/150/200% DPI layouts; real server pagination/credential/network errors; prolonged CPU/memory and audio latency. Static source contracts are not evidence that these interactive gates passed.
