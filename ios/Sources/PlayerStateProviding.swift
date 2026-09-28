import Foundation

/// Read-only player state + commands for UI branches (ux/apple-music-player,
/// ux/device-picker-remote). Views should depend on this, not on NativeStore
/// internals. Network, handoff, takeover and LAN logic stay in NativeStore.
///
/// Observing: `NativeStore` publishes track/state/device changes; the fast
/// playback clock is `store.playback` (NativePlaybackClock) and lyrics are
/// `store.lyrics` (TimedLyricsStore, publishes `current` and
/// `currentLyricIndex`). Observe those two separately so 5 Hz ticks do not
/// re-render the whole library.
@MainActor protocol PlayerStateProviding: ObservableObject {
    var currentTrack: LibraryTrack? { get }
    var isPlaying: Bool { get }
    /// Seconds; for a remote PC this is the last heartbeat sample (see `store.playback` for projection).
    var position: Double { get }
    var duration: Double { get }
    /// 0...100. Local: XASS gain (system volume via MPVolumeView). PC: agent volume.
    var volume: Double { get }
    /// Audible device: "local", "agent:<PC name>" or "other_local".
    var playingDevice: String { get }
    var playingDeviceName: String { get }
    /// True when a PC owns playback and transport controls act as a remote.
    var isRemotePC: Bool { get }
    var timedLyrics: TimedLyrics? { get }
    var currentLyricIndex: Int? { get }
    func playPause() async throws
    func next() async throws
    func previous() async throws
    func seek(to seconds: Double) async throws
    func setVolume(_ value: Double) async throws
}

extension NativeStore: PlayerStateProviding {
    var isPlaying: Bool { playing }
    var isRemotePC: Bool { pcRemoteSource != nil }
    var timedLyrics: TimedLyrics? { lyrics.current?.trackID == currentID ? lyrics.current : nil }
    var currentLyricIndex: Int? { timedLyrics == nil ? nil : lyrics.currentLyricIndex }
    func playPause() async throws { try await toggle() }
    func next() async throws { try await step(1) }
    func previous() async throws { try await step(-1) }
    func seek(to seconds: Double) async throws { try await seek(seconds) }
}
