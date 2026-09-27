import SwiftUI

struct NativeLyrics: Equatable {
    struct Line: Identifiable, Equatable {
        let id: Int
        let time: Double
        let text: String
    }
    let text: String
    let lines: [Line]
    let source: String
    let synced: Bool
    var empty: Bool { text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && lines.isEmpty }
    init(_ response: [String: Any]) {
        let value = response["lyrics"] as? [String: Any] ?? [:]
        text = String((value["text"] as? String ?? "").prefix(64_000))
        let sourceValue = value["source"] as? String ?? "none"
        source = ["embedded", "owner"].contains(sourceValue) ? sourceValue : "none"
        let parsed = (value["lines"] as? [[String: Any]] ?? []).prefix(2000).compactMap { row -> (Double, String)? in
            guard let number = row["time"] as? NSNumber, number.doubleValue.isFinite,
                  number.doubleValue >= 0, number.doubleValue <= 86_400,
                  let text = row["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            return (number.doubleValue, String(text.prefix(2000)))
        }.enumerated().sorted { $0.element.0 == $1.element.0 ? $0.offset < $1.offset : $0.element.0 < $1.element.0 }
        lines = parsed.enumerated().map { Line(id: $0.offset, time: $0.element.element.0, text: $0.element.element.1) }
        synced = value["synced"] as? Bool == true && !lines.isEmpty
    }
    func activeLine(at position: Double) -> Int? {
        guard synced, position.isFinite else { return nil }
        return lines.last(where: { $0.time <= position })?.id
    }
}

@MainActor struct NativeLyricsContent: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var lyrics: NativeLyrics?
    @State private var error: String?
    @State private var retry = 0
    @State private var follow = true
    private var active: Int? { lyrics?.activeLine(at: store.position) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let error = error {
                VStack(alignment: .leading, spacing: 12) { Label("Текст пока недоступен", systemImage: "wifi.exclamationmark").font(.headline); Text(error).font(.callout); Button("Повторить") { retry += 1 } }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            } else if let lyrics = lyrics {
                if lyrics.empty {
                    VStack(spacing: 14) {
                        Image(systemName: "quote.bubble").font(.system(size: 36)).foregroundStyle(.white.opacity(0.5))
                        Text("У этого трека нет текста").font(.title3.weight(.semibold))
                        Text("В аудиофайле нет встроенного текста. XASS не подставляет чужие тексты и не ищет их в интернете.").font(.callout).foregroundStyle(.white.opacity(0.65)).multilineTextAlignment(.center)
                    }.frame(maxWidth: .infinity, maxHeight: .infinity).accessibilityIdentifier("nativeLyricsEmpty")
                } else {
                    ScrollViewReader { proxy in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 24) {
                                if lyrics.synced {
                                    ForEach(lyrics.lines) { line in
                                        Button { store.run { try await store.seek(line.time) } } label: {
                                            Text(line.text).font(.title.weight(.bold)).multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
                                                .foregroundStyle(.white.opacity(active == line.id ? 1 : 0.42)).padding(.vertical, 5)
                                        }.buttonStyle(.plain).disabled(store.busy).id(line.id).accessibilityIdentifier("lyric-line-\(line.id)")
                                    }
                                } else { Text(lyrics.text.isEmpty ? lyrics.lines.map(\.text).joined(separator: "\n") : lyrics.text).font(.title2.weight(.semibold)).lineSpacing(12).textSelection(.enabled).accessibilityIdentifier("nativePlainLyrics") }
                                Text(lyrics.source == "embedded" ? "Текст из аудиофайла" : "Добавлено владельцем").font(.caption).foregroundStyle(.white.opacity(0.5))
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 18)
                        }.onChange(of: active) { _, id in
                            guard follow, let id = id else { return }
                            if reduceMotion { proxy.scrollTo(id, anchor: .center) }
                            else { withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(id, anchor: .center) } }
                        }.onAppear { if let active = active { proxy.scrollTo(active, anchor: .center) } }
                    }
                    if lyrics.synced {
                        Toggle("Следить за воспроизведением", isOn: $follow).font(.caption).tint(.white.opacity(0.5))
                    }
                }
            } else { ProgressView("Загрузка текста…").frame(maxWidth: .infinity, maxHeight: .infinity) }
        }.task(id: "\(trackID)-\(retry)") {
            lyrics = nil; error = nil
            do {
                let response = try await store.api.request("/api/mini/music/tracks/\(trackID)/lyrics", method: "GET", body: nil)
                try Task.checkCancellation(); lyrics = NativeLyrics(response)
            } catch is CancellationError { }
            catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }.accessibilityIdentifier("nativeLyrics")
    }
}
