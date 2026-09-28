import SwiftUI
import UIKit

/// Root player layer: exactly one of mini player / Now Playing, driven by
/// `NativeRootOverlay`. Not a sheet, so it never stacks on other sheets and the
/// mini player can morph into Now Playing with matchedGeometryEffect.
@MainActor struct NativePlayerOverlayHost<Player: PlayerStateProviding>: View {
    @ObservedObject var player: Player
    let overlay: NativeRootOverlay
    @Binding var showLyrics: Bool
    let slots: NowPlayingSlots
    /// Global maxY of the tab content area (= top of the tab bar, or the safe
    /// bottom when the tab bar is elsewhere, e.g. iPad iOS 18). Measured by the shell.
    let tabContentBottom: CGFloat?
    let setExpanded: (Bool) -> Void
    var onMiniHeightChange: (CGFloat) -> Void = { _ in }
    @Namespace private var namespace
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var look = PlayerArtworkLook.neutral
    @State private var fileArtwork: FileArtwork?
    @State private var hostBottom: CGFloat?
    @State private var clearance: CGFloat = 49
    /// Now Playing is in the hierarchy (while open and for the whole collapse).
    @State private var cardMounted = false
    /// Animated hero state. true: the card is full screen and owns every hero id;
    /// false: the card is clipped to the mini player's frame and the mini owns them.
    @State private var expanded = false
    @State private var dragOffset: CGFloat = 0
    /// Mini player pill in host space, captured only while the mini player is the visible source.
    @State private var miniFrame: CGRect = .zero
    @State private var transitionID = 0
    /// The mini player is only kept under the card while a transition needs it as
    /// hero partner; once Now Playing has settled it is removed entirely.
    @State private var miniMounted = true

    struct FileArtwork { let trackID: String; let image: UIImage }

    private var artwork: UIImage? {
        guard let track = player.track else { return nil }
        if let image = track.artworkImage { return image }
        if let file = fileArtwork, file.trackID == track.id { return file.image }
        return nil
    }
    private var lookKey: String { PlayerArtworkLook.cacheKey(trackID: player.track?.id ?? "none", image: artwork) }
    private var wantsCard: Bool { overlay == .nowPlaying && player.track != nil }
    /// The mini player stays laid out (hidden) under Now Playing, so it is the
    /// hero source on expand and the target frame on collapse.
    private var miniAvailable: Bool { player.track != nil && (overlay == .miniPlayer || overlay == .nowPlaying) }
    /// Hidden instantly while the card is on screen: never crossfaded with it.
    private var miniVisible: Bool { miniAvailable && !cardMounted }

    /// Distance from the host's safe bottom to the top of the tab bar. Keeps the
    /// last plausible value (keyboard or rotation mid-flight produce outliers).
    private func updateClearance() {
        guard let hostBottom = hostBottom, let tabBottom = tabContentBottom else { return }
        let value = (hostBottom - tabBottom).rounded()
        if value >= 0, value <= 140, abs(value - clearance) > 0.5 { clearance = value }
    }

    var body: some View {
        GeometryReader { geometry in
            let fullHeight = geometry.size.height + geometry.safeAreaInsets.top + geometry.safeAreaInsets.bottom
            let dragProgress = min(1, max(0, dragOffset / max(1, fullHeight)))
            ZStack(alignment: .bottom) {
                if miniAvailable, miniMounted || !cardMounted, let track = player.track {
                    MiniPlayerBar(player: player, track: track, artwork: artwork, namespace: namespace,
                                  heroSource: !expanded, onExpand: { expand() },
                                  onFrameChange: { frame in miniFrame = frame })
                        .background {
                            GeometryReader { proxy in
                                Color.clear
                                    .onAppear { onMiniHeightChange(proxy.size.height) }
                                    .onChange(of: proxy.size.height) { _, height in onMiniHeightChange(height) }
                            }
                        }
                        .padding(.bottom, clearance)
                        .opacity(miniVisible ? 1 : 0)
                        .allowsHitTesting(miniVisible)
                        .accessibilityHidden(!miniVisible)
                        .zIndex(1)
                }
                if cardMounted {
                    backdrop(insets: geometry.safeAreaInsets, dragProgress: dragProgress).zIndex(1.5)
                }
                if cardMounted, let track = player.track {
                    NowPlayingView(player: player, track: track, artwork: artwork, look: look, namespace: namespace,
                                   expanded: expanded, dragOffset: $dragOffset,
                                   showLyrics: $showLyrics, slots: slots, onCollapse: { collapse() })
                        // Always fully opaque; only under Reduce Motion the whole card crossfades.
                        .opacity(reduceMotion && !expanded ? 0 : 1)
                        .mask(alignment: .topLeading) { cardMask(size: geometry.size, insets: geometry.safeAreaInsets) }
                        .allowsHitTesting(expanded)
                        .zIndex(2)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
        }
        .coordinateSpace(.named(PlayerHostSpace.name))
        .background {
            GeometryReader { proxy in
                let bottom = proxy.frame(in: .global).maxY
                Color.clear
                    .onAppear { hostBottom = bottom; updateClearance() }
                    .onChange(of: bottom) { _, value in hostBottom = value; updateClearance() }
            }
        }
        .onChange(of: tabContentBottom) { _, _ in updateClearance() }
        .ignoresSafeArea(.keyboard)
        .onAppear { if wantsCard { cardMounted = true; expanded = true; miniMounted = false } }
        .onChange(of: wantsCard) { _, want in
            if want { presentCard() } else { dismissCard(animated: overlay == .miniPlayer || overlay == .transferBanner) }
        }
        .task(id: lookKey) { await refreshLook() }
        .task(id: player.track?.id) { await loadFileArtwork() }
    }

    /// Dims the library behind the card and covers the tab bar at once (no crossfade).
    private func backdrop(insets: EdgeInsets, dragProgress: CGFloat) -> some View {
        ZStack(alignment: .bottom) {
            Color.black.opacity(expanded ? 0.55 * Double(1 - dragProgress) : 0)
            Color.black.frame(height: clearance + insets.bottom)
        }
        .ignoresSafeArea()
        .contentShape(Rectangle())
        .accessibilityHidden(true)
    }

    private func cardMask(size: CGSize, insets: EdgeInsets) -> some View {
        let rect = NowPlayingCardGeometry.rect(expanded: expanded, reduceMotion: reduceMotion, miniFrame: miniFrame,
                                               size: size, insets: insets, dragOffset: dragOffset)
        let radius = NowPlayingCardGeometry.cornerRadius(expanded: expanded, reduceMotion: reduceMotion, dragOffset: dragOffset)
        return RoundedRectangle(cornerRadius: radius, style: .continuous)
            .frame(width: rect.width, height: rect.height)
            .offset(x: rect.minX, y: rect.minY)
    }

    private func expand() { setExpanded(true) }
    private func collapse() { setExpanded(false) }

    private func presentCard() {
        transitionID += 1
        let id = transitionID
        var still = Transaction(); still.disablesAnimations = true
        if !cardMounted {
            // First pass: the card is mounted clipped to the mini player, with the mini as
            // hero source; the mini hides in the same pass, so nothing is ever doubled.
            withTransaction(still) { dragOffset = 0; expanded = false; miniMounted = true; cardMounted = true }
        }
        afterLayout {
            guard id == transitionID, cardMounted else { return }
            withAnimation(PlayerMotion.expand(reduceMotion), completionCriteria: .removed) {
                expanded = true; dragOffset = 0
            } completion: {
                guard id == transitionID, cardMounted else { return }
                miniMounted = false
            }
        }
    }

    private func dismissCard(animated: Bool) {
        transitionID += 1
        let id = transitionID
        guard cardMounted else { miniMounted = true; return }
        var still = Transaction(); still.disablesAnimations = true
        guard animated else {
            withTransaction(still) { cardMounted = false; expanded = false; dragOffset = 0; miniMounted = true }
            return
        }
        // Mount the (hidden) mini player first, so it is laid out as the landing frame
        // and hero partner; then the card springs into it and is removed only once the
        // animation has fully finished. The mini (same frame) then shows at once.
        withTransaction(still) { miniMounted = true }
        afterLayout {
            guard id == transitionID, cardMounted else { return }
            withAnimation(PlayerMotion.expand(reduceMotion), completionCriteria: .removed) {
                expanded = false; dragOffset = 0
            } completion: {
                guard id == transitionID else { return }
                cardMounted = false
            }
        }
    }

    /// Runs after the current update has been laid out and committed.
    private func afterLayout(_ work: @escaping () -> Void) {
        DispatchQueue.main.async { DispatchQueue.main.async { work() } }
    }

    private func refreshLook() async {
        let key = lookKey
        if let hit = PlayerArtworkLookCache.shared.cached(key) { look = hit; return }
        guard let image = artwork else {
            // Artwork usually arrives a moment after the track: keep the old
            // colors meanwhile instead of flashing to neutral and back.
            try? await Task.sleep(for: .milliseconds(700))
            if !Task.isCancelled { look = .neutral }
            return
        }
        let computed = await PlayerArtworkLookCache.shared.look(key: key, image: image)
        if !Task.isCancelled { look = computed }
    }

    private func loadFileArtwork() async {
        guard let track = player.track, track.artworkImage == nil, let url = track.artworkURL, url.isFileURL else { return }
        let image = await Task.detached(priority: .userInitiated) { () -> UIImage? in
            guard let data = try? Data(contentsOf: url) else { return nil }
            return UIImage(data: data)?.preparingForDisplay()
        }.value
        guard !Task.isCancelled, let image = image else { return }
        fileArtwork = FileArtwork(trackID: track.id, image: image)
    }
}

// MARK: - Mini player

@MainActor struct MiniPlayerBar<Player: PlayerStateProviding>: View {
    @ObservedObject var player: Player
    let track: PlayerTrack
    let artwork: UIImage?
    let namespace: Namespace.ID
    /// Owns the hero ids while Now Playing is collapsed (exactly one source per id).
    var heroSource = true
    let onExpand: () -> Void
    /// Pill frame in `PlayerHostSpace`: the collapse target of the Now Playing card.
    var onFrameChange: (CGRect) -> Void = { _ in }
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dragX: CGFloat = 0
    @State private var horizontal: Bool?
    @State private var slideDirection: CGFloat = 1
    @GestureState private var touching = false

    private var hero: Bool { !reduceMotion }

    var body: some View {
        HStack(spacing: 10) {
            Button { PlayerHaptics.tap(); onExpand() } label: {
                HStack(spacing: 12) {
                    ZStack {
                        PlayerArtworkImage(image: artwork, cornerRadius: 8)
                            .id(track.id)
                            .transition(.playerSlide(direction: slideDirection, distance: 56, reduceMotion: reduceMotion))
                    }
                    // Hero inside the fixed frame: the flexible artwork takes the other
                    // side's size, the bar's own layout never changes.
                    .playerHero("artwork", in: namespace, enabled: hero, isSource: heroSource)
                    .frame(width: 48, height: 48)
                    .shadow(color: .black.opacity(0.25), radius: 4, y: 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(track.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text(track.artist).font(.caption).foregroundStyle(.white.opacity(0.65)).lineLimit(1)
                    }
                    .id(track.id)
                    .transition(.playerSlide(direction: slideDirection, distance: 90, reduceMotion: reduceMotion))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .playerHero("title", in: namespace, enabled: hero, isSource: heroSource, properties: .position)
                }
                .offset(x: dragX)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Открыть плеер")
            .accessibilityValue("\(track.title), \(track.artist)")
            .accessibilityHint("Смахните влево или вправо, чтобы сменить трек")
            .accessibilityAction(named: "Следующий трек") { go(1) }
            .accessibilityAction(named: "Предыдущий трек") { go(-1) }
            .accessibilityIdentifier("nativeMiniPlayer")

            Button { PlayerHaptics.tap(); player.togglePlayPause() } label: {
                Image(systemName: player.isPlaying ? "pause.fill" : "play.fill")
                    .font(.title2)
                    .contentTransition(reduceMotion ? .opacity : .symbolEffect(.replace))
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .playerHero("playButton", in: namespace, enabled: hero, isSource: heroSource, properties: .position)
            .accessibilityLabel(player.isPlaying ? "Пауза" : "Слушать")
            .accessibilityIdentifier("miniPlayerPlayPause")
            .disabled(player.isBusy)

            Button { go(1) } label: {
                Image(systemName: "forward.fill").font(.title3).frame(width: 40, height: 44).contentShape(Rectangle())
            }
            .playerHero("nextButton", in: namespace, enabled: hero, isSource: heroSource, properties: .position)
            .accessibilityLabel("Следующий трек")
            .accessibilityIdentifier("miniPlayerNext")
            .disabled(player.isBusy)
        }
        .buttonStyle(MusicPressStyle())
        .foregroundStyle(.white)
        .padding(.leading, 8).padding(.trailing, 10)
        .frame(minHeight: 64)
        .background {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color.black.opacity(0.25)))
                .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Color.white.opacity(0.08)))
                .background {
                    GeometryReader { proxy in
                        let frame = proxy.frame(in: .named(PlayerHostSpace.name))
                        Color.clear
                            .onAppear { onFrameChange(frame) }
                            .onChange(of: frame) { _, value in onFrameChange(value) }
                    }
                }
        }
        .overlay(alignment: .bottom) { MiniPlayerProgress(progress: player.progress).padding(.horizontal, 16).padding(.bottom, 3) }
        .shadow(color: .black.opacity(0.3), radius: 12, y: 4)
        .padding(.horizontal, 10).padding(.bottom, 6)
        .simultaneousGesture(swipe)
        .onChange(of: touching) { _, active in
            guard !active else { return }
            horizontal = nil
            if dragX != 0 { withAnimation(PlayerMotion.trackChange) { dragX = 0 } }
        }
        .animation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange, value: track.id)
    }

    private var swipe: some Gesture {
        DragGesture(minimumDistance: 16)
            .updating($touching) { _, state, _ in state = true }
            .onChanged { value in
                if horizontal == nil { horizontal = abs(value.translation.width) > abs(value.translation.height) }
                guard horizontal == true, !player.isBusy else { return }
                dragX = reduceMotion ? 0 : value.translation.width * 0.6
            }
            .onEnded { value in
                defer { horizontal = nil }
                guard horizontal == true, !player.isBusy else { return }
                let dx = value.translation.width, vx = value.velocity.width
                if dx < -60 || vx < -600 { go(1) } else if dx > 60 || vx > 600 { go(-1) }
                withAnimation(PlayerMotion.trackChange) { dragX = 0 }
            }
    }

    private func go(_ direction: Int) {
        guard !player.isBusy else { return }
        PlayerHaptics.tap()
        slideDirection = direction > 0 ? 1 : -1
        // One render with the new direction first, so the outgoing item leaves the right way.
        DispatchQueue.main.async {
            withAnimation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange) {
                if direction > 0 { player.next() } else { player.previous() }
            }
        }
    }
}

private struct MiniPlayerProgress: View {
    let progress: Double
    var body: some View {
        GeometryReader { geometry in
            Capsule().fill(Color.white.opacity(0.14))
                .overlay(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.7)).frame(width: geometry.size.width * progress)
                }
        }
        .frame(height: 2)
        .accessibilityHidden(true)
    }
}

// MARK: - Now Playing

private struct NowPlayingHeaderZoneKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// Heights measured in artwork mode; they size the artwork so the screen fits without scrolling.
private struct NowPlayingMetadataHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private struct NowPlayingControlsHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private enum PlayerDragMode { case undecided, active, ignored }

/// One layout for artwork and lyrics mode: the SAME artwork, title and control
/// views move and resize (AnyLayout + conditional frames), nothing is duplicated
/// or crossfaded. Only the lyrics list / volume row are inserted.
@MainActor struct NowPlayingView<Player: PlayerStateProviding>: View {
    @ObservedObject var player: Player
    let track: PlayerTrack
    let artwork: UIImage?
    let look: PlayerArtworkLook
    let namespace: Namespace.ID
    /// Host hero state: true once the card is full screen (card owns the hero ids).
    let expanded: Bool
    /// Owned by the host so the card mask follows the finger and collapses from there.
    @Binding var dragOffset: CGFloat
    @Binding var showLyrics: Bool
    let slots: NowPlayingSlots
    let onCollapse: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dragMode = PlayerDragMode.undecided
    @GestureState private var dragging = false
    @State private var dismissing = false
    @State private var scrollAtTop = true
    @State private var headerZone: CGFloat = 140
    @State private var artworkDragX: CGFloat = 0
    @State private var artworkMode = PlayerDragMode.undecided
    @GestureState private var artworkTouching = false
    @State private var slideDirection: CGFloat = 1
    /// A scrubber/volume drag is in progress: never start a swipe-down meanwhile.
    @State private var sliderEditing = false
    @State private var metadataHeight: CGFloat = 64
    @State private var controlsHeight: CGFloat = 276
    @AccessibilityFocusState private var titleFocused: Bool

    private var hero: Bool { !reduceMotion }
    /// Non-hero chrome (header, scrubber, volume, footer…) fades with the expand;
    /// hero views (artwork, title, play, next) stay opaque and fly.
    private var chromeOpacity: Double { reduceMotion || expanded ? 1 : 0 }

    var body: some View {
        GeometryReader { geometry in
            let height = geometry.size.height + geometry.safeAreaInsets.top + geometry.safeAreaInsets.bottom
            card(insets: geometry.safeAreaInsets)
                .offset(y: dragOffset)
                .simultaneousGesture(dismissGesture(height: height))
                .onChange(of: dragging) { _, active in
                    guard !active else { return }
                    dragMode = .undecided
                    if !dismissing && dragOffset > 0 { springBack() }
                }
        }
        .onPreferenceChange(NowPlayingHeaderZoneKey.self) { value in
            if value > 0, dragOffset == 0 { headerZone = value }
        }
        .onChange(of: expanded) { _, value in if value { dismissing = false } }
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(.isModal)
        .accessibilityAction(.escape) { onCollapse() }
        .accessibilityIdentifier("nowPlayingView")
        .onAppear {
            // VoiceOver: land on the song title once the expand animation settles.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { titleFocused = true }
        }
    }

    private func card(insets: EdgeInsets) -> some View {
        VStack(spacing: 0) {
            header.opacity(chromeOpacity)
            content
        }
        .foregroundStyle(.white)
        .tint(.white)
        .background {
            GeometryReader { proxy in
                // Solid base under the blurred artwork: the card is never see-through.
                // The host clips it (mini frame -> full screen), so no clip here.
                ZStack {
                    Color.black
                    NowPlayingBackground(look: look, reduceMotion: reduceMotion)
                }
                .frame(width: proxy.size.width + insets.leading + insets.trailing,
                       height: proxy.size.height + insets.top + insets.bottom)
                .padding(EdgeInsets(top: -insets.top, leading: -insets.leading, bottom: -insets.bottom, trailing: -insets.trailing))
            }
        }
    }

    private var header: some View {
        VStack(spacing: 4) {
            Capsule().fill(Color.white.opacity(0.45)).frame(width: 38, height: 5).padding(.top, 6)
                .accessibilityHidden(true)
            HStack {
                Button { PlayerHaptics.tap(); onCollapse() } label: {
                    Image(systemName: "chevron.down").font(.title3.weight(.semibold)).frame(width: 44, height: 36).contentShape(Rectangle())
                }
                .accessibilityLabel("Свернуть плеер").accessibilityIdentifier("nowPlayingCollapse")
                Spacer()
                Text(showLyrics ? "Текст песни" : slots.sourceLabel).font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.65))
                Spacer()
                if let onMore = slots.onMore {
                    Button { PlayerHaptics.tap(); onMore() } label: {
                        Image(systemName: "ellipsis").font(.title3).frame(width: 44, height: 36).contentShape(Rectangle())
                    }
                    .accessibilityLabel("Действия с треком").accessibilityIdentifier("nativePlayerMore")
                } else {
                    Color.clear.frame(width: 44, height: 36)
                }
            }
            .buttonStyle(MusicPressStyle())
        }
        .padding(.horizontal, 16)
        .contentShape(Rectangle())
        .background { zoneReporter }
    }

    private var zoneReporter: some View {
        GeometryReader { proxy in
            Color.clear.preference(key: NowPlayingHeaderZoneKey.self, value: proxy.frame(in: .global).maxY)
        }
    }

    // MARK: Single layout

    /// Artwork mode fits the screen without scrolling at default Dynamic Type:
    /// the artwork gets the height left after title + controls; only when even a
    /// minimum artwork does not fit (large text, landscape) does it scroll.
    /// Lyrics mode pins the same views to one screen with the lyrics in between.
    private var content: some View {
        GeometryReader { box in
            let topPadding: CGFloat = showLyrics ? 6 : 10
            let gap: CGFloat = 22, bottomPadding: CGFloat = showLyrics ? 10 : 16
            let widthLimit = min(box.size.width - 56, 420)
            let heightLimit = box.size.height - metadataHeight - controlsHeight - topPadding - gap - 22 - bottomPadding
            let side = max(120, min(widthLimit, heightLimit))
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 0) {
                    topGroup(side: side, gap: gap)
                        .padding(.horizontal, showLyrics ? 24 : 28)
                        .padding(.top, topPadding)
                        .padding(.bottom, showLyrics ? 4 : 0)
                        .frame(maxWidth: showLyrics ? 640 : 560)
                    if showLyrics {
                        lyricsRegion.transition(.opacity)
                    } else {
                        Spacer(minLength: 22)
                    }
                    controls(compact: showLyrics)
                        .padding(.horizontal, 28)
                        .frame(maxWidth: 560)
                }
                .padding(.bottom, bottomPadding)
                .frame(maxWidth: .infinity)
                // Lyrics: exactly one screen (the lyrics list scrolls itself).
                .frame(height: showLyrics ? box.size.height : nil)
                .frame(minHeight: box.size.height, alignment: .top)
                .background(alignment: .topLeading) {
                    PlayerScrollViewTuner { atTop in scrollAtTop = atTop }
                        .frame(width: 1, height: 1).accessibilityHidden(true)
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            // While the card is dragged the content never scrolls: it moves only once.
            .scrollDisabled(showLyrics || dragMode == .active)
            .accessibilityIdentifier("nowPlayingScroll")
        }
        .onPreferenceChange(NowPlayingMetadataHeightKey.self) { value in
            if value > 0, abs(value - metadataHeight) > 0.5 { metadataHeight = value }
        }
        .onPreferenceChange(NowPlayingControlsHeightKey.self) { value in
            if value > 0, abs(value - controlsHeight) > 0.5 { controlsHeight = value }
        }
    }

    /// Artwork + title: vertical in artwork mode, one row in lyrics mode. Same views.
    private func topGroup(side: CGFloat, gap: CGFloat) -> some View {
        let layout = showLyrics ? AnyLayout(HStackLayout(alignment: .center, spacing: 14))
                                : AnyLayout(VStackLayout(alignment: .center, spacing: gap))
        return layout {
            artworkView(side: showLyrics ? 60 : side)
            metadata(large: !showLyrics)
                .background {
                    GeometryReader { proxy in
                        Color.clear.preference(key: NowPlayingMetadataHeightKey.self, value: showLyrics ? 0 : proxy.size.height)
                    }
                }
        }
        .contentShape(Rectangle())
        // Lyrics scroll on their own: there the artwork/title row also starts a dismiss.
        .background { if showLyrics { zoneReporter } }
    }

    private func artworkView(side: CGFloat) -> some View {
        let playing = player.isPlaying
        let small = showLyrics
        let shadowOpacity: Double = small ? 0.3 : (playing ? 0.4 : 0.22)
        let shadowRadius: CGFloat = small ? 8 : (playing ? 28 : 12)
        let shadowY: CGFloat = small ? 4 : (playing ? 16 : 6)
        let cornerRadius: CGFloat = small ? 8 : 12
        let scale: CGFloat = reduceMotion || playing || small || !expanded ? 1 : 0.85
        return ZStack {
            PlayerArtworkImage(image: artwork, cornerRadius: cornerRadius)
                .id(track.id)
                .transition(.playerSlide(direction: slideDirection, distance: side * 0.7, reduceMotion: reduceMotion))
        }
        // Hero INSIDE the fixed frame: the flexible artwork takes the mini player's
        // size while collapsed and grows to `side`, the layout itself stays put.
        .playerHero("artwork", in: namespace, enabled: hero, isSource: expanded)
        .frame(width: side, height: side)
        .scaleEffect(scale)
        .shadow(color: .black.opacity(shadowOpacity), radius: shadowRadius, y: shadowY)
        .offset(x: artworkDragX)
        .animation(reduceMotion ? nil : .spring(response: 0.45, dampingFraction: 0.72), value: playing)
        .animation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange, value: track.id)
        .contentShape(Rectangle())
        .simultaneousGesture(artworkSwipe(side: side))
        .onChange(of: artworkTouching) { _, active in
            guard !active else { return }
            artworkMode = .undecided
            if artworkDragX != 0 { withAnimation(PlayerMotion.trackChange) { artworkDragX = 0 } }
        }
        .accessibilityElement()
        .accessibilityLabel("Обложка: \(track.title)")
        .accessibilityHint("Смахните влево или вправо, чтобы сменить трек")
        .accessibilityAction(named: "Следующий трек") { go(1) }
        .accessibilityAction(named: "Предыдущий трек") { go(-1) }
        .accessibilityIdentifier("nowPlayingArtwork")
    }

    private func artworkSwipe(side: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 14)
            .updating($artworkTouching) { _, state, _ in state = true }
            .onChanged { value in
                if artworkMode == .undecided {
                    artworkMode = abs(value.translation.width) > abs(value.translation.height) ? .active : .ignored
                }
                guard artworkMode == .active, !player.isBusy, !reduceMotion else { return }
                artworkDragX = value.translation.width
            }
            .onEnded { value in
                defer { artworkMode = .undecided }
                guard artworkMode == .active, !player.isBusy else { return }
                let dx = value.translation.width, vx = value.velocity.width
                if dx < -side * 0.22 || vx < -700 { go(1) } else if dx > side * 0.22 || vx > 700 { go(-1) }
                withAnimation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange) { artworkDragX = 0 }
            }
    }

    private var lyricsRegion: some View {
        Group {
            if player.lyrics == nil, let custom = slots.lyrics {
                // Legacy lyrics view (no synced lines for this song): keep its first line clear of the header row.
                custom().padding(.horizontal, 20).padding(.top, 10)
                    .mask {
                        LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.06),
                                               .init(color: .black, location: 1)], startPoint: .top, endPoint: .bottom)
                    }
            }
            else { TimedLyricsView(player: player, reduceMotion: reduceMotion) }
        }
        .frame(maxWidth: 640, maxHeight: .infinity)
        .opacity(chromeOpacity)
    }

    // MARK: Shared pieces

    private func metadata(large: Bool) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: large ? 4 : 2) {
                Text(track.title).font(large ? .title2.weight(.bold) : .headline).lineLimit(large ? 2 : 1)
                Text(track.artist).font(large ? .title3 : .subheadline).foregroundStyle(.white.opacity(0.65)).lineLimit(1)
            }
            .accessibilityElement(children: .combine)
            .accessibilityFocused($titleFocused)
            .id(track.id)
            .transition(.opacity)
            .frame(maxWidth: .infinity, alignment: .leading)
            .playerHero("title", in: namespace, enabled: hero, isSource: expanded, properties: .position)
            if let accessory = slots.titleAccessory { accessory().opacity(chromeOpacity) }
        }
        .animation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange, value: track.id)
    }

    /// One control stack for both modes; compact only changes sizes and hides the volume row.
    private func controls(compact: Bool) -> some View {
        VStack(spacing: compact ? 12 : 20) {
            PlayerScrubber(trackID: track.id, position: player.position, duration: player.duration, disabled: player.isBusy,
                           reduceMotion: reduceMotion, onEditingChanged: { sliderEditing = $0 },
                           onSeek: { value in player.seek(to: value) })
                .opacity(chromeOpacity)
            transport(compact: compact)
            if let status = slots.status { status().opacity(chromeOpacity) }
            if !compact { volume.opacity(chromeOpacity).transition(.opacity) }
            footer.opacity(chromeOpacity)
        }
        .background {
            GeometryReader { proxy in
                Color.clear.preference(key: NowPlayingControlsHeightKey.self, value: compact ? 0 : proxy.size.height)
            }
        }
    }

    private func transport(compact: Bool) -> some View {
        HStack(spacing: 0) {
            Spacer(minLength: 0)
            Button { go(-1) } label: {
                Image(systemName: "backward.fill").font(.system(size: compact ? 28 : 34)).frame(width: 64, height: 56).contentShape(Rectangle())
            }
            .accessibilityLabel("Предыдущий трек").accessibilityIdentifier("nowPlayingPrevious")
            .opacity(chromeOpacity)
            Spacer(minLength: 0)
            Button {
                PlayerHaptics.tap()
                player.togglePlayPause()
            } label: {
                Image(systemName: player.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: compact ? 40 : 50))
                    .contentTransition(reduceMotion ? .opacity : .symbolEffect(.replace))
                    .frame(width: 76, height: 76).contentShape(Rectangle())
            }
            // While collapsed the big glyph sits exactly on the mini one at its size.
            .scaleEffect(reduceMotion || expanded ? 1 : 0.5)
            .playerHero("playButton", in: namespace, enabled: hero, isSource: expanded, properties: .position)
            .accessibilityLabel(player.isPlaying ? "Пауза" : "Слушать").accessibilityIdentifier("nativePlayerToggle")
            Spacer(minLength: 0)
            Button { go(1) } label: {
                Image(systemName: "forward.fill").font(.system(size: compact ? 28 : 34)).frame(width: 64, height: 56).contentShape(Rectangle())
            }
            .scaleEffect(reduceMotion || expanded ? 1 : 0.6)
            .playerHero("nextButton", in: namespace, enabled: hero, isSource: expanded, properties: .position)
            .accessibilityLabel("Следующий трек").accessibilityIdentifier("nowPlayingNext")
            Spacer(minLength: 0)
        }
        .buttonStyle(MusicPressStyle())
        .disabled(player.isBusy)
    }

    @ViewBuilder private var volume: some View {
        if let custom = slots.volume { custom() }
        else {
            PlayerVolumeSlider(volume: player.volume, reduceMotion: reduceMotion,
                               onEditingChanged: { sliderEditing = $0 }, onChange: { player.setVolume($0) })
        }
    }

    private var footer: some View {
        HStack(alignment: .center) {
            Button { PlayerHaptics.tap(); toggleLyrics() } label: {
                Image(systemName: showLyrics ? "quote.bubble.fill" : "quote.bubble").font(.title3)
                    .frame(width: 48, height: 44)
                    .background(showLyrics ? Color.white.opacity(0.18) : Color.clear, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            .accessibilityLabel(showLyrics ? "Показать обложку" : "Текст песни").accessibilityIdentifier("nativePlayerLyrics")
            Spacer()
            if let onDevices = slots.onDevices {
                Button { PlayerHaptics.tap(); onDevices() } label: {
                    VStack(spacing: 4) {
                        Image(systemName: slots.deviceSymbol).font(.title3).frame(height: 26)
                        Text(slots.deviceLabel).font(.caption2).lineLimit(1)
                    }
                    .frame(minWidth: 100, minHeight: 44)
                }
                .accessibilityLabel("Где слушать").accessibilityValue(slots.deviceLabel).accessibilityIdentifier("nativePlayerDevices")
            }
            Spacer()
            if let onQueue = slots.onQueue {
                Button { PlayerHaptics.tap(); onQueue() } label: {
                    Image(systemName: "list.bullet").font(.title3).frame(width: 48, height: 44)
                }
                .accessibilityLabel("Очередь").accessibilityIdentifier("nativePlayerQueue")
            } else {
                Color.clear.frame(width: 48, height: 44)
            }
        }
        .foregroundStyle(.white.opacity(0.85))
        .buttonStyle(MusicPressStyle())
    }

    private func toggleLyrics() {
        withAnimation(reduceMotion ? PlayerMotion.fade : PlayerMotion.hero) { showLyrics.toggle() }
    }

    private func go(_ direction: Int) {
        guard !player.isBusy else { return }
        PlayerHaptics.tap()
        slideDirection = direction > 0 ? 1 : -1
        DispatchQueue.main.async {
            withAnimation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange) {
                if direction > 0 { player.next() } else { player.previous() }
            }
        }
    }

    // MARK: Interactive dismiss

    private func dismissGesture(height: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 10, coordinateSpace: .global)
            .updating($dragging) { _, state, _ in state = true }
            .onChanged { value in
                if dragMode == .undecided {
                    let downward = value.translation.height > 0 && value.translation.height > abs(value.translation.width) * 1.2
                    // Lyrics scroll on their own: there only the header/artwork row starts a dismiss.
                    // Artwork mode: only when the content is exactly at its top (or from the header).
                    let inHeader = value.startLocation.y <= headerZone
                    let allowed = showLyrics ? inHeader : (scrollAtTop || inHeader)
                    // A scrubber/volume drag that drifts downward seeks; it never dismisses.
                    dragMode = downward && allowed && expanded && !dismissing && !sliderEditing ? .active : .ignored
                }
                guard dragMode == .active else { return }
                dragOffset = max(0, value.translation.height)
            }
            .onEnded { value in
                guard dragMode == .active else { dragMode = .undecided; return }
                dragMode = .undecided
                let fast = value.velocity.height > 900 && dragOffset > 20
                if dragOffset > height * 0.25 || fast {
                    dismissing = true
                    PlayerHaptics.dismiss()
                    onCollapse()
                } else {
                    springBack()
                }
            }
    }

    private func springBack() {
        withAnimation(reduceMotion ? PlayerMotion.fade : .spring(response: 0.4, dampingFraction: 0.82)) { dragOffset = 0 }
    }
}
