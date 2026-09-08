import Foundation

struct CachedAudio: Codable, Identifiable {
    let track: DownloadedTrack
    var bytes: Int64
    var lastUsed: Date
    var id: Int { track.id }
}
struct AudioCacheSnapshot {
    let limitMB: Int
    let entries: [CachedAudio]
    var bytes: Int64 { entries.reduce(0) { $0 + $1.bytes } }
}

/// The automatic cache has its own directory and index. It cannot enumerate or
/// evict the pinned OfflineLibrary. All index/file work runs off the main actor.
actor AudioCache {
    static let limits = [0, 256, 512, 1024]
    nonisolated let directory: URL
    private struct Play: Codable { var count: Int; var lastUsed: Date }
    private struct Index: Codable {
        var limitMB = 256
        var entries: [CachedAudio] = []
        var plays: [Int: Play] = [:]
    }
    private var index = Index()
    init(origin: ServerOrigin, root: URL? = nil) throws {
        let base = try root ?? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        directory = base.appendingPathComponent("XASSAutomaticAudio", isDirectory: true).appendingPathComponent(origin.namespace, isDirectory: true)
    }
    nonisolated func file(_ id: Int, suffix: String = "audio") -> URL {
        let safe = ["audio", "mp3", "m4a", "wav", "flac", "ogg"].contains(suffix) ? suffix : "audio"
        return directory.appendingPathComponent("\(max(0, id)).\(safe)")
    }
    func load() throws -> AudioCacheSnapshot {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var folder = directory; var values = URLResourceValues(); values.isExcludedFromBackup = true; try folder.setResourceValues(values)
        if let data = try? Data(contentsOf: directory.appendingPathComponent("index.json")), data.count <= 2 * 1024 * 1024,
           let saved = try? JSONDecoder().decode(Index.self, from: data), Self.limits.contains(saved.limitMB) { index = saved }
        var seen = Set<Int>()
        index.entries = index.entries.compactMap { entry in
            guard entry.id > 0, seen.insert(entry.id).inserted, let size = try? file(entry.id, suffix: entry.track.fileExtension ?? "audio").resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 0 else { return nil }
            var value = entry; value.bytes = Int64(size); return value
        }
        let indexed = Set(index.entries.map { file($0.id, suffix: $0.track.fileExtension ?? "audio").lastPathComponent })
        for url in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            where !indexed.contains(url.lastPathComponent) && url.lastPathComponent.range(of: #"^[0-9]+\.(audio|mp3|m4a|wav|flac|ogg)$"#, options: .regularExpression) != nil {
            try FileManager.default.removeItem(at: url)
        }
        try trim(); return snapshot()
    }
    func hasSpaceForDownload() -> Bool {
        let free = try? directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
        return free.map { $0 >= PrivateDownload.maxBytes + 32 * 1024 * 1024 } ?? false
    }
    func snapshot() -> AudioCacheSnapshot { AudioCacheSnapshot(limitMB: index.limitMB, entries: index.entries.sorted { $0.lastUsed > $1.lastUsed }) }
    func recordPlay(_ id: Int, at time: Date = Date()) throws -> Bool {
        guard id > 0 else { return false }
        index.plays[id] = Play(count: min(2, (index.plays[id]?.count ?? 0) + 1), lastUsed: time)
        if let entry = index.entries.firstIndex(where: { $0.id == id }) { index.entries[entry].lastUsed = time }
        if index.plays.count > 2000, let oldest = index.plays.min(by: { $0.value.lastUsed < $1.value.lastUsed })?.key { index.plays.removeValue(forKey: oldest) }
        try save()
        return index.limitMB > 0 && index.plays[id]?.count == 2 && !index.entries.contains { $0.id == id }
    }
    func insert(_ track: DownloadedTrack, at time: Date = Date(), protectedID: Int? = nil) throws -> AudioCacheSnapshot {
        let location = file(track.id, suffix: track.fileExtension ?? "audio")
        guard track.id > 0, let size = try location.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 0 else { throw XASSErr.invalidMedia }
        index.entries.removeAll { $0.id == track.id }
        index.entries.append(CachedAudio(track: track, bytes: Int64(size), lastUsed: time))
        try trim(protectedID: protectedID); return snapshot()
    }
    func setLimit(_ megabytes: Int, protectedID: Int? = nil) throws -> AudioCacheSnapshot {
        guard Self.limits.contains(megabytes) else { throw XASSErr.invalidCommand }
        index.limitMB = megabytes
        if megabytes == 0 { return try clear() }
        try trim(protectedID: protectedID); return snapshot()
    }
    func clear() throws -> AudioCacheSnapshot {
        // Only app-created numeric audio files in this cache namespace.
        let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        for url in files where url.lastPathComponent.range(of: #"^[0-9]+\.(audio|mp3|m4a|wav|flac|ogg)$"#, options: .regularExpression) != nil {
            try FileManager.default.removeItem(at: url)
        }
        index.entries = []; index.plays = [:]; try save(); return snapshot()
    }
    func promote(_ entry: CachedAudio, to library: OfflineLibrary) throws -> [DownloadedTrack] {
        let source = file(entry.id, suffix: entry.track.fileExtension ?? "audio"), target = library.file(for: entry.track)
        guard FileManager.default.fileExists(atPath: source.path) else { throw XASSErr.invalidMedia }
        if !FileManager.default.fileExists(atPath: target.path) { try FileManager.default.copyItem(at: source, to: target) }
        var tracks = library.tracks().filter { $0.id != entry.id }; tracks.append(entry.track); try library.save(tracks)
        return tracks
    }
    private func trim(protectedID: Int? = nil) throws {
        let limit = Int64(index.limitMB) * 1024 * 1024
        while index.entries.reduce(Int64(0), { $0 + $1.bytes }) > limit {
            guard let item = index.entries.filter({ $0.id != protectedID }).min(by: { $0.lastUsed < $1.lastUsed }) ?? index.entries.first else { break }
            let path = file(item.id, suffix: item.track.fileExtension ?? "audio")
            if FileManager.default.fileExists(atPath: path.path) { try FileManager.default.removeItem(at: path) }
            index.entries.removeAll { $0.id == item.id }
        }
        try save()
    }
    private func save() throws {
        try JSONEncoder().encode(index).write(to: directory.appendingPathComponent("index.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
}
