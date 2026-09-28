import SwiftUI

// PC remote: title/artist, scrubbable progress, transport and volume for the
// device that plays now. Driven only by `RemotePlaybackControlling`; the
// Now Playing screen and mini-player stay untouched.

@MainActor struct RemoteControlPanel<Model: RemotePlaybackControlling>: View {
    @ObservedObject var model: Model
    var compact = false
    @State private var scrubbing: Double?
    @State private var volumeDraft: Double?
    @State private var tapCount = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .title) private var artworkSide: CGFloat = 64
    @ScaledMetric(relativeTo: .largeTitle) private var playSide: CGFloat = 60

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 12 : 20) {
            header
            if showsStatus {
                DeviceStatusView(state: model.connectionState, onRetry: { model.retry() })
            }
            trackInfo
            scrubber
            transport
            volume
        }
        .animation(DeviceUIMotion.spring(reduceMotion), value: model.connectionState)
        .animation(DeviceUIMotion.spring(reduceMotion), value: model.track)
        .sensoryFeedback(.impact(weight: .light), trigger: tapCount)
        // Selection ticks while dragging seek/volume (whole seconds / 5 % steps).
        .sensoryFeedback(.selection, trigger: scrubbing.map { Int($0) })
        .sensoryFeedback(.selection, trigger: volumeDraft.map { Int($0 / 5) })
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("remoteControlPanel")
    }

    private var showsStatus: Bool {
        switch model.connectionState {
        case .idle, .playingRemotely: return false
        default: return true
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "desktopcomputer").accessibilityHidden(true)
            Text("Играет на «\(model.deviceName)»")
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(online ? Color.green : Color.secondary)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(online ? "Воспроизведение на компьютере «\(model.deviceName)»" : "Компьютер «\(model.deviceName)» недоступен")
        .accessibilityIdentifier("remoteDeviceName")
    }

    private var online: Bool {
        if case .deviceOffline = model.connectionState { return false }
        return true
    }

    @ViewBuilder private var trackInfo: some View {
        HStack(spacing: 14) {
            ZStack {
                RoundedRectangle(cornerRadius: 10).fill(XASSStyle.surface)
                Image(systemName: "music.note")
                    .font(.system(size: (compact ? artworkSide * 0.75 : artworkSide) * 0.4, weight: .medium))
                    .foregroundStyle(XASSStyle.secondary)
            }
            .frame(width: compact ? artworkSide * 0.75 : artworkSide, height: compact ? artworkSide * 0.75 : artworkSide)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(model.track?.title ?? "Ничего не играет")
                    .font(compact ? .headline : .title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("remoteTrackTitle")
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
    }

    private var subtitle: String {
        guard let track = model.track else { return "Выберите трек в библиотеке — он зазвучит на компьютере." }
        return track.artist.isEmpty ? "Моя коллекция" : track.artist
    }

    private var scrubber: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            let upper = max(1, model.duration)
            let shown = min(max(0, scrubbing ?? model.position), upper)
            VStack(spacing: 2) {
                Slider(value: Binding(get: { shown }, set: { scrubbing = $0 }), in: 0...upper, onEditingChanged: { editing in
                    if !editing, let value = scrubbing { model.seek(to: value); scrubbing = nil }
                })
                .tint(XASSStyle.accent)
                .frame(minHeight: 44)
                .disabled(!controlsEnabled || model.duration <= 0)
                .accessibilityLabel("Позиция трека")
                .accessibilityValue("\(DeviceUIFormat.spokenTime(shown)) из \(DeviceUIFormat.spokenTime(model.duration))")
                .accessibilityAdjustableAction { direction in
                    switch direction {
                    case .increment: model.seek(to: min(upper, shown + 10))
                    case .decrement: model.seek(to: max(0, shown - 10))
                    @unknown default: break
                    }
                }
                .accessibilityIdentifier("remoteSeek")
                HStack {
                    Text(DeviceUIFormat.time(shown))
                    Spacer()
                    Text("−" + DeviceUIFormat.time(max(0, model.duration - shown)))
                }
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            }
        }
    }

    private var transport: some View {
        HStack {
            Spacer(minLength: 0)
            Button { tapCount += 1; model.previous() } label: {
                Image(systemName: "backward.fill").font(.title2).frame(minWidth: 52, minHeight: 52)
            }
            .disabled(!controlsEnabled || !model.canGoPrevious)
            .accessibilityLabel("Предыдущий трек")
            .accessibilityIdentifier("remotePrevious")
            Spacer(minLength: 12)
            Button { tapCount += 1; model.playPause() } label: {
                Image(systemName: model.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: playSide * 0.45, weight: .semibold))
                    .contentTransition(reduceMotion ? .opacity : .symbolEffect(.replace))
                    .frame(width: playSide, height: playSide)
                    .background(Circle().fill(Color.white.opacity(0.12)))
            }
            .disabled(!controlsEnabled || model.track == nil)
            .accessibilityLabel(model.isPlaying ? "Пауза" : "Слушать")
            .accessibilityHint("На компьютере «\(model.deviceName)»")
            .scaleEffect(reduceMotion || model.isPlaying ? 1 : 0.94)
            .animation(DeviceUIMotion.spring(reduceMotion), value: model.isPlaying)
            .accessibilityIdentifier("remotePlayPause")
            Spacer(minLength: 12)
            Button { tapCount += 1; model.next() } label: {
                Image(systemName: "forward.fill").font(.title2).frame(minWidth: 52, minHeight: 52)
            }
            .disabled(!controlsEnabled)
            .accessibilityLabel("Следующий трек")
            .accessibilityIdentifier("remoteNext")
            Spacer(minLength: 0)
        }
        // Plain style: several buttons inside one List row must not all fire together.
        .buttonStyle(.plain)
        .foregroundStyle(.primary)
    }

    private var volume: some View {
        let shown = min(100, max(0, volumeDraft ?? model.volume))
        return HStack(spacing: 10) {
            Image(systemName: "speaker.fill").font(.footnote).foregroundStyle(.secondary).accessibilityHidden(true)
            Slider(value: Binding(get: { shown }, set: { volumeDraft = $0 }), in: 0...100, onEditingChanged: { editing in
                if !editing, let value = volumeDraft { model.setVolume(value); volumeDraft = nil }
            })
            .tint(.white.opacity(0.85))
            .frame(minHeight: 44)
            .disabled(!controlsEnabled)
            .accessibilityLabel("Громкость на компьютере")
            .accessibilityValue(DeviceUIFormat.percent(shown))
            .accessibilityAdjustableAction { direction in
                switch direction {
                case .increment: model.setVolume(min(100, shown + 10))
                case .decrement: model.setVolume(max(0, shown - 10))
                @unknown default: break
                }
            }
            .accessibilityIdentifier("remoteVolume")
            Image(systemName: "speaker.wave.3.fill").font(.footnote).foregroundStyle(.secondary).accessibilityHidden(true)
        }
    }

    private var controlsEnabled: Bool { model.controlsEnabled }
}

/// Full-screen remote ("Пульт компьютера"). Embed in a NavigationStack.
@MainActor struct RemoteControlScreen<Model: RemotePlaybackControlling>: View {
    @ObservedObject var model: Model
    var onClose: (() -> Void)? = nil

    var body: some View {
        ScrollView {
            RemoteControlPanel(model: model)
                .padding(.horizontal, 24)
                .padding(.vertical, 16)
                .frame(maxWidth: 560)
                .frame(maxWidth: .infinity)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(XASSStyle.background)
        .navigationTitle("Пульт компьютера")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                if let onClose {
                    Button("Готово", action: onClose).accessibilityIdentifier("remoteDone")
                }
            }
        }
    }
}

#Preview("Пульт ПК") {
    NavigationStack { RemoteControlScreen(model: FixtureRemotePlayback()) }
        .preferredColorScheme(.dark)
}

#Preview("ПК не в сети") {
    NavigationStack { RemoteControlScreen(model: FixtureRemotePlayback(offline: true)) }
        .preferredColorScheme(.dark)
}
