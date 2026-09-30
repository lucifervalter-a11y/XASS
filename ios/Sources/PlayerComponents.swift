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
    /// Opens the existing trusted metadata / PC transcription flow directly
    /// from the lyrics surface. The generic player never performs network work.
    var onLyricsTools: (() -> Void)?
    var sourceLabel: String = "Моя музыка"
}

extension View {
    /// matchedGeometryEffect that switches off under Reduce Motion. Mini player and
    /// Now Playing are both mounted during a transition, so every id must have
    /// exactly one source: mini `isSource: !expanded`, card `isSource: expanded`.
    @ViewBuilder func playerHero(_ id: String, in namespace: Namespace.ID, enabled: Bool, isSource: Bool = true,
                                 properties: MatchedGeometryProperties = .frame) -> some View {
        if enabled { matchedGeometryEffect(id: id, in: namespace, properties: properties, isSource: isSource) } else { self }
    }
}

/// Coordinate space of the root player layer (mini player frame -> card mask).
enum PlayerHostSpace { static let name = "xass.playerHost" }

/// Where the Now Playing card is clipped to, in the player host's coordinates.
/// Collapsed: exactly the mini player's pill; expanded: the full screen (safe
/// areas included) following the swipe-down. Reduce Motion never morphs.
enum NowPlayingCardGeometry {
    static func rect(expanded: Bool, reduceMotion: Bool, miniFrame: CGRect, size: CGSize, insets: EdgeInsets,
                     dragOffset: CGFloat) -> CGRect {
        let full = CGRect(x: -insets.leading, y: -insets.top + max(0, dragOffset),
                          width: size.width + insets.leading + insets.trailing,
                          height: size.height + insets.top + insets.bottom)
        if expanded || reduceMotion { return full }
        if miniFrame.width > 1, miniFrame.height > 1, miniFrame.minX.isFinite, miniFrame.minY.isFinite { return miniFrame }
        // No mini player to land on (e.g. a transfer banner): slide below the screen.
        return full.offsetBy(dx: 0, dy: full.height + insets.bottom - max(0, dragOffset))
    }

    static func cornerRadius(expanded: Bool, reduceMotion: Bool, dragOffset: CGFloat) -> CGFloat {
        if expanded || reduceMotion { return min(38, max(0, dragOffset) * 0.35) }
        return 16
    }

    /// Exactly one side owns each hero id at any time.
    static func heroSources(expanded: Bool) -> (mini: Bool, card: Bool) { (!expanded, expanded) }
}

enum PlayerDismissPolicy {
    static func canBegin(downward: Bool, allowedRegion: Bool, expanded: Bool,
                         gestureArmed: Bool, dismissing: Bool, sliderEditing: Bool) -> Bool {
        downward && allowedRegion && expanded && gestureArmed && !dismissing && !sliderEditing
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
/// cached bitmap. On a track change the new look fades in ON TOP of the old,
/// fully opaque one (gradients are not interpolated), so there is no dip or flash.
struct NowPlayingBackground: View {
    let look: PlayerArtworkLook
    let reduceMotion: Bool
    @State private var previous: PlayerArtworkLook?
    @State private var topOpacity: Double = 1

    var body: some View {
        ZStack {
            if let previous = previous { NowPlayingBackgroundLayer(look: previous) }
            NowPlayingBackgroundLayer(look: look).opacity(topOpacity)
        }
        .onChange(of: look) { old, _ in
            previous = old
            topOpacity = 0
            withAnimation(reduceMotion ? PlayerMotion.fade : PlayerMotion.background) {
                topOpacity = 1
            } completion: {
                previous = nil
            }
        }
        .accessibilityHidden(true)
    }
}

private struct NowPlayingBackgroundLayer: View {
    let look: PlayerArtworkLook
    var body: some View {
        ZStack {
            LinearGradient(colors: look.colors, startPoint: .top, endPoint: .bottom)
            if let blurred = look.blurred {
                Color.clear
                    .overlay { Image(uiImage: blurred).resizable().scaledToFill().scaleEffect(1.25) }
                    .clipped()
                    .opacity(0.62)
            }
            LinearGradient(colors: look.colors.map { $0.opacity(0.55) }, startPoint: .topLeading, endPoint: .bottomTrailing)
            LinearGradient(colors: [.black.opacity(0.05), .black.opacity(0.42)], startPoint: .top, endPoint: .bottom)
        }
        .drawingGroup()
    }
}

/// Apple Music-style capsule scrubber. Selection haptics while dragging.
struct PlayerScrubber: View {
    let trackID: String
    let position: TimeInterval
    let duration: TimeInterval
    let disabled: Bool
    let reduceMotion: Bool
    /// True while the finger is on the scrubber: Now Playing must not start a swipe-down.
    var onEditingChanged: (Bool) -> Void = { _ in }
    let onSeek: (TimeInterval) -> Void
    @State private var dragValue: TimeInterval?
    @State private var pendingSeek: TimeInterval?
    @State private var hapticStep = -1
    @GestureState private var touching = false

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
                .gesture(DragGesture(minimumDistance: 0).updating($touching) { _, state, _ in state = true }.onChanged { value in
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
        .onChange(of: trackID) { _, _ in
            // A new song never inherits the previous song's pending seek or drag.
            pendingSeek = nil; dragValue = nil; hapticStep = -1
        }
        .onChange(of: touching) { _, active in
            onEditingChanged(active)
            // onEnded has already committed the seek; this only cleans up a cancelled drag.
            if !active { DispatchQueue.main.async { if dragValue != nil { dragValue = nil; hapticStep = -1 } } }
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
    var onEditingChanged: (Bool) -> Void = { _ in }
    let onChange: (Double) -> Void
    @State private var dragValue: Double?
    @GestureState private var touching = false

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
                .gesture(DragGesture(minimumDistance: 0).updating($touching) { _, state, _ in state = true }.onChanged { gesture in
                    let next = min(1, max(0, gesture.location.x / width))
                    dragValue = next; onChange(next)
                }.onEnded { _ in dragValue = nil })
            }
            .frame(height: 30)
            .animation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.8), value: dragValue == nil)
            Image(systemName: "speaker.wave.3.fill").font(.caption)
        }
        .onChange(of: touching) { _, active in
            onEditingChanged(active)
            if !active { dragValue = nil }
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

/// Tunes the enclosing UIScrollView of Now Playing: no rubber-band bounce (a
/// swipe-down must move only the card) and an exact "offset == 0" signal, so
/// the dismiss gesture only starts when the content is scrolled to the top.
struct PlayerScrollViewTuner: UIViewRepresentable {
    var onAtTopChange: (Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .clear
        return view
    }
    func updateUIView(_ view: UIView, context: Context) {
        context.coordinator.onAtTopChange = onAtTopChange
        DispatchQueue.main.async { [weak view] in
            guard let view = view else { return }
            context.coordinator.attach(from: view)
        }
    }
    static func dismantleUIView(_ uiView: UIView, coordinator: Coordinator) { coordinator.detach() }

    final class Coordinator: NSObject {
        var onAtTopChange: (Bool) -> Void = { _ in }
        private weak var scrollView: UIScrollView?
        private var observation: NSKeyValueObservation?
        private var atTop = true

        func attach(from view: UIView) {
            var current: UIView? = view.superview
            while let candidate = current, !(candidate is UIScrollView) { current = candidate.superview }
            guard let found = current as? UIScrollView, found !== scrollView else { return }
            detach()
            scrollView = found
            found.bounces = false
            found.alwaysBounceVertical = false
            observation = found.observe(\.contentOffset, options: [.initial, .new]) { [weak self] scroll, _ in
                // SwiftUI may re-apply its own bounce settings on update: keep them off.
                if scroll.bounces { scroll.bounces = false }
                if scroll.alwaysBounceVertical { scroll.alwaysBounceVertical = false }
                let top = scroll.contentOffset.y <= -scroll.adjustedContentInset.top + 0.5
                DispatchQueue.main.async { self?.report(top) }
            }
        }
        private func report(_ top: Bool) {
            guard top != atTop else { return }
            atTop = top
            onAtTopChange(top)
        }
        func detach() {
            observation?.invalidate(); observation = nil
            scrollView = nil
        }
    }
}
