# Confirmed music handoff (protocol 2)

The iOS client requests `protocol: 2` on `/api/mini/music/transfers`. The server
returns an opaque transfer ID, monotonic attempt `revision`, and `phase`:

1. `preparing`: target silently opens its audio output, checks the decoder and
   prepares media. A PC reports `prepared`, the exact handoff ID and revision in
   its heartbeat. An iPhone prerolls a separate paused AVPlayer and sends the
   same revision to `target-ack` with `state: prepared`.
2. `waiting`: only now is the source asked to stop. The source confirms its
   exact stopped position and matching revision. Source iPhones receive this
   request both in session polls and in `transfer_requested` report responses.
3. `starting`: the target receives the confirmed position. A PC must report
   matching `playing` and `audio_started` (PCM frames submitted to its output),
   not merely acknowledge the command. An iPhone waits for AVPlayer's actual
   playing state before sending `target-ack` with `state: playing`.
4. `ready`: the new lease is active. This confirms the OS playback pipeline,
   not that physical speakers are switched on or audibly unmuted.

Receipts from another attempt, premature receipts and late receipts after
cancellation are rejected. Phase updates use database compare-and-set, and
commands have durable per-attempt idempotency keys. Retrying after a worker
restart reuses an already-created command. Source and target confirmation
timeouts do not trigger automatic phone playback. A disconnected PC can still
play buffered audio, so a missing heartbeat is never treated as proof of silence.

## Compatibility

Older clients retain the `status: waiting|ready|failed` response and source ACK
shape, but only protocol 2 offers silent target preparation. Old agents are
rejected as protocol-2 targets before the source is paused. Legacy `loading`
command results no longer count as successful playback. The new iOS client
rejects a server which does not echo protocol 2. Update backend, agent and iOS
together; do not advertise protocol-2 readiness on an old agent.
The legacy session `force` field is still accepted for decoding but cannot
override an audible foreign lease. Such requests receive `transfer_required`;
only a confirmed quiet source or the handoff protocol permits ownership change.

The Windows catalog play shortcut remains paired-agent authenticated. It records
its active PC lease and refuses to start over another audible device; use the
device picker to perform a confirmed handoff. The offline local-file Windows
player remains independent of the server; its local/cast audio lock prevents
two XASS engines from owning the same PC output.

## Codec preparation and deployment

Install `ffmpeg` **and** `ffprobe` on the backend PATH. Debian/Ubuntu installers
and migration/restore scripts now install the `ffmpeg` package containing both.
For other distributions install the package provided by that distribution and
verify both executables before enabling protocol-2 PC playback. Existing hosts
need the package added during deployment; Python dependencies alone do not
provide these executables.

Preparation checks the actual codec with ffprobe and verifies the original
catalog SHA-256. Unsupported audio becomes a cached FLAC (source channel count
and sample rate preserved) or 256-kbit MP3 when required by target capabilities.
The original file is never modified. Converting already-lossy audio to FLAC
does not restore lost information. Converted media uses the same exact paired
origin/route and a signed credential-bound ticket identifying the variant.

Only catalog-owned regular files are accepted; no URL or arbitrary input path
is passed to the tools. Input demuxers and protocols are allowlisted; ffmpeg has
one job per worker, a 120-second deadline and 256-MiB input/output ceilings.
The cache is capped at 1 GiB, probe output is capped at 64 KiB, tool stderr is
discarded and orphaned partial conversions are cleaned up. Preparation failures
remain explicit and do not start or stop the source optimistically.

## Verification

`test_music_handoff_reliable` exercises the authenticated API and SQLite: phase
ordering, wrong revisions, duplicate acknowledgments, source/start timeouts,
cancellation, concurrent polls and restart recovery. `test_pc_music` verifies
silent preparation, attempt fencing and expiry with a fake audio backend.
`test_music_compatibility` includes a real ffmpeg conversion/cache test when the
executables are installed (Linux CI installs them). iOS device audio routing,
lock-screen background behavior and physical speaker latency still require
device validation; API tests cannot establish those properties.
