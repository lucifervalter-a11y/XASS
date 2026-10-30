# XASS 0.21.3: reliability acceptance

Scope is limited to the native Windows installer, local library review/undo,
confirmed playback handoff, compatible audio preparation, and durable iOS downloads.
No production deployment or installed-profile mutation is implied by this document.

## Implemented boundaries

- Handoff protocol 2 prepares a silent target, obtains a confirmed source stop with
  its exact position, and requires rendered target audio confirmation. Timeout or
  an offline heartbeat is not proof of silence. The phone never takes over an
  ambiguous failed handoff automatically. Attempts and command receipts are fenced.
- Actual codecs are probed, not guessed from extensions. Conversion is bounded to
  one concurrent job per backend process, 120 seconds and 256 MiB per file. Original
  bytes are hash-checked and retained. FLAC preserves source rate/channel layout;
  the MP3 fallback is explicitly lossy at 256 kbit/s. The server needs ffmpeg and
  ffprobe on PATH. Preparation completes before the source stops playing.
- Windows library review reads a chosen local folder. It preserves version markers,
  shows provider provenance and evidence, and never silently applies a candidate.
  MusicBrainz, LRCLIB and Cover Art Archive require internet. Acoustic lookup needs
  an already installed fpcalc (`XASS_FPCALC`) and owner-configured AcoustID key
  (`XASS_ACOUSTID_KEY`). Whisper uses the already configured offline model; it is not
  downloaded automatically. The visible match score is an auditable rule, not a
  calibrated probability: 95 requires strong acoustic evidence, known duration
  within one second, matching versions and no unresolved metadata conflict;
  75/65 means review, 40 requires an additional manual selection. Provider score
  is shown separately. A recording fingerprint does not verify an album cover.
- SHA-256 byte duplicates are distinguished from name/version candidates. Duration
  alone does not establish recording identity. No duplicates are deleted. Entire
  originals are journaled before atomic replacement; undo refuses to overwrite
  later external changes. Multi-select undo runs newest-first and reports partial
  conflicts; it is not an all-or-nothing album transaction. Local fpcalc comparison
  does not need an AcoustID key; equal first-120-second fingerprints and duration
  are review candidates, never permission to delete. Backup storage is bounded
  to 2 GiB and fails closed.
- Pinned iOS downloads use a persistent background URLSession, bounded journal,
  Keychain-protected ticket/resume data, and restart-replayable media/index commit.
  A fresh expired ticket may require restarting. Force-quit and OS scheduling are
  not guarantees of automatic continuation; see `ios/README.md`.

## Evidence before release

- Local WinUI and bundled helper build succeeded. Synthetic staged-role smoke
  includes real WAV metadata parsing; no account, microphone, or private music.
- Real Inno failure injection interrupted installation after its first of two
  files. The production updater restored original hashes, retained synthetic
  config/key/archive files, and recorded the rejected release hash. The actual
  installed XASS registration and profile were not used.
- Library tests exercise byte-exact undo, unknown tags, changed files, corrupt
  backups, process-interruption journal recovery, artwork and source candidates.
- API tests exercise the three phases, exact stopped position, target timeout,
  stale receipts, concurrent polls and retry after command persistence.
- The intermediate revision `7d2fbf1` passed 1051 backend Python tests (two skipped),
  including real ffmpeg conversion, and the complete ephemeral Windows installer
  workflow (333 Windows Python tests, actual WinUI startup, upgrade, rollback,
  registration restoration, uninstall and synthetic data preservation).
- macOS XCTest/device build, the AAC/ALAC/Opus and MP3 compatibility matrix, and
  ephemeral full Windows installer smoke must be green for the exact release
  revision before publication. Intermediate build results are not release proof.

## Explicit acceptance limits

- Automated tests do not verify audible playback on the user's real iPhone/PC,
  Bluetooth hardware, VPN transitions, or iOS background OS scheduling.
- The desktop window-control tool failed its window-identity check for the isolated
  candidate. Interactive visual acceptance is not claimed from a successful build.
- The Windows catalog's direct play action refuses an active foreign playback
  lease; device-to-device transfer is driven by the iPhone route picker. Local
  unpaired/offline players are outside server-coordinated handoff.
- Exact duration and equal names are not evidence of acoustic identity. Acoustic
  provider availability and rare recording matches require review, not promises.
