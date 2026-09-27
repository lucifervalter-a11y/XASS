import SwiftUI
import CoreFoundation

struct NativeLyrics: Equatable {
    struct Line: Identifiable, Equatable {
        let id: Int
        let time: Double
        let text: String
        var isPause: Bool { text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
    let text: String
    let lines: [Line]
    let source: String
    let sourceURL: URL?
    let status: String
    let synced: Bool
    var empty: Bool { text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && lines.allSatisfy(\.isPause) }
    var sourceLabel: String {
        switch source {
        case "embedded": return "Из аудиофайла"
        case "owner": return "Добавлено владельцем"
        case "lrclib": return "Текст · LRCLIB"
        case "on_device_transcription": return "Расшифровка на устройстве"
        default: return "Текст песни"
        }
    }
    init(_ response: [String: Any]) {
        let value = response["lyrics"] as? [String: Any] ?? [:]
        text = String((value["text"] as? String ?? "").prefix(64_000))
        let sourceValue = value["source"] as? String ?? "none"
        source = ["embedded", "owner", "lrclib", "on_device_transcription"].contains(sourceValue) ? sourceValue : "none"
        sourceURL = Self.provenanceURL(value["source_url"] as? String, source: source)
        let enrichment = response["enrichment"] as? [String: Any] ?? [:]
        let statusValue = value["status"] as? String ?? enrichment["status"] as? String ?? ""
        status = ["matched", "candidate", "ambiguous", "not_found", "insufficient_metadata", "unavailable", "rate_limited", "instrumental"].contains(statusValue) ? statusValue : ""
        let parsed = (value["lines"] as? [[String: Any]] ?? []).prefix(2000).compactMap { row -> (Double, String)? in
            guard let number = row["time"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite, number.doubleValue >= 0, number.doubleValue <= 86_400,
                  let text = row["text"] as? String else { return nil }
            return (number.doubleValue, String(text.prefix(2000)))
        }.enumerated().sorted { $0.element.0 == $1.element.0 ? $0.offset < $1.offset : $0.element.0 < $1.element.0 }
        lines = parsed.enumerated().map { Line(id: $0.offset, time: $0.element.element.0, text: $0.element.element.1) }
        synced = value["synced"] as? Bool == true && lines.contains { !$0.isPause }
    }
    static func provenanceURL(_ raw: String?, source: String) -> URL? {
        guard source == "lrclib", let raw = raw, raw.count <= 512,
              let parts = URLComponents(string: raw), parts.scheme == "https", parts.host == "lrclib.net",
              parts.user == nil, parts.password == nil, parts.port == nil || parts.port == 443,
              parts.query == nil, parts.fragment == nil,
              parts.path.range(of: #"^/lyrics/[0-9]+$"#, options: .regularExpression) != nil else { return nil }
        return parts.url
    }
    func activeLine(at position: Double) -> Int? {
        guard synced, position.isFinite, position >= 0 else { return nil }
        // Upper-bound search keeps long LRC files cheap at playback cadence.
        var lower = 0, upper = lines.count
        while lower < upper {
            let middle = (lower + upper) / 2
            if lines[middle].time <= position { lower = middle + 1 } else { upper = middle }
        }
        guard lower > 0 else { return nil }
        let time = lines[lower - 1].time
        // Empty timed LRC rows end a vocal phrase. Keep them as real pause
        // markers, but do not let a simultaneous blank hide a duet/translation.
        while lower > 0, lines[lower - 1].time == time {
            if !lines[lower - 1].isPause { return lines[lower - 1].id }
            lower -= 1
        }
        return nil
    }
    func isActive(_ line: Line, at position: Double) -> Bool {
        guard !line.isPause, let id = activeLine(at: position) else { return false }
        // Simultaneous translation/duet lines share real timestamps, not made-up intervals.
        return line.time == lines[id].time
    }
    func seekTime(lineID: Int, displayedTrackID: Int, currentTrackID: Int?) -> Double? {
        guard synced, currentTrackID == displayedTrackID, lines.indices.contains(lineID), !lines[lineID].isPause else { return nil }
        return lines[lineID].time
    }
}

enum NativeLyricsClock {
    static let maximumProjection: TimeInterval = 6
    static func projectionLimit(serverTime: String?, updatedAt: String?) -> TimeInterval {
        func date(_ raw: String?) -> Date? {
            guard let raw = raw, raw.count <= 80 else { return nil }
            let format = ISO8601DateFormatter()
            format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let value = format.date(from: raw) { return value }
            format.formatOptions = [.withInternetDateTime]
            return format.date(from: raw)
        }
        guard let server = date(serverTime), let updated = date(updatedAt) else { return 0 }
        let age = server.timeIntervalSince(updated)
        guard age.isFinite, age >= 0 else { return 0 }
        // /session has already projected the server's reported position by at
        // most 15s. Never restart that budget just because another poll arrived.
        return min(maximumProjection, max(0, 15 - age))
    }
    static func position(_ sample: Double, duration: Double, playing: Bool, sampledAt: Date, now: Date,
                         projectionLimit: TimeInterval = maximumProjection) -> Double {
        guard sample.isFinite else { return 0 }
        var result = max(0, sample)
        if playing, sampledAt != .distantPast {
            let elapsed = now.timeIntervalSince(sampledAt)
            if elapsed.isFinite, projectionLimit.isFinite {
                result += min(maximumProjection, min(max(0, projectionLimit), max(0, elapsed)))
            }
        }
        if duration.isFinite, duration > 0 { result = min(duration, result) }
        return result
    }
}

struct NativeLyricsFollowing: Equatable {
    private(set) var enabled = true
    mutating func pause() { enabled = false }
    mutating func resume() { enabled = true }
    mutating func drag(horizontal: Double, vertical: Double) {
        if abs(vertical) >= 12 && abs(vertical) > abs(horizontal) { pause() }
    }
}

@MainActor struct NativeLyricsContent: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @AccessibilityFocusState private var focusedLine: Int?
    @State private var lyrics: NativeLyrics?
    @State private var error: String?
    @State private var retry = 0
    @State private var showInformation = false
    @State private var following = NativeLyricsFollowing()

    var body: some View {
        Group {
            if let error = error {
                VStack(spacing: 12) {
                    Label("Текст пока недоступен", systemImage: "wifi.exclamationmark").font(.headline)
                    Text(error).font(.callout).multilineTextAlignment(.center)
                    Button("Повторить") { retry += 1 }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let lyrics = lyrics {
                if lyrics.empty { emptyLyrics(lyrics) }
                else {
                    TimelineView(.animation(minimumInterval: 0.1, paused: !store.playing || scenePhase != .active)) { context in
                        let position = NativeLyricsClock.position(store.position, duration: store.duration,
                            playing: store.playing && store.currentID == trackID,
                            sampledAt: store.playbackSampleAt, now: context.date,
                            projectionLimit: store.playbackProjectionLimit)
                        lyricViewport(lyrics, position: position)
                    }
                }
            } else {
                ProgressView("Ищем текст песни…").frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }.task(id: "\(trackID)-\(retry)") {
            lyrics = nil; error = nil; following.resume(); focusedLine = nil
            do {
                let response = try await store.api.request("/api/mini/music/tracks/\(trackID)/lyrics", method: "GET", body: nil)
                try Task.checkCancellation(); lyrics = NativeLyrics(response)
                store.applyEnrichedTrack(response)
            } catch is CancellationError { }
            catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }.sheet(isPresented: $showInformation, onDismiss: { retry += 1 }) {
            NavigationStack {
                NativeEnrichmentView(store: store, trackID: trackID).toolbar {
                    ToolbarItem(placement: .confirmationAction) { Button("Готово") { showInformation = false } }
                }
            }
        }.accessibilityIdentifier("nativeLyrics")
    }

    @ViewBuilder private func emptyLyrics(_ lyrics: NativeLyrics) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "quote.bubble").font(.system(size: 32)).foregroundStyle(.white.opacity(0.5))
            Text(lyrics.status == "instrumental" ? "Инструментальная композиция" : "Текст пока не найден")
                .font(.title3.weight(.semibold))
            Text(emptyExplanation(lyrics.status)).font(.callout).foregroundStyle(.white.opacity(0.65))
                .multilineTextAlignment(.center)
            if lyrics.status != "instrumental" {
                Button("Найти или расшифровать") { showInformation = true }.font(.callout.weight(.semibold))
            }
        }.padding(.horizontal, 12).frame(maxWidth: .infinity, maxHeight: .infinity).accessibilityIdentifier("nativeLyricsEmpty")
    }

    private func emptyExplanation(_ status: String) -> String {
        switch status {
        case "unavailable", "rate_limited": return "Источник текста временно недоступен. Попробуйте позже."
        case "candidate", "ambiguous", "insufficient_metadata": return "Не удалось уверенно определить запись. Уточните название и исполнителя: чужой текст не будет подставлен автоматически."
        case "instrumental": return "Для этой записи источник не указал вокальный текст."
        default: return "В файле и доступных источниках нет подтверждённого текста для этой записи."
        }
    }

    @ViewBuilder private func lyricViewport(_ lyrics: NativeLyrics, position: Double) -> some View {
        let active = store.currentID == trackID ? lyrics.activeLine(at: position) : nil
        ScrollViewReader { proxy in
            VStack(spacing: 6) {
                GeometryReader { geometry in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 22) {
                            if lyrics.synced {
                                ForEach(lyrics.lines.filter { !$0.isPause }) { line in
                                    lyricLine(line, lyrics: lyrics, active: active)
                                        .id(line.id).accessibilityFocused($focusedLine, equals: line.id)
                                }
                            } else {
                                Text(lyrics.text.isEmpty ? lyrics.lines.map(\.text).joined(separator: "\n") : lyrics.text)
                                    .font(.title2.weight(.semibold)).lineSpacing(12).textSelection(.enabled)
                                    .foregroundStyle(.white.opacity(0.88)).accessibilityIdentifier("nativePlainLyrics")
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 8)
                            .padding(.vertical, lyrics.synced ? max(24, geometry.size.height * 0.34) : 18)
                    }.scrollIndicators(.hidden)
                        .simultaneousGesture(DragGesture(minimumDistance: 12).onChanged { value in
                            following.drag(horizontal: Double(value.translation.width), vertical: Double(value.translation.height))
                        })
                        .mask {
                            LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.08),
                                .init(color: .black, location: 0.88), .init(color: .clear, location: 1)],
                                startPoint: .top, endPoint: .bottom)
                        }.onChange(of: active) { _, id in
                            guard following.enabled else { return }; scroll(proxy, to: id)
                        }.onChange(of: following.enabled) { _, enabled in
                            if enabled { scroll(proxy, to: active) }
                        }.onChange(of: focusedLine) { _, id in
                            if let id = id, id != active { following.pause() }
                        }.onAppear { scroll(proxy, to: active, animated: false) }
                }
                HStack(spacing: 12) {
                    if let sourceURL = lyrics.sourceURL { Link(lyrics.sourceLabel, destination: sourceURL) }
                    else { Text(lyrics.sourceLabel) }
                    Spacer(minLength: 4)
                    if lyrics.synced {
                        Button {
                            if following.enabled { following.pause() }
                            else { following.resume(); scroll(proxy, to: active) }
                        } label: {
                            Label(following.enabled ? "Следить" : "К текущей строке",
                                  systemImage: following.enabled ? "waveform" : "arrow.uturn.backward")
                                .font(.caption.weight(.semibold)).padding(.horizontal, 10).padding(.vertical, 8)
                                .background(.white.opacity(following.enabled ? 0.07 : 0.16), in: Capsule())
                        }.buttonStyle(.plain).accessibilityIdentifier("nativeLyricsFollow")
                            .accessibilityLabel(following.enabled ? "Приостановить автопрокрутку" : "Вернуться к текущей строке")
                            .accessibilityValue(following.enabled ? "Автопрокрутка включена" : "Автопрокрутка приостановлена")
                    } else { Text("Без тайминга") }
                }.font(.caption2).foregroundStyle(.white.opacity(0.62)).padding(.horizontal, 8)
            }
        }
    }

    private func lyricLine(_ line: NativeLyrics.Line, lyrics: NativeLyrics, active: Int?) -> some View {
        let highlighted = active.map { lyrics.lines[$0].time == line.time } ?? false
        return Button {
            guard let time = lyrics.seekTime(lineID: line.id, displayedTrackID: trackID, currentTrackID: store.currentID) else { return }
            store.run {
                guard store.currentID == trackID else { return }
                try await store.seek(time)
                if store.currentID == trackID { following.resume() }
            }
        } label: {
            Text(line.text).font(.title.weight(.bold)).multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
                .foregroundStyle(.white.opacity(highlighted ? 1 : 0.38))
                .scaleEffect(reduceMotion || highlighted ? 1 : 0.965, anchor: .leading)
                .shadow(color: .white.opacity(highlighted ? 0.10 : 0), radius: 10, y: 1)
                .animation(reduceMotion ? nil : .spring(response: 0.48, dampingFraction: 0.9), value: highlighted)
        }.buttonStyle(.plain).disabled(store.busy || store.currentID != trackID)
            .accessibilityIdentifier("lyric-line-\(line.id)")
            .accessibilityValue(highlighted ? "Текущая строка" : "")
            .accessibilityHint("Перейти к этой строке в песне")
    }

    private func scroll(_ proxy: ScrollViewProxy, to id: Int?, animated: Bool = true) {
        guard let id = id else { return }
        if reduceMotion || !animated { proxy.scrollTo(id, anchor: UnitPoint(x: 0.5, y: 0.38)) }
        else { withAnimation(.easeInOut(duration: 0.42)) { proxy.scrollTo(id, anchor: UnitPoint(x: 0.5, y: 0.38)) } }
    }
}
