import SwiftUI
import Foundation
import CoreFoundation
import Combine

/// One synced line. `start`/`end` are seconds into the track.
struct SyncedLyricLine: Identifiable, Equatable, Codable {
    let id: Int
    let start: Double
    let end: Double
    let text: String
}

/// Apple-Music-style lyrics for one track, as returned by
/// `GET /api/mini/music/tracks/{id}/timed-lyrics` and cached on disk.
struct SyncedLyrics: Equatable, Codable {
    let trackID: Int
    /// synced | plain | instrumental | not_found | unavailable | insufficient_metadata
    let status: String
    let synced: Bool
    let source: String
    let lines: [SyncedLyricLine]
    let text: String
    var cachedAt: Date = Date()
    /// A PC transcription job for this track is queued or running: re-check soon.
    var transcriptionPending: Bool? = nil

    init(trackID: Int, status: String, synced: Bool, source: String, lines: [SyncedLyricLine], text: String, cachedAt: Date = Date()) {
        self.trackID = trackID; self.status = status; self.synced = synced; self.source = source
        self.lines = lines; self.text = text; self.cachedAt = cachedAt
    }

    init?(response: [String: Any], trackID: Int) {
        guard let value = response["lyrics"] as? [String: Any] else { return nil }
        var parsed: [SyncedLyricLine] = []
        for row in (value["lines"] as? [[String: Any]] ?? []).prefix(2000) {
            func seconds(_ raw: Any?) -> Double {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite else { return -1 }
                return number.doubleValue
            }
            let start = seconds(row["start"]), end = seconds(row["end"])
            guard start >= 0, start <= 86_400, end >= start, let text = row["text"] as? String, !text.isEmpty else { continue }
            let shown = NativeLRCText.plainText(String(text.prefix(500)))
            guard !shown.isEmpty else { continue }
            parsed.append(SyncedLyricLine(id: parsed.count, start: start, end: end, text: shown))
        }
        parsed.sort { $0.start < $1.start }
        lines = parsed.enumerated().map { SyncedLyricLine(id: $0.offset, start: $0.element.start, end: $0.element.end, text: $0.element.text) }
        self.trackID = trackID
        let raw = value["status"] as? String ?? "not_found"
        status = ["synced", "plain", "instrumental", "not_found", "unavailable", "insufficient_metadata"].contains(raw) ? raw : "not_found"
        synced = value["synced"] as? Bool == true && !lines.isEmpty
        source = String((value["source"] as? String ?? "none").prefix(40))
        // Plain lyrics are shown as text: never with raw LRC tags.
        text = NativeLRCText.plainText(String((value["text"] as? String ?? "").prefix(64_000)))
        transcriptionPending = value["transcription_pending"] as? Bool == true ? true : nil
    }

    /// Server-side PC transcription (Demucs + Whisper): shown with a caution label.
    var isAutomatic: Bool { source == "pc_transcription" }

    var isEmpty: Bool { lines.isEmpty && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    /// Index of the line being sung at `position`, nil before the first line
    /// and inside instrumental gaps (after a line's end, before the next start).
    func lineIndex(at position: Double) -> Int? {
        guard synced, position.isFinite, !lines.isEmpty else { return nil }
        var lower = 0, upper = lines.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if lines[middle].start <= position { lower = middle + 1 } else { upper = middle }
        }
        let index = lower - 1
        guard index >= 0, position < lines[index].end else { return nil }
        return index
    }

    var sourceLabel: String {
        switch source {
        case "lrclib": return "LRCLIB"
        case "embedded": return "Из аудиофайла"
        case "owner": return "Добавлено вами"
        case "on_device_transcription": return "Расшифровка"
        case "pc_transcription": return PCTranscriptionStatus.automaticLabel
        default: return ""
        }
    }
}

/// Loads, caches (disk, per server + track) and prefetches synced lyrics, and
/// publishes the active line for the current playback position.
@MainActor final class SyncedLyricsStore: ObservableObject {
    @Published private(set) var current: SyncedLyrics?
    @Published private(set) var currentTrackID: Int?
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    /// Index into `current.lines`; updated from AVPlayer's periodic observer
    /// (local) or the PC session position (remote).
    @Published private(set) var currentLyricIndex: Int?

    static let foundTTL: TimeInterval = 14 * 86_400
    static let missTTL: TimeInterval = 6 * 3_600
    static let pendingTTL: TimeInterval = 30
    static let pendingRecheck: Duration = .seconds(45)
    private var pendingTask: Task<Void, Never>?
    private var memory: [Int: SyncedLyrics] = [:]
    private var inflight: [Int: Task<SyncedLyrics?, Never>] = [:]
    private let directory: URL?
    private weak var api: OwnerService?

    init(namespace: String, root: URL? = nil) {
        let base = root ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
        let folder = base?.appendingPathComponent("XASSLyrics", isDirectory: true).appendingPathComponent(namespace, isDirectory: true)
        if let folder = folder { try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
        directory = folder
    }

    func attach(_ api: OwnerService) { self.api = api }
    /// Background network fetch on track change. Unit tests that count exact
    /// requests on fixture services turn this off; the app and UI tests keep it.
    var autoFetch = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil

    /// Called whenever the playing track changes. Shows cached lyrics at once,
    /// refreshes in the background, and warms the next track.
    func trackChanged(to id: Int?, next: Int?) {
        guard id != currentTrackID else { return }
        currentTrackID = id; currentLyricIndex = nil; error = nil
        current = id.flatMap { cached($0) }
        guard let id = id, autoFetch else { loading = false; return }
        loading = current == nil
        Task { [weak self] in
            guard let self = self else { return }
            let value = await self.fetch(id)
            guard self.currentTrackID == id else { return }
            if let value = value { self.current = value } else if self.current == nil { self.error = "Нет связи с сервером. Текст появится, когда сеть вернётся." }
            self.loading = false
            self.recheckWhilePending(id)
        }
        if let next = next, next != id { prefetch(next) }
    }

    /// Drops the cached copy (e.g. a PC transcription just finished) and reloads it if it is playing.
    func invalidate(_ id: Int) {
        memory.removeValue(forKey: id)
        if let file = file(id) { try? FileManager.default.removeItem(at: file) }
        guard id == currentTrackID, autoFetch else { return }
        Task { [weak self] in
            guard let self = self else { return }
            let value = await self.fetch(id)
            if self.currentTrackID == id, let value = value { self.current = value }
        }
    }

    /// While the server transcribes the playing track, re-check so Now Playing shows the text by itself.
    private func recheckWhilePending(_ id: Int) {
        pendingTask?.cancel()
        guard current?.trackID == id, current?.transcriptionPending == true, autoFetch else { return }
        pendingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: Self.pendingRecheck)
                guard let self = self, !Task.isCancelled, self.currentTrackID == id else { return }
                self.memory.removeValue(forKey: id)
                guard let value = await self.fetch(id), self.currentTrackID == id else { continue }
                self.current = value
                if value.transcriptionPending != true { return }
            }
        }
    }

    func prefetch(_ id: Int) {
        guard autoFetch, id > 0, cached(id) == nil else { return }
        Task { [weak self] in _ = await self?.fetch(id) }
    }

    /// Awaitable load (used by retry paths and tests).
    @discardableResult func load(_ id: Int) async -> SyncedLyrics? {
        let value = await fetch(id)
        if currentTrackID == id, let value = value { current = value }
        return value
    }

    func retry() {
        guard let id = currentTrackID else { return }
        memory.removeValue(forKey: id); loading = current == nil; error = nil
        Task { [weak self] in
            guard let self = self else { return }
            let value = await self.fetch(id)
            guard self.currentTrackID == id else { return }
            if let value = value { self.current = value } else if self.current == nil { self.error = "Нет связи с сервером. Повторите позже." }
            self.loading = false
        }
    }

    func tick(position: Double, trackID: Int?) {
        guard let lyrics = current, lyrics.trackID == trackID else {
            if currentLyricIndex != nil { currentLyricIndex = nil }
            return
        }
        let index = lyrics.lineIndex(at: position)
        if index != currentLyricIndex { currentLyricIndex = index }
    }

    func cached(_ id: Int) -> SyncedLyrics? {
        if let value = memory[id], fresh(value) { return value }
        guard let file = file(id), let data = try? Data(contentsOf: file),
              let value = try? JSONDecoder().decode(SyncedLyrics.self, from: data), value.trackID == id else { return nil }
        memory[id] = value
        // Stale disk entries still display offline; fetch() refreshes them.
        return value
    }

    private func fresh(_ value: SyncedLyrics) -> Bool {
        if value.transcriptionPending == true { return Date().timeIntervalSince(value.cachedAt) < Self.pendingTTL }
        let ttl = value.synced || value.status == "plain" || value.status == "instrumental" ? Self.foundTTL : Self.missTTL
        return Date().timeIntervalSince(value.cachedAt) < ttl
    }

    private func file(_ id: Int) -> URL? { directory?.appendingPathComponent("\(max(0, id)).json") }

    private func fetch(_ id: Int) async -> SyncedLyrics? {
        if let value = memory[id], fresh(value) { return value }
        if let running = inflight[id] { return await running.value }
        guard let api = api else { return nil }
        let task = Task<SyncedLyrics?, Never> { @MainActor in
            do {
                let response = try await api.request("/api/mini/music/tracks/\(id)/timed-lyrics", method: "GET", body: nil)
                return SyncedLyrics(response: response, trackID: id)
            } catch { return nil }
        }
        inflight[id] = task
        let value = await task.value
        inflight.removeValue(forKey: id)
        guard let value = value else { return memory[id] }
        // Provider outages are not cached as "no lyrics".
        if value.status != "unavailable" || memory[id] == nil {
            memory[id] = value
            if value.status != "unavailable", let file = file(id), let data = try? JSONEncoder().encode(value) {
                try? data.write(to: file, options: .atomic)
            }
        }
        return memory[id]
    }
}
