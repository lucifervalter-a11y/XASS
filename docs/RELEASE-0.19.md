# XASS 0.19 — native music polish and security

## Music and interface

- iOS keeps all five native SwiftUI tabs. The music player uses a large cover,
  a restrained gradient sampled from that actual cover, compact transport,
  queue, output picker and real embedded lyrics. Missing artwork/text is an
  honest empty state; no generated albums, catalog lyrics or external scraping.
- Albums and artists page through the library separately from the first page
  of songs. Partial collections have an explicit load-more affordance.
- ID3 USLT/SYLT, Vorbis and MP4 lyrics are read privately with finite byte,
  operation and time budgets. LRC timing supports repeated stamps and offsets.
- Music refresh errors identify the failing operation without printing cookies,
  query strings or response bodies. A recovered player refresh clears only its
  own previous error. Search punctuation is not treated as route traversal.
- Windows keeps the dark sidebar/card design and adds a local-file music page.
  File inspection/audio startup no longer block Tk; the queue remains stable,
  idle redraws are eliminated and progress does not regenerate large images.
  This local-file player is separate from the remotely controlled server agent.

## Security

- Browser Passkey action approvals are now single-use database records, bound
  to owner, exact action parameters, live credential and login generation.
  They are consumed atomically before side effects; failures require a new
  confirmation. Old reusable proofs are rejected. Native Secure Enclave
  approvals keep their existing separate contract.
- Workspace upload limits are enforced while reading the body, including
  chunked uploads and misleading Content-Length, before saving any asset.
- Restore invalidates transient Passkey action approvals. The web cache version
  changes with the new parameter-bound confirmation client.

## Verification and limitations

Regression tests cover native presentation/models, stale favorites/deletion,
refresh recovery, lyrics/auth boundaries, proof replay, two-engine atomic
consumption, session revocation and oversized uploads. Native iOS build/tests
and screenshots run in the macOS workflow. Windows UI uses a disconnected
preview; automated audio tests use mocks, not the owner's speakers.

An unsigned IPA still requires the owner's signing method. Simulator tests
cannot establish real-device Face ID, background audio or iPhone-to-PC handoff
reliability on the owner's hardware. A security review reduces identified risk;
it is not a guarantee that no vulnerabilities exist.

Full VK/Apple Music catalog export is not advertised as implemented in this
release. Apple Music playback/library integration requires authorized MusicKit
and does not provide freely downloadable copies of protected audio. The
existing Telegram-file/ZIP and owned-file import paths remain available.
