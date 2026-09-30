import SwiftUI
import MediaPlayer
import UIKit

/// AVPlayer.volume is an application gain, not the hardware output volume.
/// Keep these controls separate so a stale server value never positions the
/// iPhone slider or silently turns down the local device's system volume.
enum NativeVolumeTarget: Equatable {
    case system, remotePlayer, pcPlayer

    init(device: String, otherLocal: Bool) {
        self = device == "local" ? (otherLocal ? .remotePlayer : .system) : .pcPlayer
    }

    var title: String {
        switch self {
        case .system: return "Громкость iPhone"
        case .remotePlayer: return "Уровень XASS на другом iPhone"
        case .pcPlayer: return "Громкость XASS на ПК"
        }
    }
}

/// MPVolumeView centers the thumb on the first and last point of its track.
/// Once a custom thumb is installed, the stock track can still span the full
/// bounds and leave half of that image outside the view at 0% and 100%.
/// Reserve exactly the public thumb image's radius at both ends so the visible
/// and hit-test geometry agree without reaching into MPVolumeView's subviews.
private final class BoundedMPVolumeView: MPVolumeView {
    override func volumeSliderRect(forBounds bounds: CGRect) -> CGRect {
        let stock = super.volumeSliderRect(forBounds: bounds)
        let thumbWidth = max(
            volumeThumbImage(for: .normal)?.size.width ?? 0,
            volumeThumbImage(for: .highlighted)?.size.width ?? 0
        )
        let radius = thumbWidth / 2
        guard radius > 0, bounds.width >= thumbWidth else { return stock }

        let minimumX = max(stock.minX, bounds.minX + radius)
        let maximumX = min(stock.maxX, bounds.maxX - radius)
        guard maximumX >= minimumX else { return stock }
        return CGRect(x: minimumX, y: stock.minY,
                      width: maximumX - minimumX, height: stock.height)
    }

    override func volumeThumbRect(
        forBounds bounds: CGRect,
        volumeSliderRect rect: CGRect,
        value: Float
    ) -> CGRect {
        var thumb = super.volumeThumbRect(
            forBounds: bounds,
            volumeSliderRect: rect,
            value: value
        )

        // MPVolumeView's stock geometry is allowed to centre the thumb on a
        // track edge.  Depending on the iOS version and content scale, that
        // can leave part of a custom thumb outside either the horizontal or
        // vertical bounds even when volumeSliderRect itself is inset.  Clamp
        // the public thumb geometry, rather than finding/moving Apple's
        // private UISlider subview, so rendering and hit testing keep using
        // the supported MPVolumeView appearance hooks.
        guard thumb.width.isFinite, thumb.height.isFinite,
              bounds.width >= thumb.width, bounds.height >= thumb.height else {
            return thumb
        }
        thumb.origin.x = min(max(thumb.minX, bounds.minX), bounds.maxX - thumb.width)
        thumb.origin.y = min(max(thumb.minY, bounds.minY), bounds.maxY - thumb.height)
        return thumb
    }
}

/// A visible, interactive system control. No hidden slider, subview lookup,
/// synthesized control events, KVC, or programmatic system-volume mutation.
/// MPVolumeView follows hardware buttons and output-route changes itself.
@MainActor struct NativeSystemVolumeView: UIViewRepresentable {
    static func makeVolumeView() -> MPVolumeView {
        let view = BoundedMPVolumeView(frame: CGRect(x: 0, y: 0, width: 240, height: 44))
        view.showsVolumeSlider = true
        view.showsRouteButton = false // The player already has a separate route picker.
        view.tintColor = UIColor.white.withAlphaComponent(0.8)
        view.backgroundColor = .clear
        // MPVolumeView remains the public system-volume control, but its stock
        // thumb is visually too large in the compact player row. A public
        // appearance API gives it a bounded 14 pt thumb without finding or
        // mutating private subviews.
        let thumb = UIGraphicsImageRenderer(size: CGSize(width: 14, height: 14)).image { context in
            UIColor.white.setFill()
            context.cgContext.fillEllipse(in: CGRect(x: 1, y: 1, width: 12, height: 12))
        }
        view.setVolumeThumbImage(thumb, for: .normal)
        view.setVolumeThumbImage(thumb, for: .highlighted)
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.accessibilityIdentifier = "nativeSystemVolume"
        // Leave accessibility to the actual native slider, including its value
        // and adjustable actions. Making the parent an AX element would hide it.
        view.isAccessibilityElement = false
        return view
    }

    func makeUIView(context: Context) -> MPVolumeView { Self.makeVolumeView() }
    func updateUIView(_ view: MPVolumeView, context: Context) {
        // Deliberately no model -> slider value write: iOS owns system volume.
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: MPVolumeView, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 240, height: 44)
    }
}

@MainActor struct NativePlayerVolumeSection: View {
    @ObservedObject var store: NativeStore
    @ObservedObject private var audio: AudioController
    @State private var draft: Double?
    @State private var editingTarget: String?

    init(store: NativeStore) {
        self.store = store
        _audio = ObservedObject(wrappedValue: store.audio)
    }

    private var target: NativeVolumeTarget { NativeVolumeTarget(device: store.selectedDevice, otherLocal: store.otherLocal) }
    private var targetKey: String { store.selectedDevice + ":" + store.canonicalClientID + ":" + store.outputID }
    private var remoteValue: Double { min(100, max(0, draft ?? (store.volume.isFinite ? store.volume : 100))) }

    var body: some View {
        VStack(spacing: 5) {
            if target == .system {
                HStack(spacing: 14) {
                    Image(systemName: "speaker.fill").font(.caption).accessibilityHidden(true)
                    NativeSystemVolumeView().frame(maxWidth: .infinity, minHeight: 44, maxHeight: 44)
                        .padding(.horizontal, 4).layoutPriority(1)
                    Image(systemName: "speaker.wave.2.fill").font(.caption).accessibilityHidden(true)
                }
                Text(target.title).font(.caption2).accessibilityIdentifier("nativeSystemVolumeLabel")
                if audio.playbackGain < 99.9 {
                    VStack(spacing: 5) {
                        Text("Уровень XASS уменьшен удалённо до \(Int(audio.playbackGain))%. Это отдельное ограничение, не громкость iPhone.")
                            .font(.caption).multilineTextAlignment(.center)
                        Button("Сбросить ограничение XASS") { store.resetLocalPlaybackGain() }
                            .font(.caption.weight(.semibold)).foregroundStyle(.white)
                            .frame(minHeight: 44).accessibilityIdentifier("nativeResetPlaybackGain")
                    }.accessibilityIdentifier("nativePlaybackGainNotice")
                }
            } else {
                PlayerVolumeSlider(volume: remoteValue / 100, reduceMotion: false,
                    onEditingChanged: updateRemoteVolume,
                    onChange: { draft = min(100, max(0, $0 * 100)) })
                    .disabled(store.busy)
                    .accessibilityLabel(target.title).accessibilityIdentifier("nativeRemoteVolume")
                Text(target.title).font(.caption2)
                if target == .remotePlayer {
                    Text("Кнопки громкости здесь меняют только этот iPhone.").font(.caption2).multilineTextAlignment(.center)
                }
            }
        }.foregroundStyle(.white.opacity(0.55))
            .onChange(of: targetKey) { _, _ in draft = nil; editingTarget = nil }
    }

    private func updateRemoteVolume(_ editing: Bool) {
        if editing { editingTarget = targetKey; return }
        let value = draft, originalTarget = editingTarget
        draft = nil; editingTarget = nil
        guard let value, originalTarget == targetKey, target != .system else { return }
        store.run {
            // A route switch between gesture end and Task execution must not
            // send the old slider value to a different device.
            guard originalTarget == targetKey, target != .system else { return }
            try await store.setVolume(value)
        }
    }
}
