# Security boundaries in 0.21

These are targeted mitigations, not a claim that every dependency or deployment has been audited.

## Dependency baseline and file ranges

XASS 0.21 pins `fastapi==0.142.2`, `starlette==1.7.0` and
`Pillow==12.3.0`. The release dependency audit reports no known advisories for
the locked backend or Windows-agent requirements. That result is a snapshot of
the advisory database at release time, not a promise that future advisories do
not exist.

XASS still rejects malformed, duplicate, multiple and oversized HTTP `Range`
headers in ASGI middleware before authentication, route execution or the
file-response parser. Only one `bytes=start-end`, `bytes=start-` or
`bytes=-suffix` range is supported, with at most 20 digits per number and 48
header bytes. Rejected ranges return HTTP 416 with `Cache-Control: no-store`.
Ordinary 206 audio seeking, resumed downloads, HEAD and If-Range remain supported.
Installer/media tickets and owner/agent authorization are still required.

## Embedded image decoders

Embedded music artwork invokes only JPEG, PNG and GIF plugins via
`Image.open(..., formats=...)`; disallowed plugins are never used for sniffing or
decoding. Byte/pixel budgets and private sanitized JPEG output remain enforced.
This narrows that endpoint's attack surface, not every possible Pillow call.

WebP embedded covers are temporarily excluded, independently of those
advisories. Pillow's WebP plugin creates a native animation decoder during
`Image.open`, and [libwebp allocates canvas buffers before returning dimensions](https://github.com/webmproject/libwebp/blob/v1.5.0/src/demux/anim_decode.c#L117-L127).
The application's later pixel limit cannot prevent that initial allocation.
Such covers use the existing no-artwork fallback; audio playback is unaffected.
Profile/project WebP uploads are still accepted because those routes store
bytes without invoking Pillow. This is not a complete bundled-C-library audit.

## Agent transport and sensitive actions

Public agent origins must use HTTPS. Pairing codes, issued device keys, update
requests and server-backed music are rejected over remote plain HTTP; loopback
is reserved for local development. HTTPS probing never silently downgrades to
HTTP and keeps the original hostname for TLS/SNI validation.

Screenshots, clipboard reads, file downloads and file uploads require a fresh
owner action proof bound to the device, command and normalized parameters. File
upload approval is checked before the request body is read. Telegram API errors
are sanitized inside the client so a bot token is not copied into tracebacks,
diagnostics, database records or backups.

## Browser approvals and uploads

Passkey approvals are single-use DB records bound to the owner, live credential,
session generation, purpose and reviewed parameters. Old reusable approvals are
rejected; repeat Face ID after updating the web client. Restoring a server backup
invalidates stored approvals. Native device approval semantics remain unchanged.

Workspace uploads enforce their configured size while receiving the stream,
including requests without an accurate Content-Length. Oversized bodies return
413 before an asset is stored or a PC command is queued.
Profile avatar and project-cover uploads use the same bounded receiver with
their existing 8 MiB and 10 MiB limits, before any file/profile mutation.

## Unmerged VK import experiments

Read-only review covered these exact remote snapshots; neither was merged:

- `origin/feat/vk-music-import` at `336017ca27e307f08e7f39d9335c07efc7b68305`.
- `origin/fix/music-agent-vk-apple` at `9a1e313d650d3e1a5e0b54911d27e5861a27000a`.

Both are unsuitable for direct merge. The first replaces `app/music_api.py`
with an empty file; the second replaces it with the eight-byte placeholder
`see-file`. Neither provides the router required by the application, so the
existing library API and backend startup would break.

The latter branch also validates download hosts using substring checks on
`urlparse(...).netloc` and follows redirects without revalidating destinations.
An apparent allowed host in URL user information or an unrelated hostname can
pass that test: it is not a safe boundary against server-side request forgery.
No such requests were executed during review. Raw provider access tokens in
query parameters and guidance to obtain another application's tokens are also
not an acceptable supported authentication contract; no credentials were used.

The experiments do not establish complete ordered album/playlist imports or
incremental provider-ID synchronization, and contain no Apple Music export
implementation. Download-time content-hash deduplication alone does not supply
those features. A future provider integration needs supported authorization,
strict download destination/redirect controls, intact API integration and real
end-to-end regression tests. Full VK/Apple library export is not claimed here.
