# Native iOS navigation changes

The shipping RootView now uses NativeAppShell. Its destinations are SwiftUI views: music, devices, downloads, settings, server/access details, enrollment, the player and audio routing. Authentication uses the existing /api/native/enrollment/options and /verify APIs with a one-time pairing link; it does not instantiate WebContainer. The server still issues the existing authenticated session cookie. No authentication or Face ID checks have been weakened.

The old NativeShell and WebContainer source remain for compatibility while shared native components are still being extracted. They are not destinations of the shipping shell. This is not a claim that all website administration features have been ported: settings now expose the implemented native server/session information, device counts, storage, pairing and music-sharing controls. Other website-only administration features are not embedded in the app.

## Behavior changes

- Each tab owns its bottom safe-area inset, keeping the mini player above the tab bar.
- One presentation host serializes enrollment, player and route sheets instead of competing sheet presenters.
- The route picker uses the existing AVRoutePickerView for iPhone audio output and waits for Windows output discovery before enabling confirmation. Failed discovery is visible and retryable.
- selectPlaybackRoute preserves selectedDevice until transfer confirms its result. The old eager assignment could skip transfer's ownedSource pause/acknowledgment and prevent a reliable iPhone-to-PC handoff.
- Existing playback, downloads, Secure Enclave authorization, device commands, server endpoints and the Windows agent are otherwise unchanged.

## Verification

Added eight XCTest cases for modal transitions and route transfer ordering/failure/idle selection, plus three UI tests for native settings/enrollment, tab-bar accessibility and player-to-route presentation. Existing iOS workflow includes all files automatically through XcodeGen Sources/Tests/UITests directories.

Local validation performed before submission: Swift syntax parsing for the five new Swift files; a compiled Foundation-only state-machine check covering six transition scenarios. This is not an iOS SDK type-check or a device playback test. The full Xcode simulator build and tests must be checked in the branch workflow. Physical iPhone playback, Face ID, Bluetooth/AirPlay and a live Windows-agent handoff still require device validation. No signed IPA or deployment to the user's phone is implied.
