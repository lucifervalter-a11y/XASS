import Foundation
import Combine

// Thin, read-mostly adapter: maps existing NativeStore state into the new
// device-picker / remote protocols. It only READS published NativeStore state
// and calls EXISTING public NativeStore methods (pickRoute, cancelTransfer,
// toggle, step, seek, setVolume, refresh, refreshSession). It adds no
// requests, no session/transfer/LAN logic and changes nothing in NativeStore,
// OwnerAPI, AudioController or SecureMediaLoader.
//
// NOT WIRED into production UI yet. Integration seam for the network branch
// (fix/ios-music-remote-transcribe), see TODO(network) notes below:
//  - Present `DevicePickerSheet(model: adapter, remote: adapter)` for the
//    `.route` case in NativeShell instead of `NativeRoutePicker` once UITests
//    move from `route-local` / «Куда играть» to `devicePicker-*` / «Где слушать».
//  - `NativeStore.pickRoute` keeps `showRoutePicker == true` while a handoff
//    runs, which is exactly what keeps this single sheet open during progress.

@MainActor final class NativeStoreDeviceAdapter: DevicePickerModel, RemotePlaybackControlling {
    let store: NativeStore
    private struct Failure: Equatable { let deviceID: String; let name: String; let message: String }
    @Published private var pending: String? = nil
    @Published private var failure: Failure? = nil
    @Published private var offlineTarget: String? = nil
    private var subscriptions = Set<AnyCancellable>()

    init(store: NativeStore) {
        self.store = store
        // Forward changes; the playback clock is separate so ticks stay cheap.
        store.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &subscriptions)
        store.playback.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }.store(in: &subscriptions)
    }

    // MARK: DevicePickerModel

    var devices: [PlaybackDevice] {
        var phone = PlaybackDevice.thisPhone()
        if store.otherLocal { phone.detail = "Сейчас играет другой iPhone" }
        var result = [phone]
        for device in store.devices {
            let id = "agent:" + device.name
            let active = id == store.selectedDevice
            let player = store.players.first(where: { $0.id == device.name })
            let online = device.online && (player?.online ?? true)
            let output: String? = active ? store.outputs.first(where: { $0.id == store.outputID })?.name : nil
            result.append(PlaybackDevice(id: id, name: device.name, kind: .computer, isOnline: online, detail: output, accessibilityKey: "device-\(device.id)"))
        }
        // Players reported without a paired device record (e.g. another client).
        let known = Set(store.devices.map(\.name))
        for (index, player) in store.players.enumerated() where !known.contains(player.id) {
            result.append(PlaybackDevice(id: "agent:" + player.id, name: player.id, kind: .otherPlayer, isOnline: player.online && player.available, detail: nil, accessibilityKey: "player-\(index)"))
        }
        return result
    }

    var activeDeviceID: String { store.selectedDevice }
    var pendingDeviceID: String? { pending ?? failure?.deviceID ?? offlineTarget }
    var isPlaying: Bool { store.playing }

    var connectionState: DeviceConnectionState {
        let list = devices
        func name(_ id: String?) -> String? { list.first(where: { $0.id == id })?.name }
        if let status = store.transferStatus {
            let progress = store.transferProgress > 0 ? min(1, store.transferProgress) : nil
            return .connecting(deviceName: name(pending) ?? store.deviceLabel, progress: progress, detail: status)
        }
        if let failure { return .switchFailed(deviceName: failure.name, message: failure.message) }
        if let target = offlineTarget, let targetName = name(target) { return .deviceOffline(deviceName: targetName) }
        if store.selectedDevice != PlaybackDevice.thisPhoneID {
            let active = list.first(where: { $0.id == store.selectedDevice })
            if active?.isOnline == false { return .deviceOffline(deviceName: active?.name ?? store.deviceLabel) }
            // TODO(network): RemotePlayer.error / stale heartbeat should map to .deviceOffline
            // or a dedicated state once the network branch defines reliable semantics.
            return .playingRemotely(deviceName: active?.name ?? store.deviceLabel)
        }
        if list.count == 1 { return .noDevices }
        return .idle
    }

    func select(_ device: PlaybackDevice) {
        guard !store.busy else { return }
        failure = nil; offlineTarget = nil
        guard device.isOnline else { offlineTarget = device.id; return }
        guard device.id != store.selectedDevice else { return }
        pending = device.id
        // TODO(network): Windows audio output choice (store.loadOutputs + pickRoute(output:))
        // still lives in NativeRoutePicker; nil keeps the agent's current output.
        Task { [weak self] in
            guard let self else { return }
            do {
                try await self.store.pickRoute(device: device.id, output: nil)
                self.pending = nil
            } catch is CancellationError {
                self.pending = nil
            } catch {
                self.pending = nil
                self.failure = Failure(deviceID: device.id, name: device.name, message: error.localizedDescription)
            }
        }
    }

    func retry() {
        if let failure, let device = devices.first(where: { $0.id == failure.deviceID }) {
            self.failure = nil
            select(device)
            return
        }
        let target = offlineTarget
        Task { [weak self] in
            guard let self else { return }
            if let target {
                await self.store.refresh()
                if let device = self.devices.first(where: { $0.id == target }), device.isOnline {
                    self.offlineTarget = nil
                    self.select(device)
                }
            } else {
                try? await self.store.refreshSession()
            }
        }
    }

    func cancelSwitch() {
        store.cancelTransfer()
        pending = nil
    }

    func refresh() async { await store.refresh() }

    // MARK: RemotePlaybackControlling

    var track: RemoteTrackInfo? {
        store.currentTrack.map { RemoteTrackInfo(id: $0.id, title: $0.title, artist: $0.artist) }
    }

    var duration: Double {
        let clock = store.playback.duration
        return clock > 0 ? clock : (store.currentTrack?.duration ?? 0)
    }

    /// Server samples arrive on polls; project forward while playing, bounded
    /// by the store's own projection limit.
    var position: Double {
        let clock = store.playback
        guard store.playing else { return clock.position }
        let elapsed = min(max(0, Date().timeIntervalSince(clock.sampleAt)), max(0, clock.projectionLimit))
        let projected = clock.position + elapsed
        return duration > 0 ? min(projected, duration) : projected
    }

    var volume: Double { store.volume }
    var deviceName: String { store.deviceLabel }
    var canGoPrevious: Bool { true }
    var controlsEnabled: Bool {
        if store.busy || store.currentID == nil { return false }
        switch connectionState {
        case .idle, .playingRemotely: return true
        default: return false
        }
    }

    func playPause() { store.run { try await self.store.toggle() } }
    func next() { store.run { try await self.store.step(1) } }
    func previous() { store.run { try await self.store.step(-1) } }
    func seek(to seconds: Double) { store.run { try await self.store.seek(seconds) } }
    func setVolume(_ value: Double) { store.run { try await self.store.setVolume(value) } }
}
