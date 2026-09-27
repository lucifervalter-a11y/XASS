import Foundation

// Deliberately not a string logger: callers cannot pass URLs, errors, payloads,
// names, paths, credentials or identifiers into an event.
enum NativeDiagnosticOperation: String, Codable, CaseIterable {
    case musicLibrary = "music_library", musicSession = "music_session", musicPlayers = "music_players"
    case musicControl = "music_control", musicTransfer = "music_transfer", musicTicket = "music_ticket"
    case musicLyrics = "music_lyrics", audioPlayback = "audio_playback"
    case artwork, download, upload, bootstrap, authentication
    case agentCommand = "agent_command", workspace, other

    var title: String {
        switch self {
        case .musicLibrary: return "Библиотека музыки"
        case .musicSession: return "Состояние плеера"
        case .musicPlayers: return "Устройства воспроизведения"
        case .musicControl: return "Управление музыкой"
        case .musicTransfer: return "Перенос воспроизведения"
        case .musicTicket: return "Доступ к аудио"
        case .musicLyrics: return "Текст песни"
        case .audioPlayback: return "Воспроизведение аудио"
        case .artwork: return "Обложка"
        case .download: return "Загрузка на iPhone"
        case .upload: return "Отправка файла"
        case .bootstrap: return "Загрузка приложения"
        case .authentication: return "Авторизация"
        case .agentCommand: return "Команда ПК"
        case .workspace: return "Рабочие инструменты"
        case .other: return "Другая операция"
        }
    }
}

enum NativeDiagnosticStep: String, Codable, CaseIterable {
    case started, response, completed, failed, cancelled, requested, acknowledged, waiting
}

enum NativeDiagnosticTarget: String, Codable, CaseIterable {
    case server, localPlayer = "local_player", pcPlayer = "pc_player"
}

enum NativeDiagnosticError: String, Codable, CaseIterable {
    case none, network, timeout, cancelled, unauthorized, forbidden, http, html
    case invalidJSON = "invalid_json", invalidEnvelope = "invalid_envelope", unsupportedResponse = "unsupported_response"
    case tooLarge = "too_large", conflict, invalidState = "invalid_state", unknown
    case sourceTimeout = "source_timeout", sourceStopFailed = "source_stop_failed"
    case targetStartFailed = "target_start_failed", targetUnavailable = "target_unavailable"
    case transferCancelled = "transfer_cancelled", replaced, agentCommandFailed = "agent_command_failed"
}

struct NativeDiagnosticEvent: Codable, Equatable {
    let time: Date
    let operation: NativeDiagnosticOperation
    let step: NativeDiagnosticStep
    let target: NativeDiagnosticTarget
    let httpStatus: Int?
    let envelopeStatus: Int?
    let error: NativeDiagnosticError
    let byteCount: Int?
    let elapsedMilliseconds: Int?

    fileprivate init(time: Date, operation: NativeDiagnosticOperation, step: NativeDiagnosticStep,
                     target: NativeDiagnosticTarget, httpStatus: Int?, envelopeStatus: Int?,
                     error: NativeDiagnosticError, byteCount: Int?, elapsedMilliseconds: Double?) {
        self.time = time; self.operation = operation; self.step = step; self.target = target; self.error = error
        self.httpStatus = httpStatus.flatMap { (100...599).contains($0) ? $0 : nil }
        self.envelopeStatus = envelopeStatus.flatMap { (100...599).contains($0) ? $0 : nil }
        self.byteCount = byteCount.flatMap { (0...1_073_741_824).contains($0) ? $0 : nil }
        self.elapsedMilliseconds = elapsedMilliseconds.flatMap {
            $0.isFinite && $0 >= 0 && $0 <= 300_000 ? Int($0.rounded()) : nil
        }
    }

    // Persisted preferences are revalidated rather than copied verbatim into export.
    fileprivate var validated: NativeDiagnosticEvent? {
        guard time.timeIntervalSince1970.isFinite, (0...4_102_444_800).contains(time.timeIntervalSince1970) else { return nil }
        return NativeDiagnosticEvent(time: time, operation: operation, step: step, target: target,
                                     httpStatus: httpStatus, envelopeStatus: envelopeStatus, error: error,
                                     byteCount: byteCount, elapsedMilliseconds: elapsedMilliseconds.map(Double.init))
    }
}

struct NativeDiagnosticSnapshot: Encodable {
    let schemaVersion: Int
    let application: String
    let appVersion: String
    let appBuild: String
    let generatedAt: Date
    let events: [NativeDiagnosticEvent]

    fileprivate init(events: [NativeDiagnosticEvent], version: String?, build: String?) {
        schemaVersion = 1; application = "XASS iOS"
        appVersion = Self.safeVersion(version); appBuild = Self.safeVersion(build)
        generatedAt = Date(); self.events = events
    }

    static func safeVersion(_ value: String?) -> String {
        guard let value = value, value.utf8.count <= 32,
              value.range(of: #"\A[0-9]{1,6}(\.[0-9]{1,6}){0,3}\z"#, options: .regularExpression) != nil else { return "unknown" }
        return value
    }
}

/// Bounded local diagnostics. Persistence and file export never run on the UI
/// thread, and one serialized writer preserves record/clear ordering.
final class NativeDiagnostics: @unchecked Sendable {
    static let shared = NativeDiagnostics()
    static let capacity = 200
    static let maxStoredBytes = 192 * 1024
    static let historyKey = "xass.native.diagnostics.history.v1"
    private static let enabledKey = "xass.native.diagnostics.enabled.v1"
    private let lock = NSLock()
    private let writer = DispatchQueue(label: "app.xass.native-diagnostics", qos: .utility)
    private let defaults: UserDefaults
    private let exportDirectory: URL
    private var events: [NativeDiagnosticEvent] = []
    private var recording: Bool
    private var generation = 0
    private var persistenceScheduled = false

    init(defaults: UserDefaults = .standard, exportDirectory: URL? = nil) {
        self.defaults = defaults
        self.exportDirectory = exportDirectory ?? FileManager.default.temporaryDirectory.appendingPathComponent("xass-diagnostics", isDirectory: true)
        recording = defaults.object(forKey: Self.enabledKey) as? Bool ?? true
        if let data = defaults.data(forKey: Self.historyKey), data.count <= Self.maxStoredBytes,
           let decoded = try? Self.decoder().decode([NativeDiagnosticEvent].self, from: data) {
            events = Array(decoded.compactMap(\.validated).suffix(Self.capacity))
        }
    }

    var isEnabled: Bool { lock.lock(); defer { lock.unlock() }; return recording }

    func setEnabled(_ enabled: Bool) {
        lock.lock(); defer { lock.unlock() }
        recording = enabled
        writer.async { [self] in defaults.set(enabled, forKey: Self.enabledKey) }
    }

    func record(operation: NativeDiagnosticOperation, step: NativeDiagnosticStep,
                target: NativeDiagnosticTarget = .server, httpStatus: Int? = nil, envelopeStatus: Int? = nil,
                error: NativeDiagnosticError = .none, byteCount: Int? = nil, elapsedMilliseconds: Double? = nil) {
        lock.lock(); defer { lock.unlock() }
        guard recording else { return }
        events.append(NativeDiagnosticEvent(time: Date(), operation: operation, step: step, target: target,
                                           httpStatus: httpStatus, envelopeStatus: envelopeStatus, error: error,
                                           byteCount: byteCount, elapsedMilliseconds: elapsedMilliseconds))
        if events.count > Self.capacity { events.removeFirst(events.count - Self.capacity) }
        schedulePersistenceLocked()
    }

    func snapshot() -> NativeDiagnosticSnapshot {
        lock.lock(); defer { lock.unlock() }
        return snapshotLocked()
    }

    private func snapshotLocked() -> NativeDiagnosticSnapshot {
        NativeDiagnosticSnapshot(events: events, version: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
                                 build: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String)
    }

    func clear() {
        lock.lock(); defer { lock.unlock() }
        generation += 1; events.removeAll()
        schedulePersistenceLocked()
        writer.async { [self] in
            // Only our single generated file; never remove an enclosing directory.
            try? FileManager.default.removeItem(at: exportDirectory.appendingPathComponent("xass-diagnostics.json"))
        }
    }

    func exportFile() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock(); defer { lock.unlock() }
            let captured = snapshotLocked(), capturedGeneration = generation
            writer.async { [self] in
                do {
                    lock.lock(); let current = generation == capturedGeneration; lock.unlock()
                    guard current else { throw CancellationError() }
                    let data = try Self.encoder().encode(captured)
                    guard data.count <= Self.maxStoredBytes else { throw CocoaError(.fileWriteOutOfSpace) }
                    try FileManager.default.createDirectory(at: exportDirectory, withIntermediateDirectories: true)
                    let url = exportDirectory.appendingPathComponent("xass-diagnostics.json")
                    try data.write(to: url, options: [.atomic, .completeFileProtection])
                    var protected = url
                    var resources = URLResourceValues(); resources.isExcludedFromBackup = true
                    try protected.setResourceValues(resources)
                    lock.lock(); let valid = generation == capturedGeneration; lock.unlock()
                    guard valid else { try? FileManager.default.removeItem(at: url); throw CancellationError() }
                    continuation.resume(returning: url)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    // Used by deterministic persistence tests, never by views or request handling.
    func flushPersistence() { writer.sync {} }

    private func schedulePersistenceLocked() {
        // A burst of requests keeps one pending snapshot, not hundreds of copies
        // of the entire ring buffer waiting to be written.
        guard !persistenceScheduled else { return }
        persistenceScheduled = true
        writer.async { [self] in
            lock.lock(); let saved = events; persistenceScheduled = false; lock.unlock()
            if saved.isEmpty { defaults.removeObject(forKey: Self.historyKey); return }
            guard let data = try? Self.encoder().encode(saved), data.count <= Self.maxStoredBytes else { return }
            defaults.set(data, forKey: Self.historyKey)
        }
    }

    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]; return encoder
    }
    private static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601; return decoder
    }
}
