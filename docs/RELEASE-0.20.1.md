# XASS 0.20.1 — visible, bounded music imports

## iPhone

- An explicit native document picker opens files in place. Security-scoped access is acquired during the selection callback, and coordinated staging/reading happens off the main thread, one file at a time.
- A shared import screen shows server limits before choosing files, preparation/upload/server-processing stages, cancellation and a separate result for every selected file. Local unreadable files do not silently abort the rest of a batch. Authentication/network failures leave the remaining files clearly marked as not attempted.
- Completed server receipts survive view/task cancellation. A ZIP that is still processing is never described as rolled back. Partially accepted archives report skipped files and failures; an archive with no usable songs is not a success.
- Diagnostic events distinguish requesting the Files picker, receiving its selection, staging and upload. Paths, filenames, payloads and credentials are excluded.

## Server

- Separate `MUSIC_MAX_ARCHIVE_UPLOAD_BYTES` (default 512 MiB, hard maximum 1 GiB). Individual audio-file limits are unchanged. Libraries advertise both limits.
- Opt-in asynchronous ZIP finalization with owner-bound status polling, immutable completion receipts and restart-safe retry of intact uploaded archives. Older clients retain synchronous finalization.
- At most two queued ZIP jobs and one extractor. Existing limits remain: 200 audio files, 500 total ZIP entries, 512 MiB expanded data, compression-ratio/path/symlink/encryption guards and disk-space reserves. Upload blocks remain 512 KiB; JSON bodies are bounded before decoding.
- Cancellation does not unlink an archive under an active worker. No audio originals are removed by upload cleanup.

## Verification boundary

The device's Files extension and iCloud/provider materialization require a physical-iPhone check. Automated tests cannot prove that every external Files provider responds to Open. Catalog coverage and singing-transcription accuracy remain distinct from importing a file. Windows binaries are unchanged by this release.
