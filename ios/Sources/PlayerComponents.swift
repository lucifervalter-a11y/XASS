import SwiftUI
import UIKit

@MainActor enum PlayerHaptics {
    private static let light = UIImpactFeedbackGenerator(style: .light)
    private static let soft = UIImpactFeedbackGenerator(style: .soft)
    private static let selection = UISelectionFeedbackGenerator()
    static func tap() { light.impactOccurred() }
    static func dismiss() { soft.impactOccurred() }
    static func prepareScrub() { selection.prepare() }
    static func scrub() { selection.selectionChanged() }
}

enum PlayerLayout {
    /// Space tab content reserves above the tab bar for the floating mini player.
    static let miniReservedHeight: CGFloat = 76
}

enum PlayerMotion {
    /// Expand/collapse and hero moves.
    static let hero = Animation.spring(response: 0.45, dampingFraction: 0.85)
    /// Reduce Motion: a plain crossfade instead of springs and geometry matching.
    static let fade = Animation.easeInOut(duration: 0.25)
    static let trackChange = Animation.spring(response: 0.42, dampingFraction: 0.9)
    static let background = Animation.easeInOut(duration: 0.7)
    static func expand(_ reduceMotion: Bool) -> Animation { reduceMotion ? fade : hero }
}

/// Everything store-specific that Now Playing can host without knowing NativeStore.
struct NowPlayingSlots {
    /// Replaces the default volume slider (e.g. MPVolumeView section for the real store).
    var volume: (() -> AnyView)?
    /// Replaces the synced lyrics view (e.g. the store's existing NativeLyricsContent).
    var lyrics: (() -> AnyView)?
    /// Loading / transfer / error messages under the transport controls.
    var status: (() -> AnyView)?
    /// Next to the title, e.g. a favorite button.
    var titleAccessory: (() -> AnyView)?
    var deviceLabel: String = "Этот iPhone"
    var deviceSymbol: String = "airplayaudio"
    /// Opens the device picker. The root overlay switches to `.devicePicker`.
    var onDevices: (() -> Void)?
    var onQueue: (() -> Void)?
    var onMore: (() -> Void)?
    var sourceLabel: String = "Моя музыка"
}

extension View {
    /// matchedGeometryEffect that switches off under Reduce Motion.
    @ViewBuilder func playerHero(_ id: String, in namespace: Namespace.ID, enabled: Bool,
                                 properties: MatchedGeometryProperties = .frame) -> some View {
        if enabled { matchedGeometryEffect(id: id, in: namespace, properties: properties) } else { self }
    }
}

extension AnyTransition {
    /// Track change: the new item slides in from the swipe side, the old one leaves the other way.
    static func playerSlide(direction: CGFloat, distance: CGFloat, reduceMotion: Bool) -> AnyTransition {
        guard !reduceMotion else { return .opacity }
        return .asymmetric(insertion: .offset(x: direction * distance).combined(with: .opacity),
                           removal: .offset(x: -direction * distance).combined(with: .opacity))
    }
}

/// Artwork or a neutral placeholder. The image is already decoded by the provider.
struct PlayerArtworkImage: View {
    let image: UIImage?
    var cornerRadius: CGFloat
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.2, green: 0.22, blue: 0.27), Color(red: 0.11, green: 0.12, blue: 0.15)],
                                     startPoint: .topLeading, endPoint: .bottomTrailing))
            if let image = image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Image(systemName: "music.note").resizable().scaledToFit().padding(24).frame(maxWidth: 96, maxHeight: 96)
                    .foregroundStyle(.white.opacity(0.45))
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        .accessibilityHidden(true)
    }
}

/// Pre-blurred artwork over a gradient of its dominant colors. The blur is a
/// cached bitmap; a track change crossfades both layers without a flash.
struct NowPlayingBackground: View {
    let look: PlayerArtworkLook
    let reduceMotion: Bool
    var body: some View {
        ZStack {
            LinearGradient(colors: look.colors, startPoint: .top, endPoint: .bottom)
            if let blurred = look.blurred {
                Color.clear
                    .overlay { Image(uiImage: blurred).resizable().scaledToFill().scaleEffect(1.25) }
                    .clipped()
                    .opacity(0.62)
                    .id(look.key)
                    .transition(.opacity)
            }
            LinearGradient(colors: look.colors.map { $0.opacity(0.55) }, startPoint: .topLeading, endPoint: .bottomTrailing)
            LinearGradient(colors: [.black.opacity(0.05), .black.opacity(0.42)], startPoint: .top, endPoint: .bottom)
        }
        .animation(reduceMotion ? PlayerMotion.fade : PlayerMotion.background, value: look.key)
        .accessibilityHidden(true)
    }
}

/// Apple Music-style capsule scrubber. Selection haptics while dragging.
struct PlayerScrubber: View {
    let position: TimeInterval
    let duration: TimeInterval
    let disabled: Bool
    let reduceMotion: Bool
    let onSeek: (TimeInterval) -> Void
    @State private var dragValue: TimeInterval?
    @State private var pendingSeek: TimeInterval?
    @State private var hapticStep = -1

    private var shown: TimeInterval { min(max(0, dragValue ?? pendingSeek ?? position), max(0, duration)) }

    var body: some View {
        let fraction = duration > 0 ? shown / duration : 0
        let active = dragValue != nil
        VStack(spacing: 6) {
            GeometryReader { geometry in
                let width = max(1, geometry.size.width)
                ZStack(alignment: .leading) {
                    Capsule().fill(.white.opacity(0.22))
                    Capsule().fill(.white.opacity(active ? 1 : 0.8)).frame(width: width * fraction)
                }
                .frame(height: active ? 11 : 6)
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                    guard duration > 0, !disabled else { return }
                    if dragValue == nil { PlayerHaptics.prepareScrub() }
                    let next = min(1, max(0, value.location.x / width))
                    dragValue = next * duration
                    let step = Int(next * 40)
                    if step != hapticStep { hapticStep = step; PlayerHaptics.scrub() }
                }.onEnded { _ in
                    if let value = dragValue { pendingSeek = value; onSeek(value) }
                    dragValue = nil; hapticStep = -1
                })
            }
            .frame(height: 22)
            .animation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.8), value: active)
            HStack {
                Text(NativeValue.time(shown))
                Spacer()
                Text("−" + NativeValue.time(max(0, duration - shown)))
            }
            .font(.caption.weight(.medium)).monospacedDigit().foregroundStyle(.white.opacity(active ? 0.9 : 0.6))
        }
        .onChange(of: position) { _, value in
            if let pending = pendingSeek, abs(value - pending) < 1.5 { pendingSeek = nil }
        }
        .task(id: pendingSeek) {
            // A remote seek may never echo back exactly; do not pin the knob forever.
            guard pendingSeek != nil else { return }
            try? await Task.sleep(for: .seconds(2.5))
            if !Task.isCancelled { pendingSeek = nil }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Позиция трека")
        .accessibilityValue("\(NativeValue.time(shown)) из \(NativeValue.time(duration))")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: onSeek(min(duration, shown + 10))
            case .decrement: onSeek(max(0, shown - 10))
            @unknown default: break
            }
        }
        .accessibilityIdentifier("nowPlayingScrubber")
        .disabled(disabled)
    }
}

/// Default volume control for providers that own their level (fixture, remote players).
struct PlayerVolumeSlider: View {
    let volume: Double
    let reduceMotion: Bool
    let onChange: (Double) -> Void
    @State private var dragValue: Double?

    var body: some View {
        let value = min(1, max(0, dragValue ?? volume))
        HStack(spacing: 12) {
            Image(systemName: "speaker.fill").font(.caption)
            GeometryReader { geometry in
                let width = max(1, geometry.size.width)
                ZStack(alignment: .leading) {
                    Capsule().fill(.white.opacity(0.22))
                    Capsule().fill(.white.opacity(dragValue == nil ? 0.8 : 1)).frame(width: width * value)
                }
                .frame(height: dragValue == nil ? 6 : 11)
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { gesture in
                    let next = min(1, max(0, gesture.location.x / width))
                    dragValue = next; onChange(next)
                }.onEnded { _ in dragValue = nil })
            }
            .frame(height: 30)
            .animation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.8), value: dragValue == nil)
            Image(systemName: "speaker.wave.3.fill").font(.caption)
        }
        .foregroundStyle(.white.opacity(0.6))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Громкость")
        .accessibilityValue("\(Int((value * 100).rounded())) %")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: onChange(min(1, value + 0.1))
            case .decrement: onChange(max(0, value - 0.1))
            @unknown default: break
            }
        }
        .accessibilityIdentifier("nowPlayingVolume")
    }
}
