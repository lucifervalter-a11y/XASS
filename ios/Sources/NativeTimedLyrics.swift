import SwiftUI
import Foundation
import CoreFoundation
import Combine

/// One synced line. `start`/`end` are seconds into the track.
struct TimedLyricLine: Identifiable, Equatable, Codable {
    let id: Int
    let start: Double
    let end: Double
    let text: String
}

/// Apple-Music-style lyrics for one track, as returned by
/// `GET /api/mini/music/tracks/{id}/timed-lyrics` and cached on disk.
struct TimedLyrics: Equatable, Codable {
    let trackID: Int
    /// synced | plain | instrumental | not_found | unavailable | insufficient_metadata
    let status: String
    let synced: Bool
    let source: String
    let lines: [TimedLyricLine]
    let text: String
    var cachedAt: Date = Date()

    init(trackID: Int, status: String, synced: Bool, source: String, lines: [TimedLyricLine], text: String, cachedAt: Date = Date()) {
        self.trackID = trackID; self.status = status; self.synced = synced; self.source = source
        self.lines = lines; self.text = text; self.cachedAt = cachedAt
    }

    init?(response: [String: Any], trackID: Int) {
        guard let value = response["lyrics"] as? [String: Any] else { return nil }
        var parsed: [TimedLyricLine] = []
        for row in (value["lines"] as? [[String: Any]] ?? []).prefix(2000) {
            func seconds(_ raw: Any?) -> Double {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite else { return -1 }
                return number.doubleValue
            }
            let start = seconds(row["start"]), end = seconds(row["end"])
            guard start >= 0, start <= 86_400, end >= start, let text = row["text"] as? String, !text.isEmpty else { continue }
            parsed.append(TimedLyricLine(id: parsed.count, start: start, end: end, text: String(text.prefix(500))))
        }
        parsed.sort { $0.start < $1.start }
        lines = parsed.enumerated().map { TimedLyricLine(id: $0.offset, start: $0.element.start, end: $0.element.end, text: $0.element.text) }
        self.trackID = trackID
        let raw = value["status"] as? String ?? "not_found"
        status = ["synced", "plain", "instrumental", "not_found", "unavailable", "insufficient_metadata"].contains(raw) ? raw : "not_found"
        synced = value["synced"] as? Bool == true && !lines.isEmpty
        source = String((value["source"] as? String ?? "none").prefix(40))
        text = String((value["text"] as? String ?? "").prefix(64_000))
    }

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
        default: return ""
        }
    }
}

/// Loads, caches (disk, per server + track) and prefetches synced lyrics, and
/// publishes the active line for the current playback position.
@MainActor final class TimedLyricsStore: ObservableObject {
    @Published private(set) var current: TimedLyrics?
    @Published private(set) var currentTrackID: Int?
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    /// Index into `current.lines`; updated from AVPlayer's periodic observer
    /// (local) or the PC session position (remote).
    @Published private(set) var currentLyricIndex: Int?

    static let foundTTL: TimeInterval = 14 * 86_400
    static let missTTL: TimeInterval = 6 * 3_600
    private var memory: [Int: TimedLyrics] = [:]
    private var inflight: [Int: Task<TimedLyrics?, Never>] = [:]
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
        }
        if let next = next, next != id { prefetch(next) }
    }

    func prefetch(_ id: Int) {
        guard autoFetch, id > 0, cached(id) == nil else { return }
        Task { [weak self] in _ = await self?.fetch(id) }
    }

    /// Awaitable load (used by retry paths and tests).
    @discardableResult func load(_ id: Int) async -> TimedLyrics? {
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

    func cached(_ id: Int) -> TimedLyrics? {
        if let value = memory[id], fresh(value) { return value }
        guard let file = file(id), let data = try? Data(contentsOf: file),
              let value = try? JSONDecoder().decode(TimedLyrics.self, from: data), value.trackID == id else { return nil }
        memory[id] = value
        // Stale disk entries still display offline; fetch() refreshes them.
        return value
    }

    private func fresh(_ value: TimedLyrics) -> Bool {
        let ttl = value.synced || value.status == "plain" || value.status == "instrumental" ? Self.foundTTL : Self.missTTL
        return Date().timeIntervalSince(value.cachedAt) < ttl
    }

    private func file(_ id: Int) -> URL? { directory?.appendingPathComponent("\(max(0, id)).json") }

    private func fetch(_ id: Int) async -> TimedLyrics? {
        if let value = memory[id], fresh(value) { return value }
        if let running = inflight[id] { return await running.value }
        guard let api = api else { return nil }
        let task = Task<TimedLyrics?, Never> { @MainActor in
            do {
                let response = try await api.request("/api/mini/music/tracks/\(id)/timed-lyrics", method: "GET", body: nil)
                return TimedLyrics(response: response, trackID: id)
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

/// Simple, working synced-lyrics screen: auto-scrolls to the active line,
/// highlights it, and seeks when a line is tapped. The visual design can be
/// replaced freely; everything it needs lives in `TimedLyricsStore`.
@MainActor struct NativeTimedLyricsView: View {
    @ObservedObject var store: NativeStore
    @ObservedObject var lyrics: TimedLyricsStore
    var onMore: (() -> Void)? = nil
    @State private var following = true

    var body: some View {
        Group {
            if let value = lyrics.current, value.trackID == store.currentID, !value.isEmpty {
                if value.synced { syncedList(value) } else { plain(value) }
            } else if lyrics.loading {
                ProgressView("Ищем текст песни…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "quote.bubble").font(.system(size: 32)).foregroundStyle(.white.opacity(0.5))
                    Text(lyrics.current?.status == "instrumental" ? "Инструментальная композиция" : "Текст пока не найден").font(.title3.weight(.semibold))
                    if let error = lyrics.error { Text(error).font(.callout).multilineTextAlignment(.center).foregroundStyle(.white.opacity(0.65)) }
                    HStack {
                        Button("Повторить") { lyrics.retry() }
                        if let onMore = onMore { Button("Найти или расшифровать") { onMore() } }
                    }.font(.callout.weight(.semibold))
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }.foregroundStyle(.white).accessibilityIdentifier("nativeTimedLyrics")
            .onReceive(Timer.publish(every: 0.2, on: .main, in: .common).autoconnect()) { now in
                // Smooth between 0.5 s AVPlayer ticks and 5 s PC heartbeats.
                let clock = store.playback
                let position = NativeLyricsClock.position(clock.position, duration: clock.duration, playing: store.playing,
                    sampledAt: clock.sampleAt, now: now, projectionLimit: clock.projectionLimit)
                lyrics.tick(position: position, trackID: store.currentID)
            }
    }

    private func syncedList(_ value: TimedLyrics) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    ForEach(value.lines) { line in
                        let active = lyrics.currentLyricIndex == line.id
                        Button {
                            following = true
                            store.run { try await store.seek(line.start) }
                        } label: {
                            Text(line.text).font(.title2.weight(.bold)).multilineTextAlignment(.leading)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .foregroundStyle(.white.opacity(active ? 1 : 0.35))
                                .scaleEffect(active ? 1 : 0.97, anchor: .leading)
                                .animation(.easeOut(duration: 0.25), value: active)
                        }.buttonStyle(.plain).id(line.id)
                            .accessibilityIdentifier("timed-lyric-\(line.id)")
                            .accessibilityValue(active ? "Текущая строка" : "")
                    }
                }.padding(.vertical, 140).padding(.horizontal, 8)
            }.scrollIndicators(.hidden)
                .simultaneousGesture(DragGesture(minimumDistance: 14).onChanged { _ in following = false })
                .onChange(of: lyrics.currentLyricIndex) { _, index in
                    guard following, let index = index else { return }
                    withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(index, anchor: UnitPoint(x: 0.5, y: 0.35)) }
                }
                .onAppear { if let index = lyrics.currentLyricIndex { proxy.scrollTo(index, anchor: UnitPoint(x: 0.5, y: 0.35)) } }
                .overlay(alignment: .bottomTrailing) {
                    if !following {
                        Button {
                            following = true
                            if let index = lyrics.currentLyricIndex { withAnimation { proxy.scrollTo(index, anchor: UnitPoint(x: 0.5, y: 0.35)) } }
                        } label: { Label("К текущей строке", systemImage: "arrow.uturn.backward").font(.caption.weight(.semibold)).padding(8).background(.white.opacity(0.16), in: Capsule()) }
                            .buttonStyle(.plain).padding(8)
                    }
                }
        }
    }

    private func plain(_ value: TimedLyrics) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(value.text).font(.title3.weight(.semibold)).lineSpacing(8).textSelection(.enabled).foregroundStyle(.white.opacity(0.88))
                Text("Текст без таймкодов").font(.caption).foregroundStyle(.white.opacity(0.5))
                if let onMore = onMore { Button("Найти или расшифровать") { onMore() }.font(.caption.weight(.semibold)) }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
