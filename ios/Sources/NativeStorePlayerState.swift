import Combine
import SwiftUI
import UIKit

/// Thin adapter from the existing NativeStore to `PlayerStateProviding`.
/// It only reads published state and forwards taps to NativeStore's existing
/// public commands (`toggle`, `step`, `seek`, `setVolume`). It owns no
/// playback, session, transfer, LAN or network logic.
@MainActor final class NativeStorePlayerState: PlayerStateProviding {
    private let store: NativeStore
    @Published private(set) var track: PlayerTrack?
    @Published private(set) var isPlaying = false
    @Published private(set) var position: TimeInterval = 0
    @Published private(set) var duration: TimeInterval = 0
    @Published private(set) var volume: Double = 1
    @Published private(set) var isBusy = false
    // TODO(player-wiring): the real lyrics still load in NativeLyricsContent
    // (passed to Now Playing as a slot). Map NativeLyrics -> TimedLyrics here
    // once lyrics live on NativeStore, then drop the slot.
    let lyrics: TimedLyrics? = nil
    let currentLyricIndex: Int? = nil
    private var subscriptions = Set<AnyCancellable>()
    private var artworkTask: Task<Void, Never>?
    private var artworkKey = ""

    init(store: NativeStore) {
        self.store = store
        // objectWillChange fires before the new value is stored; read it on the next main-queue turn.
        store.objectWillChange.receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.syncStore() }.store(in: &subscriptions)
        store.playback.objectWillChange.receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.syncClock() }.store(in: &subscriptions)
        syncStore(); syncClock()
    }

    private func syncStore() {
        if isPlaying != store.playing { isPlaying = store.playing }
        if isBusy != store.busy { isBusy = store.busy }
        let level = store.volume.isFinite ? min(1, max(0, store.volume / 100)) : 1
        if abs(volume - level) > 0.001 { volume = level }
        guard let current = store.currentTrack else {
            if track != nil { track = nil }
            artworkTask?.cancel(); artworkKey = ""
            return
        }
        let id = String(current.id)
        // Keep the previous image (even of the previous song) until the new one is
        // decoded: the UI never flashes a placeholder between two covers.
        let next = PlayerTrack(id: id, title: current.title, artist: current.artist.isEmpty ? "Моя коллекция" : current.artist,
                               artworkImage: track?.artworkImage)
        if next != track { track = next }
        let key = "\(current.id)-\(store.artworkRevision)"
        if key != artworkKey { artworkKey = key; loadArtwork(trackID: current.id, key: key) }
        if duration <= 0, current.duration > 0 { duration = current.duration }
    }

    private func syncClock() {
        let clock = store.playback
        let value = clock.position.isFinite ? max(0, clock.position) : 0
        if abs(position - value) > 0.01 { position = value }
        let total = clock.duration > 0 ? clock.duration : (store.currentTrack?.duration ?? 0)
        if abs(duration - total) > 0.01 { duration = total }
    }

    private func loadArtwork(trackID: Int, key: String) {
        artworkTask?.cancel()
        artworkTask = Task { [weak self] in
            guard let self = self else { return }
            let loaded = await self.store.artwork(trackID)
            // Decode off the main thread so the first frame of the new cover is cheap.
            let image = await Task.detached(priority: .userInitiated) { loaded?.preparingForDisplay() ?? loaded }.value
            guard !Task.isCancelled, self.artworkKey == key, var updated = self.track, updated.id == String(trackID) else { return }
            updated.artworkImage = image
            if updated != self.track { self.track = updated }
        }
    }

    func play() {
        guard !store.playing else { return }
        store.run { [store] in try await store.toggle() }
    }
    func pause() {
        guard store.playing else { return }
        store.run { [store] in try await store.toggle() }
    }
    func next() { store.run { [store] in try await store.step(1) } }
    func previous() { store.run { [store] in try await store.step(-1) } }
    func seek(to position: TimeInterval) {
        guard position.isFinite else { return }
        let value = max(0, position)
        store.run { [store] in try await store.seek(value) }
    }
    func setVolume(_ value: Double) {
        // iOS owns the local hardware volume (MPVolumeView). Only remote players take a level.
        guard value.isFinite, NativeVolumeTarget(device: store.selectedDevice, otherLocal: store.otherLocal) != .system else { return }
        let level = min(1, max(0, value)) * 100
        store.run { [store] in try await store.setVolume(level) }
    }
}
