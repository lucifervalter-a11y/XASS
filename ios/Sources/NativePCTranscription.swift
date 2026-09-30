import SwiftUI
import CoreFoundation
import Foundation

/// Server-queued song transcription on the owner's PCs
/// (`/api/mini/music/tracks/{id}/transcription`). The iPhone only asks and
/// shows the status; Demucs + Whisper run on a PC with the XASS client.
struct PCTranscriptionStatus: Equatable {
    /// none | waiting_for_pc | preparing_pc | queued | running | done | failed | catalog_available
    let status: String
    let message: String
    let estimateMinutes: Int?
    /// First-time setup progress of an installing PC (only with `preparing_pc`).
    let setupPercent: Int?

    static let known: Set<String> = ["none", "waiting_for_pc", "preparing_pc", "queued", "running", "done", "failed", "catalog_available"]
    static let automaticLabel = "Автоматически, может быть с ошибками"

    init(status: String, message: String = "", estimateMinutes: Int? = nil, setupPercent: Int? = nil) {
        self.status = Self.known.contains(status) ? status : "none"
        self.message = message
        self.estimateMinutes = estimateMinutes
        self.setupPercent = setupPercent
    }

    init(response: [String: Any]) {
        let raw = response["status"] as? String ?? "none"
        var minutes: Int?
        if let number = response["estimate_minutes"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
           number.doubleValue.isFinite {
            minutes = max(1, min(600, number.intValue))
        }
        var percent: Int?
        if let number = response["setup_percent"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
           number.doubleValue.isFinite {
            percent = max(0, min(100, number.intValue))
        }
        self.init(status: raw, message: String((response["message"] as? String ?? "").prefix(200)), estimateMinutes: minutes,
                  setupPercent: percent)
    }

    /// Still worth polling: the result will arrive without another tap.
    var isActive: Bool { ["waiting_for_pc", "preparing_pc", "queued", "running"].contains(status) }
    /// The button stays while waiting for a PC: tapping again only joins the same job.
    var canRequest: Bool { ["none", "failed", "waiting_for_pc", "catalog_available"].contains(status) }

    var displayText: String {
        if !message.isEmpty { return message }
        switch status {
        case "waiting_for_pc": return "Расшифруем, когда включится компьютер"
        case "preparing_pc": return "ПК готовится к расшифровке, \(setupPercent ?? 0)%"
        case "queued": return "В очереди на расшифровку"
        case "running": return estimateMinutes.map { "Расшифровываем на компьютере · ~\($0) мин" } ?? "Расшифровываем на компьютере"
        case "done": return "Текст распознан. " + Self.automaticLabel
        case "failed": return "Не удалось распознать текст. Можно попробовать ещё раз."
        case "catalog_available": return "Текст уже есть в каталоге"
        default: return ""
        }
    }

    var symbol: String {
        switch status {
        case "waiting_for_pc": return "desktopcomputer"
        case "preparing_pc": return "arrow.down.circle"
        case "queued": return "clock"
        case "running": return "waveform"
        case "done": return "checkmark.circle"
        case "failed": return "exclamationmark.triangle"
        default: return "text.quote"
        }
    }

    /// Poll cadence: fast while a PC works, slower while a PC installs, slow while no PC is online.
    var pollInterval: Duration {
        switch status {
        case "waiting_for_pc": return .seconds(30)
        case "preparing_pc": return .seconds(15)
        default: return .seconds(8)
        }
    }
}

@MainActor final class PCTranscriptionModel: ObservableObject {
    @Published private(set) var status: PCTranscriptionStatus?
    @Published private(set) var working = false
    @Published private(set) var error: String?
    private var poller: Task<Void, Never>?
    private var pollGeneration = UUID()
    var isPolling: Bool { poller != nil }

    static func path(_ trackID: Int) -> String { "/api/mini/music/tracks/\(trackID)/transcription" }

    func start(api: OwnerService, trackID: Int, notifyIfAlreadyDone: Bool = false, onDone: @escaping () -> Void) {
        poller?.cancel()
        working = false
        let generation = UUID()
        pollGeneration = generation
        poller = Task { [weak self] in
            defer {
                if self?.pollGeneration == generation { self?.poller = nil }
            }
            do {
                let response = try await api.request(Self.path(trackID), method: "GET", body: nil)
                guard let self = self, !Task.isCancelled else { return }
                let value = PCTranscriptionStatus(response: response)
                let alreadyDone = value.status == "done"
                self.status = value; self.error = nil
                // The sheet's first look must not reload lyrics for an old result.
                // The follower that outlives the sheet does, so a job that finished
                // while the screen was closing still updates the text.
                if alreadyDone && notifyIfAlreadyDone { onDone() }
                await self.poll(api: api, trackID: trackID, onDone: onDone)
            } catch {
                // An older server without the queue: keep the section quiet.
                if !Task.isCancelled { self?.error = nil }
            }
        }
    }

    func request(api: OwnerService, trackID: Int, language: String, force: Bool = false,
                 onDone: @escaping () -> Void) {
        guard !working else { return }
        working = true; error = nil
        poller?.cancel()
        let generation = UUID()
        pollGeneration = generation
        poller = Task { [weak self] in
            defer {
                if self?.pollGeneration == generation { self?.poller = nil }
            }
            do {
                let response = try await api.request(Self.path(trackID), method: "POST",
                                                     body: ["language": language, "force": force])
                guard let self = self, !Task.isCancelled else { return }
                self.working = false
                let value = PCTranscriptionStatus(response: response)
                self.status = value
                if value.status == "done" { onDone() }
                await self.poll(api: api, trackID: trackID, onDone: onDone)
            } catch {
                guard let self = self, !Task.isCancelled else { return }
                self.working = false
                self.error = error.localizedDescription
            }
        }
    }

    func stop() {
        pollGeneration = UUID()
        poller?.cancel(); poller = nil; working = false
    }

    private func poll(api: OwnerService, trackID: Int, onDone: @escaping () -> Void) async {
        while let current = status, current.isActive, !Task.isCancelled {
            try? await Task.sleep(for: current.pollInterval)
            guard !Task.isCancelled else { return }
            guard let response = try? await api.request(Self.path(trackID), method: "GET", body: nil) else { continue }
            guard !Task.isCancelled else { return }
            let value = PCTranscriptionStatus(response: response)
            status = value
            if value.status == "done" { onDone() }
        }
    }
}

/// Minimal UI in the song information sheet: button, status and the
/// "automatic" label. Now Playing picks the text up by itself when done.
@MainActor struct PCTranscriptionSection: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    var forceTimings = false
    @StateObject private var model = PCTranscriptionModel()
    @AppStorage("xass.pcTranscription.language") private var language = "auto"

    var body: some View {
        Section("Распознать текст на компьютере") {
            Text(forceTimings
                 ? "Слова уже найдены, но точных таймкодов нет. Компьютер прослушает запись и привяжет строки к музыке."
                 : "Если в каталоге нет текста, песню расшифрует ваш компьютер с XASS, где включено «Использовать этот ПК для расшифровки текста». Текст сам появится в плеере.")
                .font(.footnote).foregroundStyle(.secondary)
            if let status = model.status, status.status != "none" {
                Label(status.displayText, systemImage: status.symbol).font(.callout)
                    .accessibilityIdentifier("pcTranscriptionStatus")
            }
            if model.status?.status == "done" {
                Text(PCTranscriptionStatus.automaticLabel).font(.caption).foregroundStyle(.orange)
            }
            if canRequest {
                Picker("Язык песни", selection: $language) {
                    Text("Определить автоматически").tag("auto"); Text("Русский").tag("ru"); Text("English").tag("en")
                }
                Button(model.working ? "Отправляю…" : requestLabel) {
                    model.request(api: store.api, trackID: trackID, language: language,
                                  force: requestsForcedPass, onDone: refreshLyrics)
                }
                .disabled(model.working)
                .accessibilityIdentifier("pcTranscriptionRequest")
            }
            if let error = model.error { Text(error).font(.footnote).foregroundStyle(.secondary) }
        }
        .task(id: trackID) { model.start(api: store.api, trackID: trackID, onDone: refreshLyrics) }
        .onDisappear {
            let unresolved = model.isPolling && model.status == nil
            let active = model.working || unresolved || model.status?.isActive == true
            model.stop()
            // Leave the poller on the store: closing this sheet must not freeze the lyrics screen.
            if active { store.followPCTranscription(trackID: trackID) }
        }
    }

    private func refreshLyrics() {
        store.lyrics.invalidate(trackID)
        store.invalidateLyrics()
    }

    private var requestsForcedPass: Bool {
        forceTimings || ["catalog_available", "done"].contains(model.status?.status ?? "")
    }
    private var canRequest: Bool {
        (model.status?.canRequest ?? true) || model.status?.status == "done"
    }
    private var requestLabel: String {
        if forceTimings || model.status?.status == "catalog_available" { return "Построить точные таймкоды на ПК" }
        if model.status?.status == "done" { return "Перепроверить текст и таймкоды на ПК" }
        return "Распознать текст"
    }
}

enum PCTranscriptionRequestPolicy {
    static func needsForcedTimings(_ response: [String: Any]) -> Bool {
        guard let lyrics = response["lyrics"] as? [String: Any] else { return false }
        let text = (lyrics["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let hasLines = !(lyrics["lines"] as? [[String: Any]] ?? []).isEmpty
        return !text.isEmpty && !(lyrics["synced"] as? Bool == true && hasLines)
    }
}

/// Direct destination from a track's action card. It checks the canonical
/// timed-lyrics response first, so a song with plain words gets an explicit
/// force/recheck action instead of the misleading "text already exists" stop.
@MainActor struct NativePCTranscriptionView: View {
    @ObservedObject var store: NativeStore
    let trackID: Int
    @State private var forceTimings = false
    @State private var loading = true

    var body: some View {
        Form {
            if loading { ProgressView("Проверяю текущий текст…") }
            PCTranscriptionSection(store: store, trackID: trackID, forceTimings: forceTimings)
        }
        .navigationTitle("Текст на компьютере")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: trackID) {
            defer { loading = false }
            guard let response = try? await store.api.request("/api/mini/music/tracks/\(trackID)/timed-lyrics",
                                                               method: "GET", body: nil), !Task.isCancelled else { return }
            forceTimings = PCTranscriptionRequestPolicy.needsForcedTimings(response)
        }
        .accessibilityIdentifier("nativePCTranscription")
    }
}
