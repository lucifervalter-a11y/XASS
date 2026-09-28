import SwiftUI
import UIKit

// «Где слушать»: AirPlay-style device picker. One sheet lists this iPhone,
// the user's PCs and other players, shows which one is playing now, and
// renders switching progress and failures inline (no stacked overlays).
// Driven only by `DevicePickerModel` / `RemotePlaybackControlling`.

/// Apple Music / AirPlay motion: spring (response 0.45, damping 0.85), or a
/// short crossfade when Reduce Motion is on (no spring, scale or pulsing).
enum DeviceUIMotion {
    static func spring(_ reduceMotion: Bool) -> Animation {
        reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.45, dampingFraction: 0.85)
    }
    static func indicator(_ reduceMotion: Bool) -> AnyTransition {
        reduceMotion ? .opacity : .scale(scale: 0.4).combined(with: .opacity)
    }
    static func card(_ reduceMotion: Bool) -> AnyTransition {
        reduceMotion ? .opacity : .move(edge: .top).combined(with: .opacity)
    }
}

/// Haptic for the result of a switch: success when a device took over,
/// error when it failed or turned out offline; nothing otherwise.
enum DeviceUIHaptics {
    static func result(from old: DeviceConnectionState, to new: DeviceConnectionState) -> SensoryFeedback? {
        guard old.isBusy else { return nil }
        if new.isError { return .error }
        if !new.isBusy { return .success }
        return nil
    }
}

/// One reusable view for every `DeviceConnectionState`.
@MainActor struct DeviceStatusView: View {
    let state: DeviceConnectionState
    var onRetry: (() -> Void)? = nil
    var onCancel: (() -> Void)? = nil
    @ScaledMetric(relativeTo: .headline) private var iconSize: CGFloat = 30

    var body: some View {
        if state != .idle {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    icon.frame(width: iconSize + 10, height: iconSize + 10)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(state.title)
                            .font(.headline)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("deviceStatus-\(state.identifierKey)")
                        if let message = state.message {
                            Text(message)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                }
                if case .connecting(_, let progress, _) = state, let progress {
                    ProgressView(value: min(max(progress, 0), 1))
                        .tint(XASSStyle.accent)
                        .accessibilityLabel("Ход переключения")
                        .accessibilityValue(DeviceUIFormat.percent(progress * 100))
                        .accessibilityIdentifier("deviceStatusProgress")
                }
                if hasActions {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 10) { actions }
                        VStack(alignment: .leading, spacing: 10) { actions }
                    }
                }
            }
            .padding(.vertical, 6)
            .accessibilityElement(children: .contain)
            .onChange(of: state) { _, next in
                // Announce state changes so VoiceOver users are never left waiting silently.
                guard next != .idle else { return }
                UIAccessibility.post(notification: .announcement, argument: next.title)
            }
        }
    }

    private var hasActions: Bool { (state.canRetry && onRetry != nil) || (state.isBusy && onCancel != nil) }

    @ViewBuilder private var actions: some View {
        if state.canRetry, let onRetry {
            Button(action: onRetry) {
                Label("Повторить", systemImage: "arrow.clockwise").frame(minHeight: 44).padding(.horizontal, 6)
            }
            .buttonStyle(.borderedProminent)
            .tint(XASSStyle.accent)
            .accessibilityHint("Попробовать подключиться ещё раз")
            .accessibilityIdentifier("deviceStatusRetry")
        }
        if state.isBusy, let onCancel {
            Button(action: onCancel) {
                Text("Отмена").frame(minHeight: 44).padding(.horizontal, 6)
            }
            .buttonStyle(.bordered)
            .accessibilityHint("Остаться на текущем устройстве")
            .accessibilityIdentifier("deviceStatusCancel")
        }
    }

    @ViewBuilder private var icon: some View {
        if state.isBusy {
            ProgressView().controlSize(.regular).accessibilityHidden(true)
        } else {
            Image(systemName: state.systemImage)
                .font(.system(size: iconSize * 0.75, weight: .semibold))
                .foregroundStyle(tint)
                .accessibilityHidden(true)
        }
    }

    private var tint: Color {
        switch state {
        case .playingRemotely: return .green
        case .switchFailed: return .orange
        case .deviceOffline: return .gray
        case .noDevices: return XASSStyle.secondary
        case .connecting, .idle: return XASSStyle.accent
        }
    }
}

/// One row: icon, name, online/playing state, trailing indicator.
@MainActor struct DevicePickerRow: View {
    let device: PlaybackDevice
    let isActive: Bool
    let isPlaying: Bool
    let isPending: Bool
    let isFailed: Bool
    let action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var iconBox: CGFloat = 44

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                ZStack {
                    RoundedRectangle(cornerRadius: 10)
                        .fill(isActive ? XASSStyle.accent : Color.white.opacity(0.08))
                    Image(systemName: device.kind.systemImage)
                        .font(.title3)
                        .foregroundStyle(isActive ? Color.white : (device.isOnline ? Color.primary : Color.secondary))
                }
                .frame(width: iconBox, height: iconBox)
                .overlay { if isPending && !isFailed { ConnectingPulse(cornerRadius: 10) } }
                .animation(DeviceUIMotion.spring(reduceMotion), value: isActive)
                VStack(alignment: .leading, spacing: 3) {
                    Text(device.name)
                        .font(.body.weight(isActive ? .semibold : .regular))
                        .foregroundStyle(device.isOnline ? Color.primary : Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 6) {
                        Circle().fill(dotColor).frame(width: 7, height: 7)
                        Text(subtitle)
                            .font(.caption)
                            .foregroundStyle(isActive && isPlaying ? Color.green : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                trailing
                    .frame(minWidth: 28)
                    .animation(DeviceUIMotion.spring(reduceMotion), value: trailingKey)
            }
            .frame(minHeight: 56)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(Text(spokenLabel))
        .accessibilityHint(Text(hint))
        .accessibilityAddTraits(isActive ? .isSelected : [])
        .accessibilityIdentifier("devicePicker-\(device.accessibilityKey)")
    }

    private var subtitle: String {
        if isPending && !isFailed { return "Подключаюсь…" }
        if isFailed { return device.isOnline ? "Не удалось переключить" : "Не в сети" }
        if !device.isOnline { return "Не в сети" }
        if isActive { return isPlaying ? "Играет сейчас" : "Выбрано · пауза" }
        return device.detail ?? "В сети"
    }

    private var dotColor: Color {
        if isFailed { return .orange }
        if !device.isOnline { return .gray }
        return isActive && isPlaying ? .green : Color.green.opacity(0.7)
    }

    private var spokenLabel: String {
        var parts = [device.name, device.kind.spokenKind, subtitle]
        if let detail = device.detail, isActive, device.isOnline { parts.append(detail) }
        return parts.joined(separator: ", ")
    }

    private var hint: String {
        if isActive { return "Сейчас выбрано" }
        if !device.isOnline { return "Устройство не в сети. Дважды коснитесь, чтобы узнать подробности" }
        return "Дважды коснитесь, чтобы слушать здесь"
    }

    private var trailingKey: Int {
        if isPending && !isFailed { return 1 }
        if isFailed { return 2 }
        if isActive && isPlaying { return 3 }
        return isActive ? 4 : 0
    }

    @ViewBuilder private var trailing: some View {
        Group {
            if isPending && !isFailed {
                ProgressView()
            } else if isFailed {
                Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
            } else if isActive && isPlaying {
                Image(systemName: "waveform")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.green)
                    .symbolEffect(.variableColor.iterative, isActive: !reduceMotion)
            } else if isActive {
                Image(systemName: "checkmark").font(.body.weight(.semibold)).foregroundStyle(XASSStyle.accent)
            }
        }
        .transition(DeviceUIMotion.indicator(reduceMotion))
        .accessibilityHidden(true)
    }
}

/// Pulsing ring around the target row's icon while connecting.
/// Reduce Motion: a static accent outline, no pulsing.
@MainActor struct ConnectingPulse: View {
    let cornerRadius: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expanded = false
    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius)
            .stroke(XASSStyle.accent, lineWidth: 2)
            .scaleEffect(reduceMotion ? 1 : (expanded ? 1.22 : 1))
            .opacity(reduceMotion ? 0.9 : (expanded ? 0 : 0.9))
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeOut(duration: 1.1).repeatForever(autoreverses: false)) { expanded = true }
            }
            .accessibilityHidden(true)
    }
}

/// The sheet. When a PC is the active device, a compact remote is embedded
/// at the top of the same sheet (AirPlay-style "now playing" header).
@MainActor struct DevicePickerSheet<PickerModel: DevicePickerModel, RemoteModel: RemotePlaybackControlling>: View {
    @ObservedObject var model: PickerModel
    let remote: RemoteModel
    var showsSystemRoute = true
    var detents: Set<PresentationDetent> = [.medium, .large]
    var onClose: (() -> Void)? = nil
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var tapCount = 0

    private var activeDevice: PlaybackDevice? { model.devices.first { $0.id == model.activeDeviceID } }
    private var remoteActive: Bool {
        guard let active = activeDevice else { return false }
        return active.kind != .thisPhone
    }

    var body: some View {
        NavigationStack {
            List {
                if showsRemotePanel {
                    Section {
                        RemoteControlPanel(model: remote, compact: true)
                            .transition(DeviceUIMotion.card(reduceMotion))
                    } header: { Text("Сейчас играет") }
                } else if model.connectionState != .idle {
                    Section {
                        DeviceStatusView(state: model.connectionState, onRetry: { tapCount += 1; model.retry() }, onCancel: { tapCount += 1; model.cancelSwitch() })
                            .transition(DeviceUIMotion.card(reduceMotion))
                    }
                }
                Section {
                    ForEach(model.devices) { device in
                        DevicePickerRow(
                            device: device,
                            isActive: device.id == model.activeDeviceID,
                            isPlaying: model.isPlaying && device.id == model.activeDeviceID,
                            isPending: device.id == model.pendingDeviceID && model.connectionState.isBusy,
                            isFailed: device.id == model.pendingDeviceID && model.connectionState.isError
                        ) {
                            tapCount += 1
                            withAnimation(DeviceUIMotion.spring(reduceMotion)) { model.select(device) }
                        }
                        .disabled(model.connectionState.isBusy && device.id != model.pendingDeviceID)
                    }
                } header: {
                    Text("Устройства")
                } footer: {
                    Text("Музыка продолжит играть на текущем устройстве, пока новое не подтвердит запуск.")
                }
                if showsSystemRoute {
                    Section("AirPlay и Bluetooth") { NativeSystemAudioRoute() }
                }
            }
            .animation(DeviceUIMotion.spring(reduceMotion), value: model.connectionState)
            .animation(DeviceUIMotion.spring(reduceMotion), value: model.activeDeviceID)
            .sensoryFeedback(.impact(weight: .light), trigger: tapCount)
            .sensoryFeedback(trigger: model.connectionState) { old, new in DeviceUIHaptics.result(from: old, to: new) }
            .navigationTitle("Где слушать")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refresh() }
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Готово") { if let onClose { onClose() } else { dismiss() } }
                        .accessibilityIdentifier("devicePickerDone")
                }
            }
        }
        // System sheet = native AirPlay/Apple Music bottom sheet: spring
        // presentation and velocity-aware interactive swipe-down that springs
        // back when cancelled. Reduce Motion is honoured by the system.
        .presentationDetents(detents)
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(22)
    }

    /// The compact remote replaces the status card only when nothing needs
    /// attention: switching progress and errors always stay visible.
    private var showsRemotePanel: Bool {
        guard remoteActive else { return false }
        switch model.connectionState {
        case .idle, .playingRemotely: return true
        default: return false
        }
    }
}

/// Self-contained presentation from a binding. Keep it mutually exclusive with
/// any Now Playing overlay by driving `isPresented` from one explicit state.
extension View {
    @MainActor func devicePicker<PickerModel: DevicePickerModel, RemoteModel: RemotePlaybackControlling>(
        isPresented: Binding<Bool>, model: PickerModel, remote: RemoteModel
    ) -> some View {
        sheet(isPresented: isPresented) {
            DevicePickerSheet(model: model, remote: remote, onClose: { isPresented.wrappedValue = false })
        }
    }
}

#Preview("Где слушать") {
    DevicePickerSheet(model: FixtureDevicePickerModel(scenario: .normal), remote: FixtureRemotePlayback(), showsSystemRoute: false)
        .preferredColorScheme(.dark)
}

#Preview("Ошибка переключения") {
    DevicePickerSheet(model: FixtureDevicePickerModel(scenario: .failed), remote: FixtureRemotePlayback(), showsSystemRoute: false)
        .preferredColorScheme(.dark)
}

#Preview("Нет устройств") {
    DevicePickerSheet(model: FixtureDevicePickerModel(scenario: .empty), remote: FixtureRemotePlayback(), showsSystemRoute: false)
        .preferredColorScheme(.dark)
}
