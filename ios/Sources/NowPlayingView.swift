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

    struct FileArtwork { let trackID: String; let image: UIImage }

    private var artwork: UIImage? {
        guard let track = player.track else { return nil }
        if let image = track.artworkImage { return image }
        if let file = fileArtwork, file.trackID == track.id { return file.image }
        return nil
    }
    private var lookKey: String { PlayerArtworkLook.cacheKey(trackID: player.track?.id ?? "none", image: artwork) }
    /// Distance from the host's safe bottom to the top of the tab bar. Keeps the
    /// last plausible value (keyboard or rotation mid-flight produce outliers).
    private func updateClearance() {
        guard let hostBottom = hostBottom, let tabBottom = tabContentBottom else { return }
        let value = (hostBottom - tabBottom).rounded()
        if value >= 0, value <= 140, abs(value - clearance) > 0.5 { clearance = value }
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            if overlay == .nowPlaying, let track = player.track {
                NowPlayingView(player: player, track: track, artwork: artwork, look: look, namespace: namespace,
                               showLyrics: $showLyrics, slots: slots, onCollapse: { collapse() })
                    .transition(.opacity)
                    .zIndex(2)
            } else if overlay == .miniPlayer, let track = player.track {
                MiniPlayerBar(player: player, track: track, artwork: artwork, namespace: namespace, onExpand: { expand() })
                    .background {
                        GeometryReader { proxy in
                            Color.clear
                                .onAppear { onMiniHeightChange(proxy.size.height) }
                                .onChange(of: proxy.size.height) { _, height in onMiniHeightChange(height) }
                        }
                    }
                    .padding(.bottom, clearance)
                    .transition(.opacity)
                    .zIndex(1)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
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
        .animation(PlayerMotion.expand(reduceMotion), value: overlay)
        .task(id: lookKey) { await refreshLook() }
        .task(id: player.track?.id) { await loadFileArtwork() }
    }

    private func expand() {
        withAnimation(PlayerMotion.expand(reduceMotion)) { setExpanded(true) }
    }
    private func collapse() {
        withAnimation(PlayerMotion.expand(reduceMotion)) { setExpanded(false) }
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
    let onExpand: () -> Void
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
                    .frame(width: 48, height: 48)
                    .playerHero("artwork", in: namespace, enabled: hero)
                    .shadow(color: .black.opacity(0.25), radius: 4, y: 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(track.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text(track.artist).font(.caption).foregroundStyle(.white.opacity(0.65)).lineLimit(1)
                    }
                    .id(track.id)
                    .transition(.playerSlide(direction: slideDirection, distance: 90, reduceMotion: reduceMotion))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .playerHero("title", in: namespace, enabled: hero, properties: .position)
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
            .playerHero("playButton", in: namespace, enabled: hero)
            .accessibilityLabel(player.isPlaying ? "Пауза" : "Слушать")
            .accessibilityIdentifier("miniPlayerPlayPause")
            .disabled(player.isBusy)

            Button { go(1) } label: {
                Image(systemName: "forward.fill").font(.title3).frame(width: 40, height: 44).contentShape(Rectangle())
            }
            .playerHero("nextButton", in: namespace, enabled: hero)
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
                .playerHero("container", in: namespace, enabled: hero)
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

private struct NowPlayingBelowArtworkKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private enum PlayerDragMode { case undecided, active, ignored }

@MainActor struct NowPlayingView<Player: PlayerStateProviding>: View {
    @ObservedObject var player: Player
    let track: PlayerTrack
    let artwork: UIImage?
    let look: PlayerArtworkLook
    let namespace: Namespace.ID
    @Binding var showLyrics: Bool
    let slots: NowPlayingSlots
    let onCollapse: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dragOffset: CGFloat = 0
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
    /// Measured height of title + controls; the artwork gets whatever is left.
    @State private var belowArtworkHeight: CGFloat = 340
    @AccessibilityFocusState private var titleFocused: Bool

    private var hero: Bool { !reduceMotion }

    var body: some View {
        GeometryReader { geometry in
            let height = geometry.size.height + geometry.safeAreaInsets.top + geometry.safeAreaInsets.bottom
            let progress = min(1, max(0, dragOffset / max(1, height)))
            ZStack(alignment: .top) {
                // What shows behind the card while it is dragged down.
                Color.black.opacity(0.5 * (1 - progress)).ignoresSafeArea()
                card(width: geometry.size.width, insets: geometry.safeAreaInsets, radius: min(38, dragOffset * 0.35))
                    .offset(y: dragOffset)
                    .scaleEffect(reduceMotion ? 1 : 1 - progress * 0.08, anchor: .top)
            }
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
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(.isModal)
        .accessibilityAction(.escape) { onCollapse() }
        .accessibilityIdentifier("nowPlayingView")
        .onAppear {
            // VoiceOver: land on the song title once the expand animation settles.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { titleFocused = true }
        }
    }

    private func card(width: CGFloat, insets: EdgeInsets, radius: CGFloat) -> some View {
        VStack(spacing: 0) {
            header
            if showLyrics { lyricsLayout } else { artworkLayout }
        }
        .foregroundStyle(.white)
        .tint(.white)
        .background {
            GeometryReader { proxy in
                // The hero frame IS the full-screen frame (safe area included), so the
                // expand animation ends exactly where the background stays: no jump
                // when a trailing ignoresSafeArea would otherwise resize it.
                NowPlayingBackground(look: look, reduceMotion: reduceMotion)
                    .frame(width: proxy.size.width + insets.leading + insets.trailing,
                           height: proxy.size.height + insets.top + insets.bottom)
                    .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
                    .playerHero("container", in: namespace, enabled: hero)
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

    // MARK: Artwork mode

    /// Fits the screen without scrolling at default Dynamic Type: the artwork
    /// shrinks to the height left after title + controls. Only when even a
    /// minimum artwork does not fit (large text, landscape) does it scroll.
    private var artworkLayout: some View {
        GeometryReader { box in
            let horizontal: CGFloat = 28
            let topPadding: CGFloat = 10, gap: CGFloat = 22, bottomPadding: CGFloat = 16
            let widthLimit = min(box.size.width - horizontal * 2, 420)
            let heightLimit = box.size.height - belowArtworkHeight - topPadding - gap - bottomPadding
            let side = max(120, min(widthLimit, heightLimit))
            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 0) {
                    bigArtwork(side: side)
                        .padding(.top, topPadding)
                    Spacer(minLength: gap)
                    VStack(spacing: 22) {
                        metadata(large: true)
                        controls(compact: false)
                    }
                    .background {
                        GeometryReader { proxy in
                            Color.clear.preference(key: NowPlayingBelowArtworkKey.self, value: proxy.size.height)
                        }
                    }
                }
                .padding(.horizontal, horizontal).padding(.bottom, bottomPadding)
                .frame(maxWidth: 560)
                .frame(maxWidth: .infinity, minHeight: box.size.height, alignment: .top)
                .background(alignment: .topLeading) {
                    PlayerScrollViewTuner { atTop in scrollAtTop = atTop }
                        .frame(width: 1, height: 1).accessibilityHidden(true)
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            .accessibilityIdentifier("nowPlayingScroll")
        }
        .onPreferenceChange(NowPlayingBelowArtworkKey.self) { value in
            if value > 0, abs(value - belowArtworkHeight) > 0.5 { belowArtworkHeight = value }
        }
        .transition(.opacity)
    }

    private func bigArtwork(side: CGFloat) -> some View {
        let playing = player.isPlaying
        return ZStack {
            PlayerArtworkImage(image: artwork, cornerRadius: 12)
                .id(track.id)
                .transition(.playerSlide(direction: slideDirection, distance: side * 0.7, reduceMotion: reduceMotion))
        }
        .frame(width: side, height: side)
        .playerHero("artwork", in: namespace, enabled: hero)
        .scaleEffect(reduceMotion || playing ? 1 : 0.85)
        .shadow(color: .black.opacity(playing ? 0.4 : 0.22), radius: playing ? 28 : 12, y: playing ? 16 : 6)
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

    // MARK: Lyrics mode

    private var lyricsLayout: some View {
        VStack(spacing: 0) {
            HStack(spacing: 14) {
                PlayerArtworkImage(image: artwork, cornerRadius: 8)
                    .frame(width: 60, height: 60)
                    .playerHero("artwork", in: namespace, enabled: hero)
                    .shadow(color: .black.opacity(0.3), radius: 8, y: 4)
                metadata(large: false)
            }
            .padding(.horizontal, 24).padding(.top, 6).padding(.bottom, 4)
            .contentShape(Rectangle())
            .background { zoneReporter }
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
            .transition(.opacity)
            controls(compact: true)
                .padding(.horizontal, 28).padding(.bottom, 10)
                .frame(maxWidth: 560)
        }
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
            .playerHero("title", in: namespace, enabled: hero, properties: .position)
            if let accessory = slots.titleAccessory { accessory() }
        }
        .animation(reduceMotion ? PlayerMotion.fade : PlayerMotion.trackChange, value: track.id)
    }

    private func controls(compact: Bool) -> some View {
        VStack(spacing: compact ? 12 : 20) {
            PlayerScrubber(trackID: track.id, position: player.position, duration: player.duration, disabled: player.isBusy,
                           reduceMotion: reduceMotion, onEditingChanged: { sliderEditing = $0 },
                           onSeek: { value in player.seek(to: value) })
            transport(compact: compact)
            if let status = slots.status { status() }
            if !compact { volume }
            footer
        }
    }

    private func transport(compact: Bool) -> some View {
        HStack(spacing: 0) {
            Spacer(minLength: 0)
            Button { go(-1) } label: {
                Image(systemName: "backward.fill").font(.system(size: compact ? 28 : 34)).frame(width: 64, height: 56).contentShape(Rectangle())
            }
            .accessibilityLabel("Предыдущий трек").accessibilityIdentifier("nowPlayingPrevious")
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
            .playerHero("playButton", in: namespace, enabled: hero)
            .accessibilityLabel(player.isPlaying ? "Пауза" : "Слушать").accessibilityIdentifier("nativePlayerToggle")
            Spacer(minLength: 0)
            Button { go(1) } label: {
                Image(systemName: "forward.fill").font(.system(size: compact ? 28 : 34)).frame(width: 64, height: 56).contentShape(Rectangle())
            }
            .playerHero("nextButton", in: namespace, enabled: hero)
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
                    let inHeader = value.startLocation.y <= headerZone
                    let allowed = showLyrics ? inHeader : (scrollAtTop || inHeader)
                    // A scrubber/volume drag that drifts downward seeks; it never dismisses.
                    dragMode = downward && allowed && !dismissing && !sliderEditing ? .active : .ignored
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
