import Foundation
import Combine

// UI-only models for the device picker and the PC remote.
// Views consume these protocols only; they never reach OwnerAPI, transfer or
// LAN code directly. A fixture drives previews/UI screenshots, and
// `NativeStoreDeviceAdapter` (DeviceStoreAdapter.swift) maps NativeStore.

enum PlaybackDeviceKind: Equatable {
    case thisPhone, computer, otherPlayer

    var systemImage: String {
        switch self {
        case .thisPhone: return "iphone"
        case .computer: return "desktopcomputer"
        case .otherPlayer: return "hifispeaker"
        }
    }

    var spokenKind: String {
        switch self {
        case .thisPhone: return "iPhone"
        case .computer: return "компьютер"
        case .otherPlayer: return "плеер"
        }
    }
}

/// One playback target. `id` is the route key used by NativeStore
/// ("local" for this iPhone, "agent:<name>" for a PC agent).
struct PlaybackDevice: Identifiable, Equatable {
    let id: String
    var name: String
    var kind: PlaybackDeviceKind
    var isOnline: Bool
    /// Optional short secondary text, e.g. an audio output or agent version.
    var detail: String?
    /// Stable ASCII key for accessibility identifiers ("local", "device-1").
    var accessibilityKey: String

    static let thisPhoneID = "local"
    static func thisPhone(name: String = "Этот iPhone") -> PlaybackDevice {
        PlaybackDevice(id: thisPhoneID, name: name, kind: .thisPhone, isOnline: true, detail: nil, accessibilityKey: "local")
    }
}

/// Every state the picker and the remote can be in. Rendered by one
/// reusable `DeviceStatusView`, so no state ends as a silent spinner.
enum DeviceConnectionState: Equatable {
    /// Nothing special: the active device plays or is paused normally.
    case idle
    /// Only this iPhone is known; no PC agents are paired or reported.
    case noDevices
    /// A switch/handoff is in flight. `progress` in 0...1 when known.
    case connecting(deviceName: String, progress: Double?, detail: String?)
    /// Music is playing on a remote device (PC).
    case playingRemotely(deviceName: String)
    /// The target or active PC stopped answering.
    case deviceOffline(deviceName: String)
    /// A switch was attempted and failed; can be retried.
    case switchFailed(deviceName: String, message: String)

    var isBusy: Bool { if case .connecting = self { return true }; return false }
    var canRetry: Bool {
        switch self {
        case .switchFailed, .deviceOffline: return true
        default: return false
        }
    }
    var isError: Bool {
        switch self {
        case .switchFailed, .deviceOffline: return true
        default: return false
        }
    }

    var title: String {
        switch self {
        case .idle: return "Готово"
        case .noDevices: return "Других устройств нет"
        case .connecting(let name, _, _): return "Подключаюсь к «\(name)»…"
        case .playingRemotely(let name): return "Играет на «\(name)»"
        case .deviceOffline(let name): return "«\(name)» не в сети"
        case .switchFailed(let name, _): return "Не удалось переключить на «\(name)»"
        }
    }

    var message: String? {
        switch self {
        case .idle: return nil
        case .noDevices: return "Музыка играет на этом iPhone. Установите агент XASS на компьютер, чтобы слушать на нём и управлять им отсюда."
        case .connecting(_, _, let detail): return detail ?? "Музыка продолжит играть на текущем устройстве, пока новое не подтвердит запуск."
        case .playingRemotely: return "Управляйте воспроизведением с iPhone: пауза, перемотка и громкость."
        case .deviceOffline: return "Проверьте, что компьютер включён, агент XASS запущен и есть интернет. Музыка на iPhone не прервётся."
        case .switchFailed(_, let message): return message.isEmpty ? "Устройство не ответило вовремя." : message
        }
    }

    var systemImage: String {
        switch self {
        case .idle: return "checkmark.circle"
        case .noDevices: return "desktopcomputer.trianglebadge.exclamationmark"
        case .connecting: return "arrow.triangle.2.circlepath"
        case .playingRemotely: return "hifispeaker.and.homepod"
        case .deviceOffline: return "wifi.slash"
        case .switchFailed: return "exclamationmark.triangle"
        }
    }

    /// Short key for accessibility identifiers and UI tests.
    var identifierKey: String {
        switch self {
        case .idle: return "idle"
        case .noDevices: return "noDevices"
        case .connecting: return "connecting"
        case .playingRemotely: return "playingRemotely"
        case .deviceOffline: return "deviceOffline"
        case .switchFailed: return "switchFailed"
        }
    }
}

/// Minimal track description for the remote; no library or network types.
struct RemoteTrackInfo: Equatable {
    var id: Int?
    var title: String
    var artist: String
}

/// Device picker contract ("Где слушать").
@MainActor protocol DevicePickerModel: ObservableObject {
    /// This iPhone first, then PCs and other players.
    var devices: [PlaybackDevice] { get }
    /// Route key of the device that owns playback now.
    var activeDeviceID: String { get }
    /// Route key a switch is heading to, while connecting or after a failure.
    var pendingDeviceID: String? { get }
    var connectionState: DeviceConnectionState { get }
    /// Whether the active device has something playing right now.
    var isPlaying: Bool { get }
    func select(_ device: PlaybackDevice)
    func retry()
    func cancelSwitch()
    func refresh() async
}

/// PC remote contract. Volume is 0...100 to match the server/agent scale.
@MainActor protocol RemotePlaybackControlling: ObservableObject {
    var track: RemoteTrackInfo? { get }
    /// Current position in seconds (may be projected between server samples).
    var position: Double { get }
    var duration: Double { get }
    var isPlaying: Bool { get }
    var volume: Double { get }
    var deviceName: String { get }
    var connectionState: DeviceConnectionState { get }
    var canGoPrevious: Bool { get }
    /// Controls are temporarily unavailable (command in flight or offline).
    var controlsEnabled: Bool { get }
    func playPause()
    func next()
    func previous()
    func seek(to seconds: Double)
    func setVolume(_ value: Double)
    func retry()
}

enum DeviceUIFormat {
    static func time(_ value: Double) -> String { NativeValue.time(value) }
    static func spokenTime(_ value: Double) -> String {
        let total = Int(value.isFinite ? min(max(0, value), 86400) : 0)
        let minutes = total / 60, seconds = total % 60
        return minutes > 0 ? "\(minutes) мин \(seconds) с" : "\(seconds) с"
    }
    static func percent(_ value: Double) -> String { "\(Int(min(100, max(0, value)).rounded())) процентов" }
}
