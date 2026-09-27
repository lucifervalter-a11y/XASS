# Security boundaries in 0.19

These are targeted mitigations, not a claim that every dependency or deployment has been audited.

## File response Range parsing

The current `fastapi==0.116.1` pin constrains Starlette to `<0.48.0`. This includes
[GHSA-7f5h-v6xp-fcq8](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8),
whose upstream fix starts at Starlette 0.49.1.

XASS now rejects malformed, duplicate, multiple and oversized HTTP `Range`
headers in ASGI middleware before authentication, route execution or the
vulnerable file-response parser. Only one `bytes=start-end`, `bytes=start-` or
`bytes=-suffix` range is supported, with at most 20 digits per number and 48
header bytes. Rejected ranges return HTTP 416 with `Cache-Control: no-store`.
Ordinary 206 audio seeking, resumed downloads, HEAD and If-Range remain supported.
Installer/media tickets and owner/agent authorization are still required.

This bounds the affected regex/merge workload; it does not patch Starlette
itself or address unrelated upstream advisories. A coordinated FastAPI/Starlette
upgrade still needs dependency-resolution and regression testing. Do not force
an incompatible Starlette version into the existing FastAPI constraint.

## Embedded image decoders

The `Pillow==11.3.0` pin also has newer upstream advisories, including
[PSD memory corruption](https://github.com/python-pillow/Pillow/security/advisories/GHSA-pwv6-vv43-88gr).
Embedded music artwork invokes only JPEG, PNG and GIF plugins via
`Image.open(..., formats=...)`; disallowed plugins are never used for sniffing or
decoding. Byte/pixel budgets and private sanitized JPEG output remain enforced.
This narrows that endpoint's attack surface, not every possible Pillow call.
A tested Pillow upgrade remains required; no package pins were changed here.

WebP embedded covers are temporarily excluded, independently of those
advisories. Pillow's WebP plugin creates a native animation decoder during
`Image.open`, and [libwebp allocates canvas buffers before returning dimensions](https://github.com/webmproject/libwebp/blob/v1.5.0/src/demux/anim_decode.c#L117-L127).
The application's later pixel limit cannot prevent that initial allocation.
Such covers use the existing no-artwork fallback; audio playback is unaffected.
Profile/project WebP uploads are still accepted because those routes store
bytes without invoking Pillow. This is not a complete bundled-C-library audit.

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
