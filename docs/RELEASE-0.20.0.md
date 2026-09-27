# XASS 0.20.0 — system volume and a living music library

## iPhone

- Real visible `MPVolumeView` for local system volume; hardware buttons and output routes are owned by iOS, not stale server values. Fresh local playback starts with unity application gain. Remote PC gain is separate; an explicit control can clear an attenuation requested by another client.
- Lyrics follow actual line timestamps, highlight and slightly lift the active line, and seek on tap. Manual scrolling pauses follow mode. VoiceOver, Reduce Motion, small screens, duplicate timestamps and stale remote playback samples are handled without invented word timing.
- Automatic metadata lookup never blocks Play. Track information exposes provenance, ambiguous suggestions, retry and reversible original labels.
- Optional **on-device-only** transcription of downloaded audio, using 45-second clips and real speech timestamps, up to 15 minutes. No microphone, audio upload, cloud fallback or hidden paid API. Permission and a supported on-device language model are required; results are a reviewable draft, not authoritative song lyrics.

## Server / safety

- LRCLIB, MusicBrainz and Cover Art Archive, with provider rate limits, Retry-After, bounded streamed responses/images, TLS, explicit host/path redirect validation and negative caching.
- Exact normalized title/artist and duration required for automatic matches. Fuzzy spelling and ambiguous results require confirmation, bound to a result token. Known excerpts never inherit full-song timed lyrics, even after their labels are cleaned.
- New additive `music_enrichment` table stores source information, original labels, cached lyrics/thumbnails and owner-reviewed transcripts. Source audio and embedded tags are never rewritten. Private owner-only endpoints; no lyrics/artwork enrichment on public routes.
- Catalog calls release DB transactions. Optimistic metadata checks and revision guards prevent a delayed lookup from undoing manual edits or a restore. Source URLs are allowlisted on the phone. No raw lyrics, filenames or recognition results are written to diagnostic events.

## Verification boundary

System output volume and Speech language availability must be checked on a physical iPhone: Simulator cannot prove hardware volume behavior or offline model availability. Song catalog coverage and singing transcription accuracy vary. This release does not add Shazam audio fingerprint matching, vocal separation, licensed Apple Music lyrics or guaranteed word-by-word karaoke. Windows binaries are unchanged.

Implementation references: [Apple system volume](https://developer.apple.com/documentation/mediaplayer/mpvolumeview), [Apple time-synced lyrics](https://support.apple.com/en-euro/guide/iphone/iphb9bf483aa/ios), [LRCLIB API](https://lrclib.net/docs), [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API), [Cover Art Archive API](https://musicbrainz.org/doc/Cover_Art_Archive/API), [on-device Speech](https://developer.apple.com/documentation/speech/sfspeechrecognitionrequest/requiresondevicerecognition).
