import SwiftUI
import AVKit

/// Main application shell integrating all XASS native views, Hermes AI Agent, and multi-theme switcher.
@MainActor
public struct ContentView: View {
    @ObservedObject var app: AppState
    @ObservedObject var store: NativeStore
    @StateObject private var hermes: HermesService
    @ObservedObject private var themeManager = ThemeManager.shared
    @StateObject private var workspace: NativeWorkspaceStore
    @StateObject private var storePlayer: NativeStorePlayerState

    @State private var selectedTab: Int = 0
    @State private var showThemeSheet: Bool = false
    @State private var showQueue: Bool = false
    @State private var miniHeight: CGFloat = 64
    @State private var tabContentBottom: CGFloat?

    init(app: AppState, store: NativeStore) {
        self.app = app
        self.store = store
        _hermes = StateObject(wrappedValue: HermesService(api: store.api))
        _workspace = StateObject(wrappedValue: NativeWorkspaceStore(api: store.api))
        _storePlayer = StateObject(wrappedValue: NativeStorePlayerState(store: store))
    }

    /// Convenience initializer for previews, mocks, and testing.
    public init() {
        let defaultApp = AppState()
        let defaultOrigin = defaultApp.origin ?? (try! ServerOrigin("https://preview.xass.invalid"))
        let defaultStore = defaultApp.native ?? NativeStore(api: OwnerAPI(origin: defaultOrigin), audio: defaultApp.audio)
        self.init(app: defaultApp, store: defaultStore)
    }

    private var currentTheme: AppTheme {
        themeManager.currentTheme
    }

    public var body: some View {
        ZStack(alignment: .bottom) {
            tabs
                .accessibilityHidden(store.showPlayer)

            // Mini player layer
            if let track = store.currentTrack, !store.showPlayer {
                miniPlayerBar(track: track)
                    .padding(.bottom, tabContentBottom ?? 49)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .tint(currentTheme.accentColor)
        .preferredColorScheme(.dark)
        .sheet(isPresented: $showThemeSheet) {
            NavigationStack {
                ThemeSelectorView()
                    .toolbar {
                        ToolbarItem(placement: .topBarTrailing) {
                            Button("Готово") {
                                showThemeSheet = false
                            }
                            .font(.headline)
                            .foregroundStyle(currentTheme.accentColor)
                        }
                    }
            }
            .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $store.showPlayer) {
            NowPlayingView(player: storePlayer)
        }
    }

    // MARK: - Tab Navigation

    private var tabs: some View {
        TabView(selection: $selectedTab) {
            tabItemView(
                NativeHomeView(store: store, workspace: workspace),
                tag: 0
            )
            .tabItem {
                Label("Главная", systemImage: "house")
            }
            .tag(0)
            .accessibilityIdentifier("tab-home")
            .accessibilityLabel("Вкладка Главная")

            tabItemView(
                NativeLibraryView(store: store),
                tag: 1
            )
            .tabItem {
                Label("Музыка", systemImage: "music.note")
            }
            .tag(1)
            .accessibilityIdentifier("tab-music")
            .accessibilityLabel("Вкладка Музыка")

            tabItemView(
                HermesView(hermes: hermes, store: store),
                tag: 2
            )
            .tabItem {
                Label("XASS AI", systemImage: "sparkles")
            }
            .tag(2)
            .accessibilityIdentifier("tab-hermes")
            .accessibilityLabel("Вкладка XASS AI")
            .badge(hermes.isThinking ? "●" : nil)

            tabItemView(
                NativeSiteView(store: store, workspace: workspace),
                tag: 3
            )
            .tabItem {
                Label("Сайт", systemImage: "person.crop.square")
            }
            .tag(3)
            .accessibilityIdentifier("tab-site")
            .accessibilityLabel("Вкладка Сайт")

            tabItemView(
                NativeToolsView(app: app, store: store, workspace: workspace),
                tag: 4
            )
            .tabItem {
                Label("Инструменты", systemImage: "square.grid.2x2")
            }
            .tag(4)
            .accessibilityIdentifier("tab-tools")
            .accessibilityLabel("Вкладка Инструменты")

            tabItemView(
                NativeWeatherView(store: store, workspace: workspace),
                tag: 5
            )
            .tabItem {
                Label("Погода", systemImage: "cloud.sun")
            }
            .tag(5)
            .accessibilityIdentifier("tab-weather")
            .accessibilityLabel("Вкладка Погода")
        }
        .overlay(alignment: .topTrailing) {
            themeSwitcherFloatingPill
                .padding(.trailing, 16)
                .padding(.top, 48)
        }
    }

    // MARK: - Floating Theme Selector Pill

    private var themeSwitcherFloatingPill: some View {
        Button {
            showThemeSheet = true
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "paintpalette.fill")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(currentTheme.accentColor)

                Text(currentTheme.displayName)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.white)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(currentTheme.cardColor, in: Capsule())
            .overlay(Capsule().stroke(currentTheme.borderColor, lineWidth: 1))
            .shadow(color: currentTheme.glowingTintColor, radius: 6)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Сменить тему оформления. Текущая: \(currentTheme.displayName)")
        .accessibilityHint("Дважды коснитесь, чтобы открыть выбор цветовых тем")
        .accessibilityIdentifier("themeSelectorButton")
    }

    // MARK: - Mini Player

    private func miniPlayerBar(track: LibraryTrack) -> some View {
        Button {
            store.showPlayer = true
        } label: {
            HStack(spacing: 12) {
                TrackArtwork(store: store, trackID: track.id, radius: 8)
                    .frame(width: 44, height: 44)

                VStack(alignment: .leading, spacing: 3) {
                    Text(track.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)
                        .lineLimit(1)

                    Text(track.artist.isEmpty ? store.deviceLabel : track.artist)
                        .font(.caption)
                        .foregroundStyle(currentTheme.secondaryTextColor)
                        .lineLimit(1)
                }

                Spacer()

                Button {
                    store.run {
                        try await store.togglePlayPause()
                    }
                } label: {
                    Image(systemName: store.playing ? "pause.fill" : "play.fill")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(currentTheme.accentColor)
                        .frame(width: 38, height: 38)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(store.playing ? "Пауза" : "Воспроизведение")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 16)
                    .fill(currentTheme.cardColor)
                    .overlay(RoundedRectangle(cornerRadius: 16).stroke(currentTheme.borderColor, lineWidth: 1))
                    .shadow(color: Color.black.opacity(0.4), radius: 8)
            )
            .padding(.horizontal, 12)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Сейчас играет: \(track.title), \(track.artist)")
    }

    private func tabItemView<V: View>(_ view: V, tag: Int) -> some View {
        view
            .background(currentTheme.backgroundColor.ignoresSafeArea())
    }
}
