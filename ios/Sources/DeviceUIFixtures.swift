import Foundation
import SwiftUI

// UI fixtures for previews and `--native-ui-fixture` screenshots.
// No network, transfer, LAN, audio or authentication: everything below is
// local state with short simulated delays.

enum DeviceUIFixtureScenario: String {
    /// This iPhone plays; «Студия» online, «Ноутбук» offline.
    case normal
    /// A switch to «Студия» is in progress (static, for screenshots).
    case connecting
    /// The last switch to «Студия» failed; retry succeeds.
    case failed
    /// The user tapped offline «Ноутбук».
    case offline
    /// Only this iPhone is known.
    case empty
    /// «Студия» is the active device and plays.
    case remote

    /// `--native-ui-device-state <name>`; defaults to `normal`.
    static var current: DeviceUIFixtureScenario {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "--native-ui-device-state"), args.indices.contains(index + 1) else { return .normal }
        return DeviceUIFixtureScenario(rawValue: args[index + 1]) ?? .normal
    }
}

@MainActor final class FixtureDevicePickerModel: DevicePickerModel {
    @Published private(set) var devices: [PlaybackDevice]
    @Published private(set) var activeDeviceID: String
    @Published private(set) var pendingDeviceID: String? = nil
    @Published private(set) var connectionState: DeviceConnectionState = .idle
    @Published private(set) var isPlaying = true
    /// Makes the next switch fail once (used by previews/tests).
    var failNextSwitch = false
    /// Remote fixture kept in sync with the active device.
    weak var remote: FixtureRemotePlayback?
    private var switchTask: Task<Void, Never>?
    private let stepDelay: Duration

    static let studio = PlaybackDevice(id: "agent:Студия", name: "Студия", kind: .computer, isOnline: true, detail: "Динамики (Realtek Audio)", accessibilityKey: "device-1")
    static let laptop = PlaybackDevice(id: "agent:Ноутбук", name: "Ноутбук", kind: .computer, isOnline: false, detail: nil, accessibilityKey: "device-2")

    init(scenario: DeviceUIFixtureScenario = .normal, stepDelay: Duration = .milliseconds(350)) {
        self.stepDelay = stepDelay
        devices = scenario == .empty ? [.thisPhone()] : [.thisPhone(), Self.studio, Self.laptop]
        activeDeviceID = scenario == .remote ? Self.studio.id : PlaybackDevice.thisPhoneID
        switch scenario {
        case .normal, .remote, .empty:
            connectionState = restingState
        case .connecting:
            pendingDeviceID = Self.studio.id
            connectionState = .connecting(deviceName: Self.studio.name, progress: 0.45, detail: "Жду подтверждения от «Студия»…")
        case .failed:
            pendingDeviceID = Self.studio.id
            connectionState = .switchFailed(deviceName: Self.studio.name, message: "«Студия» не подтвердил запуск за 10 секунд. Музыка продолжает играть на этом iPhone.")
        case .offline:
            pendingDeviceID = Self.laptop.id
            connectionState = .deviceOffline(deviceName: Self.laptop.name)
        }
    }

    private var restingState: DeviceConnectionState {
        if !devices.contains(where: { $0.kind != .thisPhone }) { return .noDevices }
        if let active = devices.first(where: { $0.id == activeDeviceID }), active.kind != .thisPhone {
            return active.isOnline ? .playingRemotely(deviceName: active.name) : .deviceOffline(deviceName: active.name)
        }
        return .idle
    }

    func select(_ device: PlaybackDevice) {
        if device.id == activeDeviceID && !connectionState.isError { return }
        switchTask?.cancel()
        pendingDeviceID = device.id
        guard device.isOnline else {
            // Fail fast: an offline target never gets a silent spinner.
            connectionState = .deviceOffline(deviceName: device.name)
            return
        }
        let fails = failNextSwitch
        failNextSwitch = false
        connectionState = .connecting(deviceName: device.name, progress: 0, detail: "Жду подтверждения от «\(device.name)»…")
        switchTask = Task { [weak self, stepDelay] in
            for step in 1...4 {
                try? await Task.sleep(for: stepDelay)
                guard !Task.isCancelled, let self else { return }
                if step < 4 {
                    self.connectionState = .connecting(deviceName: device.name, progress: Double(step) / 4, detail: step < 2 ? "Останавливаю музыку на текущем устройстве…" : "Запускаю на «\(device.name)»…")
                } else if fails {
                    self.connectionState = .switchFailed(deviceName: device.name, message: "«\(device.name)» не ответил вовремя. Музыка продолжает играть на прежнем устройстве.")
                } else {
                    self.activeDeviceID = device.id
                    self.pendingDeviceID = nil
                    self.connectionState = self.restingState
                    self.remote?.adopt(deviceName: device.kind == .thisPhone ? nil : device.name)
                }
            }
        }
    }

    func retry() {
        guard let id = pendingDeviceID, let device = devices.first(where: { $0.id == id }) else {
            connectionState = restingState
            return
        }
        if device.isOnline { select(device); return }
        // Re-check an offline device: brief check, then an honest result.
        switchTask?.cancel()
        connectionState = .connecting(deviceName: device.name, progress: nil, detail: "Проверяю, в сети ли «\(device.name)»…")
        switchTask = Task { [weak self, stepDelay] in
            try? await Task.sleep(for: stepDelay * 3)
            guard !Task.isCancelled, let self else { return }
            self.connectionState = .deviceOffline(deviceName: device.name)
        }
    }

    func cancelSwitch() {
        switchTask?.cancel()
        switchTask = nil
        pendingDeviceID = nil
        connectionState = restingState
    }

    func refresh() async {
        try? await Task.sleep(for: stepDelay)
    }
}

@MainActor final class FixtureRemotePlayback: RemotePlaybackControlling {
    private static let playlist: [RemoteTrackInfo] = [
        RemoteTrackInfo(id: 1, title: "Тихий город", artist: "Тестовая библиотека"),
        RemoteTrackInfo(id: 2, title: "Северный свет", artist: "Тестовая библиотека"),
        RemoteTrackInfo(id: 3, title: "После дождя", artist: "Тестовая библиотека")
    ]
    private static let durations: [Double] = [224, 196, 243]
    @Published private(set) var track: RemoteTrackInfo? = nil
    @Published private(set) var position: Double = 84
    @Published private(set) var duration: Double = 224
    @Published private(set) var isPlaying = true
    @Published private(set) var volume: Double = 65
    @Published private(set) var deviceName: String
    @Published private(set) var connectionState: DeviceConnectionState
    var canGoPrevious: Bool { true }
    var controlsEnabled: Bool {
        switch connectionState {
        case .idle, .playingRemotely: return true
        default: return false
        }
    }
    private var index = 0
    private var ticker: Task<Void, Never>?

    init(deviceName: String = "Студия", offline: Bool = false, ticking: Bool = true) {
        self.deviceName = deviceName
        track = Self.playlist[0]
        connectionState = offline ? .deviceOffline(deviceName: deviceName) : .playingRemotely(deviceName: deviceName)
        isPlaying = !offline
        if ticking { startTicker() }
    }

    private func startTicker() {
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self else { return }
                guard self.isPlaying, self.controlsEnabled else { continue }
                if self.position + 1 >= self.duration { self.move(1) } else { self.position += 1 }
            }
        }
    }

    func adopt(deviceName: String?) {
        guard let deviceName else { return }
        self.deviceName = deviceName
        connectionState = .playingRemotely(deviceName: deviceName)
        isPlaying = true
    }

    private func move(_ direction: Int) {
        index = (index + direction + Self.playlist.count) % Self.playlist.count
        track = Self.playlist[index]
        duration = Self.durations[index]
        position = 0
    }

    func playPause() { isPlaying.toggle() }
    func next() { move(1) }
    func previous() { if position > 3 { position = 0 } else { move(-1) } }
    func seek(to seconds: Double) { position = min(max(0, seconds), duration) }
    func setVolume(_ value: Double) { volume = min(100, max(0, value)) }
    func retry() {
        guard connectionState.isError else { return }
        connectionState = .connecting(deviceName: deviceName, progress: nil, detail: "Проверяю связь с «\(deviceName)»…")
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard let self else { return }
            self.connectionState = .playingRemotely(deviceName: self.deviceName)
            self.isPlaying = true
        }
    }
}

/// Fixture host screens used by `--native-ui-screen devicepicker|remote`.
@MainActor struct DevicePickerFixtureScreen: View {
    @StateObject private var picker: FixtureDevicePickerModel
    @StateObject private var remote: FixtureRemotePlayback
    init(scenario: DeviceUIFixtureScenario = .current) {
        let remote = FixtureRemotePlayback()
        let picker = FixtureDevicePickerModel(scenario: scenario)
        picker.remote = remote
        _picker = StateObject(wrappedValue: picker)
        _remote = StateObject(wrappedValue: remote)
    }
    // Large detent keeps every row on screen for deterministic screenshots/UITests.
    var body: some View { DevicePickerSheet(model: picker, remote: remote, detents: [.large]) }
}

@MainActor struct RemoteControlFixtureScreen: View {
    @StateObject private var remote: FixtureRemotePlayback
    @Environment(\.dismiss) private var dismiss
    init(scenario: DeviceUIFixtureScenario = .current) {
        _remote = StateObject(wrappedValue: FixtureRemotePlayback(offline: scenario == .offline))
    }
    var body: some View {
        NavigationStack { RemoteControlScreen(model: remote, onClose: { dismiss() }) }
    }
}

/// Presents the fixture screens from NativeShell. Simulator Debug only.
@MainActor struct DeviceUIFixtureSheets: ViewModifier {
    @State private var picker = false
    @State private var remote = false
    func body(content: Content) -> some View {
        content
            .sheet(isPresented: $picker) { DevicePickerFixtureScreen() }
            .sheet(isPresented: $remote) { RemoteControlFixtureScreen() }
            .onAppear {
                #if DEBUG && targetEnvironment(simulator)
                guard NativeFixture.enabled else { return }
                switch NativeFixture.screen {
                case "devicepicker": picker = true
                case "remote": remote = true
                default: break
                }
                #endif
            }
    }
}

extension View {
    @MainActor func deviceUIFixtureSheets() -> some View { modifier(DeviceUIFixtureSheets()) }
}
