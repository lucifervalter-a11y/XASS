import SwiftUI
import UIKit

/// Apple Music-style synced lyrics driven only by `PlayerStateProviding`.
/// Autoscroll keeps the current line near one third of the height; a finger
/// scroll pauses it and it resumes after a short idle period.
@MainActor struct TimedLyricsView<Player: PlayerStateProviding>: View {
    @ObservedObject var player: Player
    let reduceMotion: Bool
    @State private var following = true
    @State private var resumeTask: Task<Void, Never>?
    /// Line the user tapped: shown as current until the (possibly async) seek
    /// catches up, so the highlight never bounces back to the old line.
    @State private var heldIndex: Int?
    static var resumeDelay: Duration { .milliseconds(3500) }

    private var displayedIndex: Int? { heldIndex ?? player.currentLyricIndex }

    var body: some View {
        Group {
            if let lyrics = player.lyrics, !lyrics.lines.isEmpty {
                viewport(lyrics)
            } else {
                placeholder
            }
        }
        .onDisappear { resumeTask?.cancel(); resumeTask = nil }
        .onChange(of: player.currentLyricIndex) { _, index in
            if let held = heldIndex, index == held { heldIndex = nil }
        }
        .task(id: heldIndex) {
            // A seek that never lands (remote player, error) must not pin the line forever.
            guard heldIndex != nil else { return }
            try? await Task.sleep(for: .seconds(3))
            if !Task.isCancelled { heldIndex = nil }
        }
    }

    private var placeholder: some View {
        VStack(spacing: 14) {
            Image(systemName: "quote.bubble").font(.system(size: 40, weight: .semibold)).foregroundStyle(.white.opacity(0.45))
            Text("Текста пока нет").font(.title3.weight(.bold)).foregroundStyle(.white)
            Text("Для этой песни нет синхронизированного текста.").font(.callout)
                .foregroundStyle(.white.opacity(0.6)).multilineTextAlignment(.center)
        }
        .padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("nowPlayingLyricsEmpty")
    }

    private func viewport(_ lyrics: TimedLyrics) -> some View {
        GeometryReader { geometry in
            ScrollViewReader { proxy in
                ScrollView(.vertical, showsIndicators: false) {
                    // Equatable: playback ticks do not rebuild the lines, only a new current line does.
                    TimedLyricLinesColumn(lines: lyrics.lines, current: displayedIndex, reduceMotion: reduceMotion,
                                          topInset: geometry.size.height * 0.30, bottomInset: geometry.size.height * 0.62) { index in
                        guard lyrics.lines.indices.contains(index) else { return }
                        PlayerHaptics.tap()
                        heldIndex = index
                        player.seek(to: lyrics.lines[index].start)
                        resumeTask?.cancel(); resumeTask = nil
                        following = true
                        scroll(proxy, to: index, animated: true)
                    }
                    .equatable()
                    .background(alignment: .topLeading) {
                        if #available(iOS 18.0, *) {
                            EmptyView()
                        } else {
                            // iOS 17 fallback: UIKit pan + end of deceleration.
                            PlayerScrollPanObserver(onBegan: userBeganScrolling, onEnded: userEndedScrolling)
                                .frame(width: 1, height: 1).accessibilityHidden(true)
                        }
                    }
                }
                .modifier(LyricsScrollPhaseModifier(onBegan: userBeganScrolling, onEnded: userEndedScrolling))
                .mask {
                    LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.1),
                                           .init(color: .black, location: 0.85), .init(color: .clear, location: 1)],
                                   startPoint: .top, endPoint: .bottom)
                }
                .onAppear {
                    // After the first layout pass, so scrollTo knows the row frames.
                    DispatchQueue.main.async { scroll(proxy, to: player.currentLyricIndex ?? 0, animated: false) }
                }
                .onChange(of: displayedIndex) { _, index in
                    if following { scroll(proxy, to: index, animated: true) }
                }
                .onChange(of: following) { _, enabled in
                    if enabled { scroll(proxy, to: displayedIndex, animated: true) }
                }
                .onChange(of: player.track?.id) { _, _ in
                    resumeTask?.cancel(); resumeTask = nil; following = true; heldIndex = nil
                    DispatchQueue.main.async { scroll(proxy, to: player.currentLyricIndex ?? 0, animated: false) }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("nowPlayingLyrics")
    }

    private func scroll(_ proxy: ScrollViewProxy, to index: Int?, animated: Bool) {
        guard let index = index else { return }
        let anchor = UnitPoint(x: 0.5, y: 0.3)
        if reduceMotion || !animated { proxy.scrollTo(index, anchor: anchor) }
        else { withAnimation(.spring(response: 0.6, dampingFraction: 0.9)) { proxy.scrollTo(index, anchor: anchor) } }
    }

    private func userBeganScrolling() {
        resumeTask?.cancel(); resumeTask = nil
        if following { following = false }
    }

    private func userEndedScrolling() {
        resumeTask?.cancel()
        resumeTask = Task { @MainActor in
            try? await Task.sleep(for: Self.resumeDelay)
            guard !Task.isCancelled else { return }
            following = true
        }
    }
}

/// iOS 18+: pause on a user drag, resume timer starts only when scrolling is
/// fully idle (after deceleration). Programmatic scrollTo reports `.animating`
/// and is ignored.
private struct LyricsScrollPhaseModifier: ViewModifier {
    let onBegan: () -> Void
    let onEnded: () -> Void
    @ViewBuilder func body(content: Content) -> some View {
        if #available(iOS 18.0, *) {
            content.onScrollPhaseChange { old, new in
                switch new {
                case .interacting: onBegan()
                case .idle where old == .interacting || old == .decelerating: onEnded()
                default: break
                }
            }
        } else {
            content
        }
    }
}

struct TimedLyricLinesColumn: View, Equatable {
    let lines: [TimedLyricLine]
    let current: Int?
    let reduceMotion: Bool
    let topInset: CGFloat
    let bottomInset: CGFloat
    let onTap: (Int) -> Void

    static func == (lhs: TimedLyricLinesColumn, rhs: TimedLyricLinesColumn) -> Bool {
        lhs.current == rhs.current && lhs.reduceMotion == rhs.reduceMotion && lhs.topInset == rhs.topInset
            && lhs.bottomInset == rhs.bottomInset && lhs.lines == rhs.lines
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            ForEach(lines.indices, id: \.self) { index in row(index) }
        }
        .padding(.horizontal, 28).padding(.top, topInset).padding(.bottom, bottomInset)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ index: Int) -> some View {
        let isCurrent = index == current
        let distance = current.map { abs(index - $0) } ?? 0
        // Nearby rows move first: a small cascade when the current line changes.
        let animation: Animation? = reduceMotion ? nil : .spring(response: 0.5, dampingFraction: 0.86).delay(Double(min(distance, 6)) * 0.035)
        return Button { onTap(index) } label: {
            Text(lines[index].text)
                .font(.system(.title, design: .default, weight: .bold))
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .foregroundStyle(.white.opacity(isCurrent ? 1 : 0.35))
                .scaleEffect(isCurrent || reduceMotion ? 1 : 0.96, anchor: .leading)
                .blur(radius: isCurrent || reduceMotion ? 0 : min(2.2, 0.8 + Double(distance) * 0.3))
                .animation(animation, value: current)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .id(index)
        .accessibilityIdentifier("lyric-line-\(index)")
        .accessibilityValue(isCurrent ? "Текущая строка" : "")
        .accessibilityHint("Перейти к этой строке в песне")
    }
}

/// Reports the user's own pan on the enclosing UIScrollView. Programmatic
/// `scrollTo` does not trigger it, so autoscroll never pauses itself.
struct PlayerScrollPanObserver: UIViewRepresentable {
    var onBegan: () -> Void
    var onEnded: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .clear
        return view
    }
    func updateUIView(_ view: UIView, context: Context) {
        context.coordinator.onBegan = onBegan
        context.coordinator.onEnded = onEnded
        DispatchQueue.main.async { [weak view] in
            guard let view = view else { return }
            context.coordinator.attach(from: view)
        }
    }
    static func dismantleUIView(_ uiView: UIView, coordinator: Coordinator) { coordinator.detach() }

    final class Coordinator: NSObject {
        var onBegan: () -> Void = {}
        var onEnded: () -> Void = {}
        private weak var scrollView: UIScrollView?

        func attach(from view: UIView) {
            var current: UIView? = view.superview
            while let candidate = current, !(candidate is UIScrollView) { current = candidate.superview }
            guard let found = current as? UIScrollView, found !== scrollView else { return }
            detach()
            found.panGestureRecognizer.addTarget(self, action: #selector(panned(_:)))
            scrollView = found
        }
        func detach() {
            decelerationTimer?.invalidate(); decelerationTimer = nil
            scrollView?.panGestureRecognizer.removeTarget(self, action: #selector(panned(_:)))
            scrollView = nil
        }
        private var decelerationTimer: Timer?
        @objc private func panned(_ gesture: UIPanGestureRecognizer) {
            switch gesture.state {
            case .began:
                decelerationTimer?.invalidate(); decelerationTimer = nil
                onBegan()
            case .ended, .cancelled, .failed:
                // Report the end only once the flick has stopped decelerating.
                decelerationTimer?.invalidate()
                decelerationTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] timer in
                    guard let self = self else { timer.invalidate(); return }
                    if self.scrollView?.isDecelerating != true {
                        timer.invalidate(); self.decelerationTimer = nil
                        self.onEnded()
                    }
                }
            default: break
            }
        }
    }
}
