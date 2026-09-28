import SwiftUI
import CoreFoundation

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

    static func path(_ trackID: Int) -> String { "/api/mini/music/tracks/\(trackID)/transcription" }

    func start(api: OwnerService, trackID: Int, onDone: @escaping () -> Void) {
        poller?.cancel()
        poller = Task { [weak self] in
            do {
                let response = try await api.request(Self.path(trackID), method: "GET", body: nil)
                guard let self = self, !Task.isCancelled else { return }
                self.status = PCTranscriptionStatus(response: response); self.error = nil
                await self.poll(api: api, trackID: trackID, onDone: onDone)
            } catch {
                // An older server without the queue: keep the section quiet.
                if !Task.isCancelled { self?.error = nil }
            }
        }
    }

    func request(api: OwnerService, trackID: Int, language: String, onDone: @escaping () -> Void) {
        guard !working else { return }
        working = true; error = nil
        poller?.cancel()
        poller = Task { [weak self] in
            do {
                let response = try await api.request(Self.path(trackID), method: "POST", body: ["language": language])
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

    func stop() { poller?.cancel(); poller = nil }

    private func poll(api: OwnerService, trackID: Int, onDone: @escaping () -> Void) async {
        while let current = status, current.isActive, !Task.isCancelled {
            try? await Task.sleep(for: current.pollInterval)
            guard !Task.isCancelled else { return }
            guard let response = try? await api.request(Self.path(trackID), method: "GET", body: nil) else { continue }
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
    @StateObject private var model = PCTranscriptionModel()
    @AppStorage("xass.pcTranscription.language") private var language = "ru"

    var body: some View {
        Section("Распознать текст на компьютере") {
            Text("Если в каталоге нет текста, песню расшифрует ваш компьютер с XASS, где включено «Использовать этот ПК для расшифровки текста». Текст сам появится в плеере.")
                .font(.footnote).foregroundStyle(.secondary)
            if let status = model.status, status.status != "none" {
                Label(status.displayText, systemImage: status.symbol).font(.callout)
                    .accessibilityIdentifier("pcTranscriptionStatus")
            }
            if model.status?.status == "done" {
                Text(PCTranscriptionStatus.automaticLabel).font(.caption).foregroundStyle(.orange)
            }
            if model.status?.canRequest ?? true {
                Picker("Язык песни", selection: $language) {
                    Text("Русский").tag("ru"); Text("English").tag("en"); Text("Определить автоматически").tag("auto")
                }
                Button(model.working ? "Отправляю…" : "Распознать текст") {
                    model.request(api: store.api, trackID: trackID, language: language, onDone: refreshLyrics)
                }
                .disabled(model.working)
                .accessibilityIdentifier("pcTranscriptionRequest")
            }
            if let error = model.error { Text(error).font(.footnote).foregroundStyle(.secondary) }
        }
        .task(id: trackID) { model.start(api: store.api, trackID: trackID, onDone: refreshLyrics) }
        .onDisappear { model.stop() }
    }

    private func refreshLyrics() {
        store.lyrics.invalidate(trackID)
        store.invalidateLyrics()
    }
}
