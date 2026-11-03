# Windows music UI visual refresh — Issue #33

Baseline: clean public `main` checkout at `28a1bd0`. This design update is an original XASS interface inspired at a high level by contemporary editorial music players; it does not reproduce another product's layout or use its artwork.

## What changed

- A more expressive XASS music masthead and a calm, charcoal surface, with cover art contributing only a restrained tint to the backdrop.
- A larger, editorially weighted current-track title and artist beside prominent album art.
- Dedicated rounded surfaces for the now-playing progress/elapsed-duration/transport controls and for the existing queue/library/history/lyrics navigation.
- Responsive layout continues to switch between a side-by-side player/library and a compact stacked view.
- Existing file picker, local/server source selection, search/paging, play/pause/skip/stop, seek, volume, history, lyrics, Discord control, keyboard shortcuts and automation names remain in place.
- Windows high-contrast mode receives system background/text/highlight colors instead of the decorative dark surfaces.

## Validation

Static regression tests cover the new design hooks and continued presence of key player/accessibility identifiers. Tests cannot render the WinUI page. A real Windows + Windows App SDK build and visual/keyboard/contrast review are still required; this Linux environment has no `dotnet` executable.
