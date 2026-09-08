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
    @State private var image: UIImage?
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: radius).fill(XASSStyle.surface)
            if let image = image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: "music.note").font(.system(size: 24, weight: .medium)).foregroundStyle(XASSStyle.secondary) }
        }.aspectRatio(1, contentMode: .fit).clipShape(RoundedRectangle(cornerRadius: radius))
            .accessibilityHidden(true)
            .task(id: trackID) { image = nil; image = await store.artwork(trackID) }
    }
}

@MainActor struct NativeMessage: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        if let text = store.error ?? store.notice {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: store.error == nil ? "checkmark.circle" : "exclamationmark.circle").foregroundStyle(store.error == nil ? Color.green : Color.orange)
                Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button { store.error = nil; store.notice = nil } label: { Image(systemName: "xmark").frame(width: 24, height: 24) }.accessibilityLabel("Скрыть сообщение")
            }.padding(14).background(XASSStyle.surface, in: RoundedRectangle(cornerRadius: 14)).accessibilityElement(children: .contain)
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
        }.padding(.leading, 10).padding(.trailing, 4).padding(.vertical, 9)
            .background(XASSStyle.surface, in: RoundedRectangle(cornerRadius: 12))
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
    var body: some View {
        NavigationStack {
            List {
                if !store.authorized {
                    NativeLoginPrompt(store: store).listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                if store.error != nil || store.notice != nil { NativeMessage(store: store).listRowBackground(Color.clear).listRowSeparator(.hidden) }
                Picker("Библиотека", selection: $filter) {
                    Text("Все").tag("all"); Text("Избранное").tag("favorites"); Text("Плейлисты").tag("playlists")
                }.pickerStyle(.segmented).listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 12, trailing: 16)).listRowBackground(Color.clear).listRowSeparator(.hidden)
                if let name = store.uploadName {
                    VStack(alignment: .leading) { Text(name).font(.callout).lineLimit(1); ProgressView(value: store.uploadProgress); Text("Загрузка на сервер…").font(.caption).foregroundStyle(.secondary) }.listRowBackground(Color.clear).accessibilityIdentifier("musicUploadProgress")
                }
                if filter == "playlists" {
                    Button { playlistToEdit = nil; editingPlaylist = true } label: { Label("Создать плейлист", systemImage: "plus") }.listRowBackground(Color.clear)
                    ForEach(store.playlists.filter { query.isEmpty || $0.name.localizedCaseInsensitiveContains(query) }) { playlist in
                        NavigationLink { NativePlaylistView(store: store, playlistID: playlist.id) } label: {
                            Label { VStack(alignment: .leading) { Text(playlist.name); Text("Треков: \(playlist.trackIDs.count)").font(.caption).foregroundStyle(.secondary) } } icon: { Image(systemName: "music.note.list").frame(width: 34) }
                        }.listRowBackground(XASSStyle.surface)
                    }
                } else {
                    let rows = store.rows(filter: filter, query: query)
                    ForEach(rows) { track in
                        NativeTrackRow(store: store, track: track, rows: rows) { selectedTrack = track }
                            .listRowInsets(EdgeInsets(top: 1, leading: 16, bottom: 1, trailing: 16)).listRowSeparator(.hidden).listRowBackground(Color.clear)
                    }
                    if rows.isEmpty && store.authorized && !store.loading {
                        ContentUnavailableView(query.isEmpty ? "Ваша музыка — здесь" : "Ничего не найдено", systemImage: "music.note", description: Text(query.isEmpty ? "Добавьте свои аудиофайлы кнопкой «+»." : "Попробуйте другое название или исполнителя.")).listRowBackground(Color.clear)
                    }
                }
            }.listStyle(.plain).scrollContentBackground(.hidden).background(XASSStyle.background)
                .navigationTitle("Музыка").searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Поиск")
                .toolbar { ToolbarItem(placement: .topBarTrailing) { Button { importFiles = true } label: { Image(systemName: "plus").font(.title2) }.disabled(!store.authorized || store.uploadName != nil).accessibilityLabel("Добавить музыку") } }
                .refreshable { await store.refresh() }
                .overlay { if store.loading && store.tracks.isEmpty { ProgressView() } }
                .safeAreaInset(edge: .bottom, spacing: 0) { NativeMiniPlayer(store: store) }
                .sheet(item: $selectedTrack) { track in NativeTrackActions(store: store, track: track) }
                .sheet(isPresented: $editingPlaylist) { NativePlaylistEditor(store: store, playlist: playlistToEdit) }
                .fileImporter(isPresented: $importFiles, allowedContentTypes: [.audio, .zip], allowsMultipleSelection: true) { result in
                    switch result { case .success(let urls): store.run { for url in urls { try await store.upload(url) } }; case .failure(let error): store.handle(error) }
                }
        }
    }
}

@MainActor struct NativeMiniPlayer: View {
    @ObservedObject var store: NativeStore
    var body: some View {
        if let track = store.currentTrack {
            HStack(spacing: 12) {
                Button { store.showPlayer = true } label: {
                    HStack(spacing: 12) {
                        TrackArtwork(store: store, trackID: track.id).frame(width: 44, height: 44)
                        VStack(alignment: .leading, spacing: 3) { Text(track.title).font(.body.weight(.medium)).lineLimit(1); Text(store.deviceLabel).font(.caption).foregroundStyle(XASSStyle.secondary).lineLimit(1) }
                        Spacer(minLength: 0)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityIdentifier("nativeMiniPlayer")
                Button { store.run { try await store.toggle() } } label: { Image(systemName: store.playing ? "pause.fill" : "play.fill").font(.title3).frame(width: 40, height: 44) }.accessibilityLabel(store.playing ? "Пауза" : "Слушать")
                Button { store.run { try await store.step(1) } } label: { Image(systemName: "forward.end.fill").font(.title3).frame(width: 40, height: 44) }.accessibilityLabel("Следующий трек")
            }.foregroundStyle(.white).padding(.horizontal, 16).padding(.vertical, 10)
                .background(XASSStyle.surface).overlay(alignment: .top) { Divider() }.disabled(store.busy)
        }
    }
}

@MainActor struct NativePlayerView: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var showDevices = false
    @State private var showQueue = false
    @State private var scrubbing: Double?
    @State private var volume: Double?
    var body: some View {
        GeometryReader { geometry in
            ScrollView {
                VStack(spacing: 20) {
                    HStack {
                        Button { dismiss() } label: { Image(systemName: "chevron.down").font(.title3).frame(width: 44, height: 36) }.accessibilityLabel("Свернуть плеер")
                        Spacer()
                        Button { showQueue = true } label: { Image(systemName: "list.bullet").font(.title3).frame(width: 44, height: 36) }.accessibilityLabel("Очередь")
                    }.foregroundStyle(.white)
                    if let track = store.currentTrack {
                        TrackArtwork(store: store, trackID: track.id, radius: 16)
                            .frame(width: min(geometry.size.width - 48, max(160, geometry.size.height * 0.33)), height: min(geometry.size.width - 48, max(160, geometry.size.height * 0.33)))
                            .padding(.bottom, 4)
                        HStack(spacing: 14) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(track.title).font(.title2.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                                Text(track.artist.isEmpty ? "Моя коллекция" : track.artist).font(.subheadline).foregroundStyle(XASSStyle.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            Button { store.run { try await store.favorite(track) } } label: { Image(systemName: track.favorite ? "heart.fill" : "heart").font(.title2).frame(width: 44, height: 44) }.accessibilityLabel(track.favorite ? "Убрать из избранного" : "В избранное")
                        }
                        VStack(spacing: 0) {
                            Slider(value: Binding(get: { scrubbing ?? min(store.position, max(1, store.duration)) }, set: { scrubbing = $0 }), in: 0...max(1, store.duration), onEditingChanged: { editing in if !editing, let value = scrubbing { scrubbing = nil; store.run { try await store.seek(value) } } }).accessibilityLabel("Позиция трека")
                            HStack { Text(NativeValue.time(scrubbing ?? store.position)); Spacer(); Text(NativeValue.time(store.duration)) }.font(.caption).monospacedDigit().foregroundStyle(XASSStyle.secondary)
                        }
                        if store.busy || store.playbackState == "loading" { HStack { ProgressView(); Text(store.busy ? "Переключаю устройство…" : "Загрузка трека…").font(.caption) }.accessibilityIdentifier("nativePlayerLoading") }
                        HStack {
                            Button { store.shuffle.toggle(); store.updateQueue() } label: { Image(systemName: "shuffle").font(.title3).frame(width: 44, height: 48) }.foregroundStyle(store.shuffle ? XASSStyle.accent : XASSStyle.secondary).accessibilityLabel("Перемешивание").disabled(!store.canEditQueue)
                            Spacer()
                            Button { store.run { try await store.step(-1) } } label: { Image(systemName: "backward.end.fill").font(.system(size: 26)).frame(width: 44, height: 48) }.accessibilityLabel("Предыдущий трек")
                            Spacer()
                            Button { store.run { try await store.toggle() } } label: { Image(systemName: store.playing ? "pause.fill" : "play.fill").font(.system(size: 36)).frame(width: 56, height: 56) }.accessibilityLabel(store.playing ? "Пауза" : "Слушать").accessibilityIdentifier("nativePlayerToggle")
                            Spacer()
                            Button { store.run { try await store.step(1) } } label: { Image(systemName: "forward.end.fill").font(.system(size: 26)).frame(width: 44, height: 48) }.accessibilityLabel("Следующий трек")
                            Spacer()
                            Button { store.repeatMode = store.repeatMode == "off" ? "all" : store.repeatMode == "all" ? "one" : "off"; store.updateQueue() } label: { Image(systemName: store.repeatMode == "one" ? "repeat.1" : "repeat").font(.title3).frame(width: 44, height: 48) }.foregroundStyle(store.repeatMode == "off" ? XASSStyle.secondary : XASSStyle.accent).accessibilityLabel("Повтор: \(store.repeatMode)").disabled(!store.canEditQueue)
                        }.foregroundStyle(.white).disabled(store.busy)
                        if store.otherLocal { Text("Команды выполняются после ответа другого устройства. Для переноса выберите «Этот iPhone» ниже.").font(.caption).foregroundStyle(.secondary) }
                        Button { showDevices = true } label: { HStack(spacing: 12) { Image(systemName: store.selectedDevice == "local" ? "airplayaudio" : "desktopcomputer").font(.title3); Text(store.deviceLabel).foregroundStyle(.white); Spacer(); Image(systemName: "chevron.right").foregroundStyle(.secondary) }.padding(16).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 16)).overlay(RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.07))) }.accessibilityIdentifier("nativePlayerDevices")
                        HStack(spacing: 14) {
                            Image(systemName: "speaker.fill").foregroundStyle(.secondary)
                            Slider(value: Binding(get: { volume ?? store.volume }, set: { volume = $0 }), in: 0...100, onEditingChanged: { editing in if !editing, let value = volume { volume = nil; store.run { try await store.setVolume(value) } } }).accessibilityLabel("Громкость XASS")
                            Image(systemName: "speaker.wave.2.fill").foregroundStyle(.secondary)
                        }
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 12) { sharing; download(track) }
                            VStack(spacing: 12) { sharing; download(track) }
                        }
                    }
                    NativeMessage(store: store)
                }.padding(.horizontal, 24).padding(.top, 10).padding(.bottom, 24).frame(maxWidth: 560).frame(maxWidth: .infinity)
            }.background(XASSStyle.surface).scrollBounceBehavior(.basedOnSize)
        }.presentationDragIndicator(.visible).presentationDetents([.large])
            .sheet(isPresented: $showDevices) { NativePlayerDevices(store: store) }
            .sheet(isPresented: $showQueue) { NativeQueueView(store: store) }
    }
    private var sharing: some View {
        Toggle(isOn: Binding(get: { store.shareSite }, set: { desired in store.run { try await store.setSharing(desired) } })) {
            Text(store.shareSaving ? "Сохраняю…" : "На сайте").font(.subheadline)
        }.disabled(store.shareSaving || store.busy).padding(14).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 16)).accessibilityIdentifier("nativeShareSite")
    }
    private func download(_ track: LibraryTrack) -> some View {
        Button { store.run { try await store.download(track) } } label: {
            Label(store.audio.downloads.contains(where: { $0.id == track.id }) ? "Сохранено" : "Загрузить", systemImage: "arrow.down.to.line").font(.subheadline.weight(.medium)).frame(maxWidth: .infinity).padding(18)
        }.disabled(store.audio.downloadIDs.contains(track.id)).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 16)).accessibilityIdentifier("nativeDownload")
    }
}

@MainActor struct NativeQueueView: View {
    @ObservedObject var store: NativeStore
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List(store.queue.isEmpty ? store.tracks : store.queue) { track in
                Button { store.run { try await store.play(track) }; dismiss() } label: { HStack { Text(track.title); Spacer(); if track.id == store.currentID { Image(systemName: "waveform") } } }
            }.navigationTitle("Очередь").toolbar { Button("Готово") { dismiss() } }
        }
    }
}

@MainActor struct NativeTrackActions: View {
    @ObservedObject var store: NativeStore
    let track: LibraryTrack
    @Environment(\.dismiss) private var dismiss
    @State private var confirmDelete = false
    var body: some View {
        NavigationStack {
            List {
                Button { store.run { try await store.favorite(track) }; dismiss() } label: { Label(track.favorite ? "Убрать из избранного" : "В избранное", systemImage: "heart") }
                Button { store.run { try await store.download(track) }; dismiss() } label: { Label("Сохранить на iPhone", systemImage: "arrow.down.circle") }
                Section("Добавить в плейлист") {
                    if store.playlists.isEmpty { Text("Создайте плейлист на вкладке «Плейлисты».").foregroundStyle(.secondary) }
                    ForEach(store.playlists) { playlist in Button(playlist.name) { store.run { try await store.savePlaylist(id: playlist.id, name: playlist.name, trackIDs: playlist.trackIDs.contains(track.id) ? playlist.trackIDs : playlist.trackIDs + [track.id]) }; dismiss() } }
                }
                Button("Убрать из библиотеки", role: .destructive) { confirmDelete = true }
            }.navigationTitle(track.title).navigationBarTitleDisplayMode(.inline).toolbar { Button("Готово") { dismiss() } }
                .confirmationDialog("Убрать трек из библиотеки? Файл останется на сервере для восстановления.", isPresented: $confirmDelete, titleVisibility: .visible) { Button("Убрать трек", role: .destructive) { store.run { try await store.deleteTrack(track) }; dismiss() } }
        }.presentationDetents([.medium, .large])
    }
}

@MainActor struct NativePlaylistView: View {
    @ObservedObject var store: NativeStore
    let playlistID: Int
    @State private var editing = false
    @State private var confirmDelete = false
    @Environment(\.dismiss) private var dismiss
    private var playlist: LibraryPlaylist? { store.playlists.first { $0.id == playlistID } }
    var body: some View {
        List {
            if let playlist = playlist {
                ForEach(store.rows(filter: "all", query: "", playlist: playlist)) { track in
                    Button { store.run { try await store.play(track, rows: store.rows(filter: "all", query: "", playlist: playlist)) } } label: { HStack { Text(track.title); Spacer(); Text(NativeValue.time(track.duration)).foregroundStyle(.secondary) } }
                }
            }
        }.navigationTitle(playlist?.name ?? "Плейлист").toolbar { Menu { Button("Изменить") { editing = true }; Button("Удалить плейлист", role: .destructive) { confirmDelete = true } } label: { Image(systemName: "ellipsis") } }
            .sheet(isPresented: $editing) { NativePlaylistEditor(store: store, playlist: playlist) }
            .confirmationDialog("Удалить плейлист? Треки останутся в библиотеке.", isPresented: $confirmDelete, titleVisibility: .visible) { Button("Удалить", role: .destructive) { if let playlist = playlist { store.run { try await store.deletePlaylist(playlist) }; dismiss() } } }
    }
}

@MainActor struct NativePlaylistEditor: View {
    @ObservedObject var store: NativeStore
    let playlist: LibraryPlaylist?
    @State private var name = ""
    @State private var selected = Set<Int>()
    @State private var saving = false
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            Form {
                TextField("Название плейлиста", text: $name).accessibilityIdentifier("playlistName")
                Section("Треки") { ForEach(store.tracks) { track in Toggle(track.title, isOn: Binding(get: { selected.contains(track.id) }, set: { if $0 { selected.insert(track.id) } else { selected.remove(track.id) } })) } }
                NativeMessage(store: store)
            }.navigationTitle(playlist == nil ? "Новый плейлист" : "Изменить плейлист")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() } }; ToolbarItem(placement: .confirmationAction) { Button("Сохранить") { saving = true; store.run { defer { saving = false }; let preserved = (playlist?.trackIDs ?? []).filter { selected.contains($0) }; let ids = preserved + store.tracks.filter { selected.contains($0.id) && !preserved.contains($0.id) }.map(\.id); try await store.savePlaylist(id: playlist?.id, name: String(name.prefix(160)), trackIDs: ids); dismiss() } }.disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) } }
                .onAppear { name = playlist?.name ?? ""; selected = Set(playlist?.trackIDs ?? []) }
        }
    }
}
