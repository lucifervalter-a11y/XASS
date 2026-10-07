import AVFoundation
import Speech
import Foundation
import Combine

struct NativeTranscriptWord: Equatable {
    let text: String
    let time: Double
    let duration: Double
}

struct NativeTranscriptClip: Equatable {
    let start: Double
    let duration: Double
}

enum NativeTranscriptMode: Equatable {
    case onDevice, appleNetwork

    var privacyNotice: String {
        switch self {
        case .onDevice: return "Распознавание выполнялось на iPhone. Аудио не отправлялось в облако."
        case .appleNetwork: return "Вы разрешили распознавание Apple через интернет. Аудио могло отправляться в Apple."
        }
    }

    var failureMessage: String {
        switch self {
        case .onDevice:
            return "Распознавание на iPhone не завершилось. Проверьте доступность локальной модели языка и повторите. " + privacyNotice
        case .appleNetwork:
            return "Распознавание Apple через интернет не завершилось. Проверьте подключение и повторите позже. " + privacyNotice
        }
    }
}

struct NativeTranscriptResult: Equatable {
    let lrc: String
    let failedClips: [NativeTranscriptClip]
    let totalClips: Int
    let mode: NativeTranscriptMode
    var isPartial: Bool { !failedClips.isEmpty }

    var reviewNotice: String {
        guard isPartial else { return "Автоматическая расшифровка · проверьте слова. " + mode.privacyNotice }
        func stamp(_ seconds: Double) -> String {
            let value = Int(seconds.rounded(.up))
            return String(format: "%02d:%02d", value / 60, value % 60)
        }
        let gaps = failedClips.map { stamp($0.start) + "–" + stamp($0.start + $0.duration) }.joined(separator: ", ")
        return "Частичная расшифровка: не распознано \(failedClips.count) из \(totalClips) фрагментов. Пропуски: \(gaps). Проверьте и дополните текст перед сохранением. " + mode.privacyNotice
    }
}

/// Recognition failures may leave useful words, but must remain visible in the
/// review result. Cancellation and size limits still abort the whole operation.
struct NativeTranscriptAccumulator {
    let totalClips: Int
    let mode: NativeTranscriptMode
    private var words: [NativeTranscriptWord] = []
    private var failedClips: [NativeTranscriptClip] = []
    private var processedClips = 0

    init(totalClips: Int, mode: NativeTranscriptMode) {
        self.totalClips = totalClips
        self.mode = mode
    }

    mutating func append(_ result: Result<[NativeTranscriptWord], Error>, for clip: NativeTranscriptClip) throws {
        switch result {
        case .success(let chunk):
            let bounded = chunk.filter { $0.time >= 0 && $0.time < clip.duration }
            guard words.count + bounded.count <= 8000 else {
                throw OwnerAPIError(status: 422, message: "Результат распознавания слишком большой. Текст не был сохранён.")
            }
            words.append(contentsOf: bounded.map {
                NativeTranscriptWord(text: $0.text, time: clip.start + $0.time, duration: $0.duration)
            })
        case .failure(let error):
            if error is CancellationError { throw error }
            failedClips.append(clip)
        }
        processedClips += 1
    }

    func result() throws -> NativeTranscriptResult {
        guard processedClips == totalClips, totalClips > 0 else { throw CancellationError() }
        if failedClips.count == totalClips {
            throw OwnerAPIError(status: 422, message: mode.failureMessage)
        }
        let value = NativeTranscriptFormat.lrc(words)
        guard !value.isEmpty, value.utf8.count <= NativeTranscriptPolicy.maximumBytes else {
            throw OwnerAPIError(status: 422, message: "Не удалось уверенно распознать слова. Инструментальная музыка и пение распознаются не всегда. " + mode.privacyNotice)
        }
        return NativeTranscriptResult(lrc: value, failedClips: failedClips, totalClips: totalClips, mode: mode)
    }
}

enum NativeTranscriptPolicy {
    static let maximumDuration: Double = 900
    static let clipDuration: Double = 45
    static let maximumBytes = 64_000

    static func clips(duration: Double) -> [NativeTranscriptClip]? {
        guard duration.isFinite, duration > 0, duration <= maximumDuration else { return nil }
        return (0..<Int(ceil(duration / clipDuration))).map { index in
            let start = Double(index) * clipDuration
            return NativeTranscriptClip(start: start, duration: min(clipDuration, duration - start))
        }
    }

    static func localFile(_ file: URL) -> Bool {
        guard file.isFileURL, file.host?.isEmpty ?? true,
              let values = try? file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]),
              values.isRegularFile == true, values.isSymbolicLink != true else { return false }
        return true
    }

    static func recognitionRequest(file: URL, onDevice: Bool = true) -> SFSpeechURLRecognitionRequest {
        let request = SFSpeechURLRecognitionRequest(url: file)
        request.requiresOnDeviceRecognition = onDevice
        request.shouldReportPartialResults = false
        request.taskHint = .dictation
        return request
    }
}

enum NativeTranscriptFormat {
    static func lrc(_ words: [NativeTranscriptWord]) -> String {
        guard words.count <= 8000 else { return "" }
        var lines: [String] = [], group: [NativeTranscriptWord] = []
        var byteCount = 0, oversized = false, vocalEnd: Double = 0
        func append(centiseconds: Int, text: String) {
            let stamp = String(format: "[%02d:%02d.%02d]", centiseconds / 6000, centiseconds / 100 % 60, centiseconds % 100)
            let line = text.isEmpty ? stamp : stamp + " " + text
            byteCount += line.utf8.count + (lines.isEmpty ? 0 : 1)
            if byteCount <= NativeTranscriptPolicy.maximumBytes { lines.append(line) }
            else { oversized = true }
        }
        func flush(pauseAt: Double? = nil) {
            guard let first = group.first else { return }
            let centiseconds = Int((first.time * 100).rounded())
            append(centiseconds: centiseconds, text: group.map(\.text).joined(separator: " "))
            if let end = pauseAt {
                // Empty LRC rows stop highlighting at the actual last word's
                // end, not at the next phrase or throughout an instrumental.
                append(centiseconds: max(centiseconds + 1, Int((end * 100).rounded())), text: "")
            }
            group = []
        }
        let safeWords = words.prefix(8000).enumerated().compactMap { index, word -> (Int, NativeTranscriptWord)? in
            guard word.time.isFinite, word.duration.isFinite, word.time >= 0, word.duration >= 0,
                  word.time < NativeTranscriptPolicy.maximumDuration, word.duration <= NativeTranscriptPolicy.clipDuration,
                  word.text.utf8.count <= 2048 else { return nil }
            let separators = CharacterSet.whitespacesAndNewlines.union(.controlCharacters)
            let text = word.text.components(separatedBy: separators).filter { !$0.isEmpty }.joined(separator: " ")
                .replacingOccurrences(of: "[", with: "(").replacingOccurrences(of: "]", with: ")")
            guard !text.isEmpty else { return nil }
            return (index, NativeTranscriptWord(text: text, time: word.time, duration: word.duration))
        }.sorted { $0.1.time == $1.1.time ? $0.0 < $1.0 : $0.1.time < $1.1.time }
        for (_, word) in safeWords {
            if let first = group.first, let last = group.last,
               group.count >= 7 || word.time - first.time > 4 || word.time - last.time - last.duration > 1 {
                flush(pauseAt: word.time - vocalEnd > 1 ? vocalEnd : nil)
            }
            if oversized { return "" } // Never silently save a truncated transcript.
            group.append(word)
            vocalEnd = max(vocalEnd, min(NativeTranscriptPolicy.maximumDuration, word.time + word.duration))
        }
        flush(pauseAt: vocalEnd)
        return oversized ? "" : lines.joined(separator: "\n")
    }
}

/// Each callback belongs to one exact request. A late result/cancel/timeout
/// from a previous chunk must never resume the next chunk's continuation.
@MainActor final class NativeTranscriptCompletion<Value> {
    private var pending: (UUID, CheckedContinuation<Value, Error>)?
    var activeToken: UUID? { pending?.0 }

    @discardableResult func install(_ continuation: CheckedContinuation<Value, Error>, token: UUID) -> Bool {
        guard pending == nil else { continuation.resume(throwing: CancellationError()); return false }
        pending = (token, continuation)
        return true
    }

    @discardableResult func resolve(_ result: Result<Value, Error>, token: UUID, cleanup: () -> Void = {}) -> Bool {
        guard let current = pending, current.0 == token else { return false }
        pending = nil
        cleanup()
        current.1.resume(with: result)
        return true
    }
}

/// cancelExport called before export starts has no effect. Serialize the two
/// calls so cancellation either prevents start or cancels an already-started job.
final class NativeTranscriptExportGate: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var started = false
    func start(_ operation: () -> Void) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled, !started else { return false }
        started = true; operation(); return true
    }
    func cancel(_ operation: () -> Void) {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled else { return }
        cancelled = true
        if started { operation() }
    }
}

/// No microphone or automatic cloud fallback. Apple network recognition requires
/// explicit opt-in when no local model is available; downloaded audio uses short clips.
@MainActor final class NativeTranscription: ObservableObject {
    @Published private(set) var progress: Double = 0
    @Published private(set) var running = false
    private var recognition: SFSpeechRecognitionTask?
    private let completion = NativeTranscriptCompletion<[NativeTranscriptWord]>()
    private var timeout: Task<Void, Never>?

    /// `allowNetwork`: the owner explicitly allowed Apple's server recognition
    /// when this iPhone has no on-device model for the language.
    func transcribe(file: URL, language: String, allowNetwork: Bool = false) async throws -> NativeTranscriptResult {
        guard !running, NativeTranscriptPolicy.localFile(file) else { throw XASSErr.invalidMedia }
        try Task.checkCancellation()
        running = true; progress = 0
        defer { running = false }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: language)) else {
            throw OwnerAPIError(status: 422, message: "iOS не поддерживает распознавание выбранного языка.")
        }
        let onDevice = recognizer.supportsOnDeviceRecognition
        guard onDevice || allowNetwork else {
            throw OwnerAPIError(status: 422, message: "На этом iPhone нет локальной модели распознавания выбранного языка. Включите «Разрешить распознавание Apple через интернет» или скачайте язык диктовки в настройках iOS. Аудио не отправлялось.")
        }
        let mode: NativeTranscriptMode = onDevice ? .onDevice : .appleNetwork
        guard recognizer.isAvailable else {
            throw OwnerAPIError(status: 503, message: onDevice
                ? "Распознавание на iPhone сейчас недоступно. Проверьте локальную модель языка или повторите позже."
                : "Распознавание Apple через интернет сейчас недоступно. Проверьте сеть или повторите позже.")
        }
        let permission = try await authorization()
        try Task.checkCancellation()
        guard permission == .authorized else { throw OwnerAPIError(status: 403, message: "Разрешите распознавание речи для XASS в настройках iPhone.") }
        let asset = AVURLAsset(url: file)
        let length = try await asset.load(.duration).seconds
        try Task.checkCancellation()
        guard let clips = NativeTranscriptPolicy.clips(duration: length) else {
            throw OwnerAPIError(status: 422, message: "Расшифровка доступна для треков длительностью до 15 минут.")
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("xass-transcript-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false, attributes: [.protectionKey: FileProtectionType.complete])
        defer { try? FileManager.default.removeItem(at: folder) } // Only this operation's UUID temporary directory.
        var transcript = NativeTranscriptAccumulator(totalClips: clips.count, mode: mode)
        for segment in clips {
            try Task.checkCancellation()
            guard let exporter = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetAppleM4A) else { throw XASSErr.invalidMedia }
            let clip = folder.appendingPathComponent("segment-\(Int(segment.start)).m4a")
            exporter.outputURL = clip; exporter.outputFileType = .m4a
            exporter.timeRange = CMTimeRange(start: CMTime(seconds: segment.start, preferredTimescale: 600), duration: CMTime(seconds: segment.duration, preferredTimescale: 600))
            let exportGate = NativeTranscriptExportGate()
            try await withTaskCancellationHandler(operation: {
                try await withCheckedThrowingContinuation { (done: CheckedContinuation<Void, Error>) in
                    guard exportGate.start({ exporter.exportAsynchronously { done.resume() } }) else {
                        done.resume(throwing: CancellationError()); return
                    }
                }
            }, onCancel: { exportGate.cancel { exporter.cancelExport() } })
            try Task.checkCancellation()
            guard exporter.status == .completed else { throw OwnerAPIError(status: 422, message: "Не удалось прочитать аудиофайл для расшифровки.") }
            // Speech can fail on an instrumental clip or a service error. Keep
            // useful text from other clips, with explicit gaps for review.
            let chunk: Result<[NativeTranscriptWord], Error>
            do { chunk = .success(try await recognize(clip, recognizer: recognizer, onDevice: onDevice)) }
            catch { chunk = .failure(error) }
            try transcript.append(chunk, for: segment)
            try? FileManager.default.removeItem(at: clip)
            progress = min(1, (segment.start + segment.duration) / length)
        }
        try Task.checkCancellation()
        return try transcript.result()
    }

    private func authorization() async throws -> SFSpeechRecognizerAuthorizationStatus {
        let gate = NativeTranscriptCompletion<SFSpeechRecognizerAuthorizationStatus>(), token = UUID()
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { done in
                if Task.isCancelled { done.resume(throwing: CancellationError()); return }
                guard gate.install(done, token: token) else { return }
                SFSpeechRecognizer.requestAuthorization { value in
                    Task { @MainActor in gate.resolve(.success(value), token: token) }
                }
            }
        }, onCancel: { Task { @MainActor in gate.resolve(.failure(CancellationError()), token: token) } })
    }

    private func recognize(_ file: URL, recognizer: SFSpeechRecognizer, onDevice: Bool) async throws -> [NativeTranscriptWord] {
        try Task.checkCancellation()
        let token = UUID()
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { done in
                if Task.isCancelled { done.resume(throwing: CancellationError()); return }
                guard completion.install(done, token: token) else { return }
                let request = NativeTranscriptPolicy.recognitionRequest(file: file, onDevice: onDevice)
                recognition = recognizer.recognitionTask(with: request) { [weak self] result, error in
                    Task { @MainActor in
                        if let result = result, result.isFinal {
                            let words = result.bestTranscription.segments.map { NativeTranscriptWord(text: $0.substring, time: $0.timestamp, duration: $0.duration) }
                            self?.finish(.success(words), token: token)
                        } else if error != nil {
                            let mode: NativeTranscriptMode = onDevice ? .onDevice : .appleNetwork
                            self?.finish(.failure(OwnerAPIError(status: 422, message: mode.failureMessage)), token: token)
                        }
                    }
                }
                timeout = Task { [weak self] in
                    do { try await Task.sleep(nanoseconds: 60_000_000_000) } catch { return }
                    self?.finish(.failure(OwnerAPIError(status: 408, message: "Расшифровка фрагмента заняла слишком много времени. Попробуйте позже.")), token: token)
                }
            }
        }, onCancel: { Task { @MainActor [weak self] in self?.finish(.failure(CancellationError()), token: token) } })
    }

    private func finish(_ result: Result<[NativeTranscriptWord], Error>, token: UUID) {
        completion.resolve(result, token: token) {
            timeout?.cancel(); timeout = nil
            recognition?.cancel(); recognition = nil
        }
    }
}
