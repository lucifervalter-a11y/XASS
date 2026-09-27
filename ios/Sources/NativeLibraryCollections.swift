import SwiftUI

/// Separate pagination keeps browsing albums from replacing the user's song search.
@MainActor final class MusicCollectionCatalog: ObservableObject {
    @Published private(set) var tracks: [LibraryTrack] = []
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    @Published private(set) var complete = false
    private var nextOffset = 0
    private let api: OwnerService
    init(api: OwnerService) { self.api = api }

    func load() async {
        guard !loading, !complete else { return }
        loading = true; error = nil
        defer { loading = false }
        do {
            // At most 2,000 rows per UI batch, without loading audio or artwork.
            for _ in 0..<10 {
                try Task.checkCancellation()
                let offset = nextOffset
                let page = try await api.request("/api/mini/music/library?offset=\(offset)&limit=200", method: "GET", body: nil)
                try Task.checkCancellation()
                let incoming = (page["tracks"] as? [[String: Any]] ?? []).compactMap(LibraryTrack.init)
                var seen = Set(tracks.map(\.id))
                tracks += incoming.filter { seen.insert($0.id).inserted }
                if page["has_more"] as? Bool != true { complete = true; return }
                guard let next = page["next_offset"] as? Int, next > offset else { throw OwnerAPIError.invalidResponse }
                nextOffset = next
            }
        } catch is CancellationError { }
        catch { if !Task.isCancelled { self.error = error.localizedDescription } }
    }
}

@MainActor struct NativeCollectionLibrary: View {
    @ObservedObject var store: NativeStore
    let kind: MusicCollection.Kind
    @StateObject private var catalog: MusicCollectionCatalog
    @State private var query = ""
    @Environment(\.dynamicTypeSize) private var typeSize
    init(store: NativeStore, kind: MusicCollection.Kind) {
        self.store = store; self.kind = kind
        _catalog = StateObject(wrappedValue: MusicCollectionCatalog(api: store.api))
    }
    private var groups: [MusicCollection] {
        MusicCollection.groups(catalog.tracks, kind: kind).filter { query.isEmpty || ($0.title + " " + $0.subtitle).localizedCaseInsensitiveContains(query) }
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let error = catalog.error {
                    VStack(alignment: .leading, spacing: 8) { Text(error).font(.callout); Button("Повторить") { Task { await catalog.load() } } }
                        .padding().background(XASSStyle.surface, in: RoundedRectangle(cornerRadius: 14))
                }
                if kind == .album {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: typeSize.isAccessibilitySize ? 260 : 145), spacing: 18)], spacing: 24) {
                        ForEach(groups) { group in
                            NavigationLink { NativeCollectionDetail(store: store, collection: group, kind: kind, complete: catalog.complete) } label: {
                                VStack(alignment: .leading, spacing: 8) {
                                    if let id = group.artworkID { TrackArtwork(store: store, trackID: id, radius: 12) }
                                    Text(group.title).font(.headline).foregroundStyle(.white).lineLimit(2)
                                    Text(group.subtitle).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.buttonStyle(MusicPressStyle()).accessibilityIdentifier("nativeAlbum-\(group.artworkID ?? 0)")
                        }
                    }
                } else {
                    LazyVStack(spacing: 0) {
                        ForEach(groups) { group in
                            NavigationLink { NativeCollectionDetail(store: store, collection: group, kind: kind, complete: catalog.complete) } label: {
                                HStack(spacing: 14) {
                                    if let id = group.artworkID { TrackArtwork(store: store, trackID: id, radius: 28).frame(width: 56, height: 56) }
                                    VStack(alignment: .leading, spacing: 4) { Text(group.title).foregroundStyle(.white); Text(group.subtitle).font(.caption).foregroundStyle(.secondary) }
                                    Spacer(); Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
                                }.padding(.vertical, 12).contentShape(Rectangle())
                            }.buttonStyle(.plain).accessibilityIdentifier("nativeArtist-\(group.artworkID ?? 0)")
                            Divider().overlay(.white.opacity(0.07))
                        }
                    }
                }
                if catalog.loading { ProgressView("Читаю метаданные библиотеки…").frame(maxWidth: .infinity) }
                else if !catalog.complete {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Показана часть библиотеки: \(catalog.tracks.count) треков. Состав альбомов может быть неполным.").font(.footnote).foregroundStyle(.secondary)
                        Button("Загрузить ещё") { Task { await catalog.load() } }.buttonStyle(.bordered)
                    }
                } else if groups.isEmpty {
                    ContentUnavailableView(query.isEmpty ? (kind == .album ? "Нет альбомов" : "Нет исполнителей") : "Ничего не найдено", systemImage: kind == .album ? "square.stack" : "person.crop.circle", description: Text("Здесь используются теги ваших аудиофайлов. Треки без названия альбома остаются в разделе «Все»."))
                }
            }.padding(20)
        }.background(XASSStyle.background).navigationTitle(kind == .album ? "Альбомы" : "Исполнители")
            .searchable(text: $query, prompt: "Поиск в коллекции")
            .task { await catalog.load() }
    }
}

@MainActor struct NativeCollectionDetail: View {
    @ObservedObject var store: NativeStore
    let collection: MusicCollection
    let kind: MusicCollection.Kind
    let complete: Bool
    @State private var selectedTrack: LibraryTrack?
    @State private var removedTracks = Set<Int>()
    private var rows: [LibraryTrack] { collection.tracks.filter { !removedTracks.contains($0.id) }.map { store.resolvedTrack($0) } }
    var body: some View {
        ScrollView {
            VStack(spacing: 22) {
                if let id = collection.artworkID {
                    TrackArtwork(store: store, trackID: id, radius: kind == .artist ? 140 : 16, large: true)
                        .frame(maxWidth: 300).padding(.horizontal, 36).shadow(color: .black.opacity(0.2), radius: 20, y: 12)
                }
                VStack(spacing: 6) {
                    Text(collection.title).font(.title2.bold()).multilineTextAlignment(.center)
                    Text(collection.subtitle).font(.title3).foregroundStyle(.white.opacity(0.75)).multilineTextAlignment(.center)
                    Text("\(rows.count) треков · \(NativeValue.time(rows.reduce(0) { $0 + $1.duration }))").font(.caption).foregroundStyle(.white.opacity(0.55))
                }
                HStack(spacing: 12) {
                    Button { store.run { try await store.playAll(rows, shuffled: false) } } label: { Label("Слушать", systemImage: "play.fill").frame(maxWidth: .infinity).padding(.vertical, 8) }.buttonStyle(.borderedProminent).tint(.white).foregroundStyle(.black).accessibilityIdentifier("nativeCollectionPlay")
                    Button { store.run { try await store.playAll(rows, shuffled: true) } } label: { Image(systemName: "shuffle").frame(width: 44, height: 44) }.buttonStyle(.bordered).tint(.white).accessibilityLabel("Перемешать коллекцию")
                }.disabled(store.busy || rows.isEmpty)
                if !complete { Text("Это загруженная часть коллекции. Вернитесь к списку и нажмите «Загрузить ещё», чтобы получить остальные треки.").font(.footnote).foregroundStyle(.secondary) }
                LazyVStack(spacing: 0) {
                    ForEach(rows) { track in
                        NativeTrackRow(store: store, track: track, rows: rows) { selectedTrack = track }
                        Divider().padding(.leading, 60).opacity(0.2)
                    }
                }
                NativeMessage(store: store)
            }.padding(20).frame(maxWidth: 660).frame(maxWidth: .infinity)
        }.background { NativeMusicBackdrop(store: store, trackID: collection.artworkID) }
            .navigationBarTitleDisplayMode(.inline).toolbarBackground(.hidden, for: .navigationBar)
            .sheet(item: $selectedTrack) { track in NativeTrackActions(store: store, track: track, onDelete: { removedTracks.insert($0) }) }
            .accessibilityIdentifier("nativeCollectionDetail")
    }
}
