import SwiftUI
import UniformTypeIdentifiers

enum XASSStyle {
    static let background = Color.black
    static let surface = Color(red: 22 / 255, green: 25 / 255, blue: 30 / 255)
    static let accent = Color(red: 52 / 255, green: 120 / 255, blue: 246 / 255)
    static let secondary = Color(red: 0.67, green: 0.69, blue: 0.73)
}

@MainActor struct TrackArtwork: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    var radius: Double = 8
    var large = false
    @State private var image: UIImage?
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: radius).fill(large ? Color(red: 0.13, green: 0.15, blue: 0.18) : XASSStyle.surface)
            if let image = image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: "music.note").font(.system(size: large ? 64 : 24, weight: .medium)).foregroundStyle(XASSStyle.secondary) }
        }.aspectRatio(1, contentMode: .fit).clipShape(RoundedRectangle(cornerRadius: radius))
            .accessibilityHidden(true)
            .task(id: "\(trackID)-\(store.artworkRevision)") {
                image = nil
                let loaded = await store.artwork(trackID)
                guard !Task.isCancelled else { return }
                image = loaded
            }
    }
}

@MainActor struct NativeMessage: View {
    @ObservedObject var store: NativeStore
    @State private var showDiagnostics = false
    @State private var confirmRecovery = false
    var body: some View {
        if let text = store.error ?? store.notice ?? (store.canRecoverPlayback ? "Прежний плеер давно не отвечает. Если музыка на нём остановлена, можно сбросить зависшую сессию." : nil) {
          VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: store.error == nil ? "checkmark.circle" : "exclamationmark.circle").foregroundStyle(store.error == nil ? Color.green : Color.orange)
                Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { store.error = nil; store.notice = nil } label: { Image(systemName: "xmark").frame(width: 24, height: 24) }.accessibilityLabel("Скрыть сообщение")
            }
            if store.error != nil {
                Button { showDiagnostics = true } label: { Label("Отправить диагностику", systemImage: "square.and.arrow.up") }
                    .font(.subheadline).buttonStyle(.borderless).accessibilityIdentifier("musicErrorDiagnostics")
            }
            if store.canRecoverPlayback {
                Button("Сбросить зависший плеер") { confirmRecovery = true }
                    .font(.subheadline).buttonStyle(.borderless).disabled(store.busy).accessibilityIdentifier("recoverStaleMusic")
            }
          }.padding(14).background(XASSStyle.surface, in: RoundedRectangle(cornerRadius: 14)).accessibilityElement(children: .contain)
                .sheet(isPresented: $showDiagnostics) {
                    NavigationStack {
                        NativeDiagnosticLogView().toolbar {
                            ToolbarItem(placement: .confirmationAction) { Button("Готово") { showDiagnostics = false } }
                        }
                    }
                }
                .confirmationDialog("Музыка на прежнем устройстве остановлена?", isPresented: $confirmRecovery, titleVisibility: .visible) {
                    Button("Да, звук остановлен — сбросить сессию") { store.run { try await store.recoverStalePlayback() } }
                    Button("Отмена", role: .cancel) {}
                } message: {
                    Text("Закройте старый плеер или остановите музыку на нём. Сервер не может выключить звук на устройстве без связи. После сброса старый плеер потеряет управление сессией.")
                }
        }
    }
}

@MainActor struct NativeTrackRow: View {
    @ObservedObject var store: NativeStore
    let track: LibraryTrack
    var rows: [LibraryTrack]
    var menu: () -> Void
    var body: some View {
        HStack(spacing: 12) {
            Button { store.run { try await store.play(track, rows: rows) } } label: {
                HStack(spacing: 12) {
                    TrackArtwork(store: store, trackID: track.id).frame(width: 44, height: 44)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(track.title).font(.body).foregroundStyle(.white).lineLimit(2)
                        Text(track.artist.isEmpty ? "Моя коллекция" : track.artist).font(.subheadline).foregroundStyle(XASSStyle.secondary).lineLimit(1)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(store.busy).accessibilityIdentifier("track-\(track.id)")
            Text(NativeValue.time(track.duration)).font(.caption).monospacedDigit().foregroundStyle(XASSStyle.secondary)
            Button(action: menu) { Image(systemName: "ellipsis").font(.title3).frame(width: 36, height: 44).contentShape(Rectangle()) }
                .buttonStyle(.plain).foregroundStyle(.white).accessibilityLabel("Действия: \(track.title)")
        }.padding(.leading, 4).padding(.trailing, 4).padding(.vertical, 9)
    }
}

@MainActor struct NativeLibraryView: View {
    @ObservedObject var store: NativeStore
    @State private var query = ""
    @State private var filter = "all"
    @State private var importFiles = false
    @State private var selectedTrack: LibraryTrack?
    @State private var editingPlaylist = false
    @State private var playlistToEdit: LibraryPlaylist?
    @State private var searchTask: Task<Void, Never>?
    @State private var collectionRoute: String?
    var body: some View {
        NavigationStack {
            List {
                if !store.authorized {
                    NativeLoginPrompt(store: store).listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                if store.error != nil || store.notice != nil || store.canRecoverPlayback { NativeMessage(store: store).listRowBackground(Color.clear).listRowSeparator(.hidden) }
                HStack(spacing: 12) {
                    // Multiple NavigationLinks in one List row activate together on iOS.
                    // A single destination binding gives each explicit button one route.
                    Button { collectionRoute = "albums" } label: { Label("Альбомы", systemImage: "square.stack").frame(maxWidth: .infinity) }.buttonStyle(.bordered).accessibilityIdentifier("nativeAlbums")
                    Button { collectionRoute = "artists" } label: { Label("Исполнители", systemImage: "person.crop.circle").frame(maxWidth: .infinity) }.buttonStyle(.bordered).accessibilityIdentifier("nativeArtists")
                }.font(.subheadline.weight(.medium)).listRowBackground(Color.clear).listRowSeparator(.hidden)
                Picker("Библиотека", selection: $filter) {
                    Text("Все").tag("all"); Text("Избранное").tag("favorites"); Text("Плейлисты").tag("playlists")
                }.pickerStyle(.segmented).listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 12, trailing: 16)).listRowBackground(Color.clear).listRowSeparator(.hidden)
                if store.isMusicImporting || !store.musicImportResults.isEmpty {
                    Button { importFiles = true } label: {
                        if store.isMusicImporting { NativeMusicImportStatus(store: store) }
                        else { Label("Результат импорта", systemImage: "tray.full").font(.callout) }
                    }.buttonStyle(.plain).listRowBackground(Color.clear).accessibilityIdentifier("musicImportStatus")
                }
                if filter == "playlists" {
                    Button { playlistToEdit = nil; editingPlaylist = true } label: { Label("Создать плейлист", systemImage: "plus") }.listRowBackground(Color.clear)
                    ForEach(store.playlists.filter { query.isEmpty || $0.name.localizedCaseInsensitiveContains(query) }) { playlist in
                        NavigationLink { NativePlaylistView(store: store, playlistID: playlist.id) } label: {
                            Label { VStack(alignment: .leading) { Text(playlist.name); Text("Треков: \(playlist.trackIDs.count)").font(.caption).foregroundStyle(.secondary) } } icon: { Image(systemName: "music.note.list").frame(width: 34) }
                        }.listRowBackground(XASSStyle.surface)
                    }
                } else {
                    // Server already filtered by q / favorite; keep local filter only as a light safety net.
                    let rows = store.rows(filter: filter, query: "")
                    if !rows.isEmpty {
                        HStack(spacing: 10) {
                            Button { store.run { try await store.playAll(rows, shuffled: false) } } label: { Label("Слушать всё", systemImage: "play.fill").frame(maxWidth: .infinity) }.accessibilityIdentifier("nativePlayAll")
                            Button { store.run { try await store.playAll(rows, shuffled: true) } } label: { Label("Перемешать", systemImage: "shuffle").frame(maxWidth: .infinity) }.accessibilityIdentifier("nativeShuffleAll")
                        }.buttonStyle(.bordered).font(.subheadline.weight(.medium)).disabled(store.busy)
                            .listRowInsets(EdgeInsets(top: 0, leading: 16, bottom: 10, trailing: 16)).listRowSeparator(.hidden).listRowBackground(Color.clear)
                    }
                    ForEach(rows) { track in
                        NativeTrackRow(store: store, track: track, rows: rows) { selectedTrack = track }
                            .listRowInsets(EdgeInsets(top: 1, leading: 16, bottom: 1, trailing: 16)).listRowSeparator(.hidden).listRowBackground(Color.clear)
                            .overlay(alignment: .bottom) { Divider().padding(.leading, 72).padding(.trailing, 16).opacity(0.35) }
                            .onAppear { if track.id == rows.last?.id { store.run { await store.loadMoreTracks() } } }
                    }
                    if store.libraryLoadingMore {
                        HStack { Spacer(); ProgressView(); Spacer() }
                            .listRowBackground(Color.clear).listRowSeparator(.hidden)
                    }
                    if store.libraryHasMore && !store.libraryLoadingMore && !rows.isEmpty {
                        Button("Показать ещё") { store.run { await store.loadMoreTracks() } }
                            .listRowBackground(Color.clear).accessibilityIdentifier("nativeLibraryMore")
                    }
                    if rows.isEmpty && store.authorized && !store.loading {
                        ContentUnavailableView(query.isEmpty ? "Ваша музыка — здесь" : "Ничего не найдено", systemImage: "music.note", description: Text(query.isEmpty ? "Добавьте свои аудиофайлы кнопкой «+»." : "Попробуйте другое название или исполнителя.")).listRowBackground(Color.clear)
                    }
                }
            }.listStyle(.plain).scrollContentBackground(.hidden).background(XASSStyle.background)
                .navigationTitle("Музыка").searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Поиск")
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) { NavigationLink { NativeDownloadsView(store: store, audio: store.audio) } label: { Image(systemName: "arrow.down.circle") }.accessibilityLabel("Загрузки") }
                    ToolbarItem(placement: .topBarTrailing) { Button { importFiles = true } label: { Image(systemName: "plus").font(.title2) }.disabled(!store.authorized).accessibilityLabel("Добавить музыку") }
                }
                .refreshable { await store.refresh() }
                .onChange(of: query) { _, value in
                    searchTask?.cancel()
                    guard filter != "playlists" else { return }
                    let favorite = filter == "favorites"
                    searchTask = Task { try? await Task.sleep(for: .milliseconds(350)); guard !Task.isCancelled else { return }; await store.searchLibrary(query: value, favorite: favorite) }
                }
                .onChange(of: filter) { _, value in
                    searchTask?.cancel()
                    if value == "playlists" { return }
                    store.run { await store.searchLibrary(query: query, favorite: value == "favorites") }
                }
                .onDisappear { searchTask?.cancel() }
                .navigationDestination(item: $collectionRoute) { route in NativeCollectionLibrary(store: store, kind: route == "albums" ? .album : .artist) }
                .overlay { if store.loading && store.tracks.isEmpty { ProgressView() } }
                .sheet(item: $selectedTrack) { track in NativeTrackActions(store: store, track: track) }
                .sheet(isPresented: $editingPlaylist) { NativePlaylistEditor(store: store, playlist: playlistToEdit) }
                .sheet(isPresented: $importFiles) { NativeMusicImportView(store: store) }
        }
    }
}

@MainActor struct NativeMiniPlayer: View {
    @ObservedObject var store: NativeStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        if let track = store.currentTrack {
            HStack(spacing: 12) {
                Button { store.showPlayer = true } label: {
                    HStack(spacing: 12) {
                        TrackArtwork(store: store, trackID: track.id).frame(width: 38, height: 38)
                        VStack(alignment: .leading, spacing: 2) { Text(track.title).font(.subheadline.weight(.medium)).lineLimit(1); Text(track.artist.isEmpty ? store.deviceLabel : track.artist).font(.caption).foregroundStyle(XASSStyle.secondary).lineLimit(1) }
                        Spacer(minLength: 0)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityIdentifier("nativeMiniPlayer")
                Button { store.run { try await store.toggle() } } label: { Image(systemName: store.playing ? "pause.fill" : "play.fill").font(.title3).frame(width: 40, height: 44) }.accessibilityLabel(store.playing ? "Пауза" : "Слушать")
                Button { store.run { try await store.step(1) } } label: { Image(systemName: "forward.end.fill").font(.title3).frame(width: 40, height: 44) }.accessibilityLabel("Следующий трек")
            }.foregroundStyle(.white).padding(.horizontal, 12).padding(.vertical, 7)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 18))
                .overlay(alignment: .bottomLeading) { NativeMiniProgress(clock: store.playback) }
                .padding(.horizontal, 10).padding(.vertical, 5).background(Color.black.opacity(0.84)).disabled(store.busy)
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: track.id)
        }
    }
}

@MainActor private struct NativeMiniProgress: View {
    @ObservedObject var clock: NativePlaybackClock
    var body: some View {
        GeometryReader { size in
            Capsule().fill(.white.opacity(0.36)).frame(width: size.size.width * min(1, max(0, clock.position / max(1, clock.duration))), height: 2)
        }.frame(height: 2).padding(.horizontal, 15).padding(.bottom, 2).accessibilityHidden(true)
    }
}

@MainActor private struct NativePlayerScrubber: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var clock: NativePlaybackClock
    @Binding var scrubbing: Double?
    var body: some View {
        VStack(spacing: 0) {
            Slider(value: Binding(get: { scrubbing ?? min(clock.position, max(1, clock.duration)) }, set: { scrubbing = $0 }), in: 0...max(1, clock.duration), onEditingChanged: { editing in
                if !editing, let value = scrubbing { scrubbing = nil; store.run { try await store.seek(value) } }
            }).tint(.white.opacity(0.9)).disabled(clock.duration <= 0 || store.busy).accessibilityLabel("Позиция трека")
            HStack { Text(NativeValue.time(scrubbing ?? clock.position)); Spacer(); Text("−" + NativeValue.time(max(0, clock.duration - (scrubbing ?? clock.position)))) }
                .font(.caption).monospacedDigit().foregroundStyle(.white.opacity(0.55))
        }
    }
}

@MainActor struct NativePlayerView: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showQueue = false
    @State private var showLyrics = false
    @State private var showLyricsTools = false
    @State private var selectedTrack: LibraryTrack?
    @State private var scrubbing: Double?
    var body: some View {
        ScrollView {
            VStack(spacing: 18) {
                    HStack {
                        Button { dismiss() } label: { Image(systemName: "chevron.down").font(.title3).frame(width: 44, height: 36) }.accessibilityLabel("Свернуть плеер")
                        Spacer()
                        Text(showLyrics ? "Текст песни" : "Моя музыка").font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.6))
                        Spacer()
                        Button { selectedTrack = store.currentTrack } label: { Image(systemName: "ellipsis").font(.title3).frame(width: 44, height: 36) }.accessibilityLabel("Действия с треком").accessibilityIdentifier("nativePlayerMore")
                    }.foregroundStyle(.white)
                    if let track = store.currentTrack {
                        if showLyrics {
                            HStack(spacing: 12) {
                                TrackArtwork(store: store, trackID: track.id).frame(width: 48, height: 48)
                                metadata(track)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            NativeTimedLyricsView(store: store, lyrics: store.lyrics, onMore: { showLyricsTools = true })
                                .frame(height: 420)
                                .transition(.opacity)
                        } else {
                            // Fixed artwork keeps this ScrollView's identity stable while playback ticks.
                            // Small screens and large type reach the controls by scrolling.
                            TrackArtwork(store: store, trackID: track.id, radius: 14, large: true)
                                .frame(maxWidth: 400).aspectRatio(1, contentMode: .fit)
                                .shadow(color: .black.opacity(0.24), radius: 22, y: 12)
                                .scaleEffect(!store.playing && !reduceMotion ? 0.96 : 1)
                                .animation(reduceMotion ? nil : .spring(response: 0.45, dampingFraction: 0.85), value: store.playing)
                                .padding(.vertical, 8).transition(.opacity)
                            metadata(track)
                        }
                        NativePlayerScrubber(store: store, clock: store.playback, scrubbing: $scrubbing)
                        if store.busy || store.playbackState == "loading" { HStack { ProgressView(); Text(store.transferStatus ?? (store.busy ? "Выполняю действие…" : "Загрузка трека…")).font(.caption) }.accessibilityIdentifier("nativePlayerLoading") }
                        HStack {
                            Button { store.run { try await store.step(-1) } } label: { Image(systemName: "backward.end.fill").font(.system(size: 26)).frame(width: 44, height: 48) }.accessibilityLabel("Предыдущий трек")
                            Spacer()
                            Button { store.run { try await store.toggle() } } label: { Image(systemName: store.playing ? "pause.fill" : "play.fill").font(.system(size: 36)).frame(width: 56, height: 56) }.accessibilityLabel(store.playing ? "Пауза" : "Слушать").accessibilityIdentifier("nativePlayerToggle")
                            Spacer()
                            Button { store.run { try await store.step(1) } } label: { Image(systemName: "forward.end.fill").font(.system(size: 26)).frame(width: 44, height: 48) }.accessibilityLabel("Следующий трек")
                        }.padding(.horizontal, 32).foregroundStyle(.white).buttonStyle(MusicPressStyle()).disabled(store.busy)
                        if store.otherLocal { Text("Команды выполняются после ответа другого устройства. Для переноса выберите «Этот iPhone» ниже.").font(.caption).foregroundStyle(.secondary) }
                        NativePlayerVolumeSection(store: store)
                        HStack(alignment: .top) {
                            Button { showLyrics.toggle() } label: { Image(systemName: "quote.bubble").font(.title3).frame(width: 48, height: 44).background(showLyrics ? Color.white.opacity(0.17) : Color.clear, in: RoundedRectangle(cornerRadius: 12)) }.accessibilityLabel(showLyrics ? "Показать обложку" : "Текст песни").accessibilityIdentifier("nativePlayerLyrics")
                            Spacer()
                            Button { store.openRoutePicker() } label: { VStack(spacing: 5) { Image(systemName: store.selectedDevice == "local" ? "airplayaudio" : "desktopcomputer").font(.title3).frame(height: 30); Text(store.deviceLabel).font(.caption2).lineLimit(2).multilineTextAlignment(.center) }.frame(minWidth: 100, minHeight: 44) }.accessibilityIdentifier("nativePlayerDevices")
                            Spacer()
                            Button { showQueue = true } label: { Image(systemName: "list.bullet").font(.title3).frame(width: 48, height: 44) }.accessibilityLabel("Очередь").accessibilityIdentifier("nativePlayerQueue")
                        }.foregroundStyle(.white.opacity(0.8)).buttonStyle(MusicPressStyle())
                        if store.shareSite { Label("Сейчас на вашем сайте", systemImage: "dot.radiowaves.left.and.right").font(.caption2).foregroundStyle(.white.opacity(0.55)) }
                    }
                    NativeMessage(store: store)
                }.padding(.horizontal, 28).padding(.top, 10).padding(.bottom, 24).frame(maxWidth: 560).frame(maxWidth: .infinity)
        }.scrollBounceBehavior(.basedOnSize)
            .background { NativeMusicBackdrop(store: store, trackID: store.currentID) }
            .sheet(isPresented: $showLyricsTools, onDismiss: { store.lyrics.retry() }) {
                if let id = store.currentID {
                    NavigationStack {
                        NativeEnrichmentView(store: store, trackID: id).toolbar {
                            ToolbarItem(placement: .confirmationAction) { Button("Готово") { showLyricsTools = false } }
                        }
                    }
                }
            }
            .presentationDragIndicator(.visible).presentationDetents([.large]).tint(.white)
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: showLyrics)
            .sheet(isPresented: $showQueue) { NativeQueueView(store: store) }
            .sheet(item: $selectedTrack) { track in NativeTrackActions(store: store, track: track) }
            .onChange(of: store.currentID) { _, _ in scrubbing = nil }
            .onAppear {
                #if DEBUG && targetEnvironment(simulator)
                if NativeFixture.enabled {
                    if NativeFixture.screen == "lyrics" { showLyrics = true }
                    if NativeFixture.screen == "queue" { showQueue = true }
                }
                #endif
            }
    }
    private func metadata(_ track: LibraryTrack) -> some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text(track.title).font(.title2.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                Text(track.artist.isEmpty ? "Моя коллекция" : track.artist).font(.body).foregroundStyle(.white.opacity(0.62))
            }.frame(maxWidth: .infinity, alignment: .leading)
            Button { store.run { try await store.favorite(track) } } label: { Image(systemName: track.favorite ? "star.fill" : "star").font(.title3).frame(width: 44, height: 44) }.disabled(store.busy).accessibilityLabel(track.favorite ? "Убрать из избранного" : "В избранное")
        }.foregroundStyle(.white)
    }
}

@MainActor struct NativeQueueView: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List {
                HStack(spacing: 12) {
                    Button { store.run { try await store.setQueueMode(shuffled: !store.shuffle) } } label: { Label("Перемешать", systemImage: "shuffle").frame(maxWidth: .infinity) }.tint(store.shuffle ? .white : XASSStyle.secondary).accessibilityIdentifier("nativeQueueShuffle")
                    Button { store.run { try await store.setQueueMode(repeatMode: store.repeatMode == "off" ? "all" : store.repeatMode == "all" ? "one" : "off") } } label: { Label(store.repeatMode == "one" ? "Один трек" : "Повтор", systemImage: store.repeatMode == "one" ? "repeat.1" : "repeat").frame(maxWidth: .infinity) }.tint(store.repeatMode == "off" ? XASSStyle.secondary : .white).accessibilityIdentifier("nativeQueueRepeat")
                }.buttonStyle(.bordered).disabled(!store.canEditQueue).listRowBackground(Color.clear).listRowSeparator(.hidden)
                Section(store.queue.isEmpty ? "Выбрать из библиотеки" : "Следующие треки") {
                    ForEach(store.queue.isEmpty ? store.tracks : store.queue) { track in
                        Button { store.run { try await store.play(track); dismiss() } } label: {
                            HStack(spacing: 12) {
                                TrackArtwork(store: store, trackID: track.id).frame(width: 44, height: 44)
                                VStack(alignment: .leading, spacing: 4) { Text(track.title).foregroundStyle(.white).lineLimit(2); Text(track.artist).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                                Spacer(); if track.id == store.currentID { Image(systemName: "waveform").foregroundStyle(.white) }
                            }.padding(.vertical, 4)
                        }.disabled(store.busy).listRowBackground(Color.clear).accessibilityIdentifier("queue-track-\(track.id)")
                    }
                }
                NativeMessage(store: store).listRowBackground(Color.clear)
            }.listStyle(.plain).scrollContentBackground(.hidden).background(XASSStyle.background).navigationTitle("Очередь").toolbar { Button("Готово") { dismiss() } }
        }.tint(.white)
    }
}

@MainActor struct NativeTrackActions: View {
    @ObservedObject var store: NativeStore
    let track: LibraryTrack
    var onDelete: ((Int) -> Void)? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false
    @State private var acting = false
    private var current: LibraryTrack { store.resolvedTrack(track) }
    var body: some View {
        NavigationStack {
            List {
                Button { perform { try await store.favorite(current) } } label: { Label(current.favorite ? "Убрать из избранного" : "В избранное", systemImage: "heart") }
                Button { perform { try await store.download(track) } } label: { Label("Сохранить на iPhone", systemImage: "arrow.down.circle") }.disabled(store.audio.downloadIDs.contains(track.id)).accessibilityIdentifier("nativeDownload")
                NavigationLink { NativeEnrichmentView(store: store, trackID: track.id) } label: {
                    Label("Текст и информация о песне", systemImage: "sparkle.magnifyingglass")
                }.accessibilityIdentifier("nativeTrackInformation")
                if track.id == store.currentID {
                    Toggle(isOn: Binding(get: { store.shareSite }, set: { desired in store.run { try await store.setSharing(desired) } })) {
                        Label(store.shareSaving ? "Сохраняю…" : "Показывать на сайте", systemImage: "dot.radiowaves.left.and.right")
                    }.disabled(store.shareSaving || store.busy).accessibilityIdentifier("nativeShareSite")
                }
                Section("Добавить в плейлист") {
                    if store.playlists.isEmpty { Text("Создайте плейлист на вкладке «Плейлисты».").foregroundStyle(.secondary) }
                    ForEach(store.playlists) { playlist in Button(playlist.name) { perform { try await store.savePlaylist(id: playlist.id, name: playlist.name, trackIDs: playlist.trackIDs.contains(track.id) ? playlist.trackIDs : playlist.trackIDs + [track.id]) } } }
                }
                Button("Убрать из библиотеки", role: .destructive) { confirmDelete = true }
                NativeMessage(store: store)
            }.disabled(acting).navigationTitle(track.title).navigationBarTitleDisplayMode(.inline).toolbar { Button("Готово") { dismiss() }.disabled(acting) }
                .confirmationDialog("Убрать трек из библиотеки? Файл останется на сервере для восстановления.", isPresented: $confirmDelete, titleVisibility: .visible) { Button("Убрать трек", role: .destructive) { perform { try await store.deleteTrack(track); onDelete?(track.id) } } }
        }.presentationDetents([.medium, .large]).interactiveDismissDisabled(acting).tint(XASSStyle.accent)
    }
    private func perform(_ action: @escaping () async throws -> Void) {
        guard !acting else { return }
        acting = true
        store.run { defer { acting = false }; try await action(); dismiss() }
    }
}

@MainActor struct NativePlaylistView: View {
    @ObservedObject var store: NativeStore
    let playlistID: Int
    @State private var editing = false
    @State private var confirmDelete = false
    @State private var tracks: [LibraryTrack] = []
    @State private var loading = false
    @State private var selectedTrack: LibraryTrack?
    @Environment(\.dismiss) private var dismiss
    private var playlist: LibraryPlaylist? { store.playlists.first { $0.id == playlistID } }
    var body: some View {
        List {
            if let playlist = playlist {
                if !tracks.isEmpty {
                    HStack {
                        Button { store.run { try await store.playAll(tracks, shuffled: false) } } label: { Label("Слушать", systemImage: "play.fill") }
                        Spacer()
                        Button { store.run { try await store.playAll(tracks, shuffled: true) } } label: { Label("Перемешать", systemImage: "shuffle") }
                    }.buttonStyle(.bordered).disabled(store.busy)
                }
                ForEach(tracks) { track in
                    NativeTrackRow(store: store, track: track, rows: tracks) { selectedTrack = track }.listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                if tracks.isEmpty && !loading { ContentUnavailableView("В плейлисте пока нет треков", systemImage: "music.note.list", description: Text("Добавьте музыку через меню «Изменить».")) }
                NativeMessage(store: store)
                Text("Треков в плейлисте: \(playlist.trackIDs.count)").font(.caption).foregroundStyle(.secondary)
            }
        }.navigationTitle(playlist?.name ?? "Плейлист").toolbar { Menu { Button("Изменить") { editing = true }; Button("Удалить плейлист", role: .destructive) { confirmDelete = true } } label: { Image(systemName: "ellipsis") } }
            .task(id: playlist?.trackIDs) { await loadTracks() }
            .refreshable { await loadTracks() }
            .overlay { if loading && tracks.isEmpty { ProgressView() } }
            .sheet(isPresented: $editing, onDismiss: { Task { await loadTracks() } }) { NativePlaylistEditor(store: store, playlist: playlist) }
            .sheet(item: $selectedTrack, onDismiss: { Task { await loadTracks() } }) { track in NativeTrackActions(store: store, track: track) }
            .confirmationDialog("Удалить плейлист? Треки останутся в библиотеке.", isPresented: $confirmDelete, titleVisibility: .visible) { Button("Удалить", role: .destructive) { if let playlist = playlist { store.run { try await store.deletePlaylist(playlist) }; dismiss() } } }
    }
    private func loadTracks() async {
        guard let playlist = playlist, !loading else { return }
        loading = true; defer { loading = false }
        do { tracks = try await store.playlistTracks(playlist) }
        catch is CancellationError { }
        catch { store.handle(error) }
    }
}

@MainActor struct NativePlaylistEditor: View {
    @ObservedObject var store: NativeStore
    let playlist: LibraryPlaylist?
    @State private var name = ""
    @State private var selected = Set<Int>()
    @State private var saving = false
    @State private var tracks: [LibraryTrack] = []
    @State private var loading = false
    @State private var nextOffset: Int? = 0
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                TextField("Название плейлиста", text: $name).accessibilityIdentifier("playlistName")
                Section("Треки") {
                    ForEach(tracks) { track in Toggle(track.title, isOn: Binding(get: { selected.contains(track.id) }, set: { if $0 { selected.insert(track.id) } else { selected.remove(track.id) } })) }
                    if loading { ProgressView("Загрузка библиотеки…") }
                    else if nextOffset != nil { Button("Показать ещё треки") { Task { await loadMore() } } }
                }
                NativeMessage(store: store)
            }.navigationTitle(playlist == nil ? "Новый плейлист" : "Изменить плейлист")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() }.disabled(saving) }; ToolbarItem(placement: .confirmationAction) { Button("Сохранить") { saving = true; store.run { defer { saving = false }; let preserved = (playlist?.trackIDs ?? []).filter { selected.contains($0) }; let ids = preserved + tracks.filter { selected.contains($0.id) && !preserved.contains($0.id) }.map(\.id); try await store.savePlaylist(id: playlist?.id, name: String(name.prefix(160)), trackIDs: ids); dismiss() } }.disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) } }
                .onAppear { name = playlist?.name ?? ""; selected = Set(playlist?.trackIDs ?? []) }
                .task { await loadMore() }
        }.interactiveDismissDisabled(saving)
    }
    private func loadMore() async {
        guard !loading, let offset = nextOffset else { return }
        loading = true; defer { loading = false }
        do {
            let response = try await store.api.request("/api/mini/music/library?offset=\(offset)&limit=200", method: "GET", body: nil)
            let page = (response["tracks"] as? [[String: Any]] ?? []).compactMap(LibraryTrack.init)
            let seen = Set(tracks.map(\.id)); tracks += page.filter { !seen.contains($0.id) }
            nextOffset = response["has_more"] as? Bool == true ? response["next_offset"] as? Int : nil
            if let next = nextOffset, next <= offset { nextOffset = nil }
        } catch is CancellationError { }
        catch { store.handle(error) }
    }
}
