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

    static func recognitionRequest(file: URL) -> SFSpeechURLRecognitionRequest {
        let request = SFSpeechURLRecognitionRequest(url: file)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = false
        request.taskHint = .dictation
        return request
    }
}

enum NativeTranscriptFormat {
    static func lrc(_ words: [NativeTranscriptWord]) -> String {
        guard words.count <= 8000 else { return "" }
        var lines: [String] = [], group: [NativeTranscriptWord] = []
        var byteCount = 0, oversized = false
        func flush() {
            guard let first = group.first else { return }
            let centiseconds = Int((first.time * 100).rounded())
            let text = group.map(\.text).joined(separator: " ")
            let line = String(format: "[%02d:%02d.%02d] %@", centiseconds / 6000, centiseconds / 100 % 60, centiseconds % 100, text)
            byteCount += line.utf8.count + (lines.isEmpty ? 0 : 1)
            if byteCount <= NativeTranscriptPolicy.maximumBytes { lines.append(line) }
            else { oversized = true }
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
               group.count >= 7 || word.time - first.time > 4 || word.time - last.time - last.duration > 1 { flush() }
            if oversized { return "" } // Never silently save a truncated transcript.
            group.append(word)
        }
        flush()
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

/// No microphone and no cloud fallback. A downloaded file is processed in short,
/// private clips because the legacy Speech API is designed for short requests.
@MainActor final class NativeTranscription: ObservableObject {
    @Published private(set) var progress: Double = 0
    @Published private(set) var running = false
    private var recognition: SFSpeechRecognitionTask?
    private let completion = NativeTranscriptCompletion<[NativeTranscriptWord]>()
    private var timeout: Task<Void, Never>?

    func transcribe(file: URL, language: String) async throws -> String {
        guard !running, NativeTranscriptPolicy.localFile(file) else { throw XASSErr.invalidMedia }
        try Task.checkCancellation()
        running = true; progress = 0
        defer { running = false }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: language)), recognizer.supportsOnDeviceRecognition else {
            throw OwnerAPIError(status: 422, message: "На этом iPhone нет локального распознавания выбранного языка. Аудио не отправлялось в облако.")
        }
        let permission = try await authorization()
        try Task.checkCancellation()
        guard permission == .authorized else { throw OwnerAPIError(status: 403, message: "Разрешите распознавание речи для XASS в настройках iPhone.") }
        let asset = AVURLAsset(url: file)
        let length = try await asset.load(.duration).seconds
        try Task.checkCancellation()
        guard let clips = NativeTranscriptPolicy.clips(duration: length) else {
            throw OwnerAPIError(status: 422, message: "Локальная расшифровка доступна для треков длительностью до 15 минут.")
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("xass-transcript-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false, attributes: [.protectionKey: FileProtectionType.complete])
        defer { try? FileManager.default.removeItem(at: folder) } // Only this operation's UUID temporary directory.
        var words: [NativeTranscriptWord] = []
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
            let chunk = try await recognize(clip, recognizer: recognizer)
            let boundedChunk = chunk.filter { $0.time >= 0 && $0.time < segment.duration }
            guard words.count + boundedChunk.count <= 8000 else {
                throw OwnerAPIError(status: 422, message: "Результат распознавания слишком большой. Текст не был сохранён.")
            }
            words.append(contentsOf: boundedChunk.map {
                NativeTranscriptWord(text: $0.text, time: segment.start + $0.time, duration: $0.duration)
            })
            try? FileManager.default.removeItem(at: clip)
            progress = min(1, (segment.start + segment.duration) / length)
        }
        try Task.checkCancellation()
        let value = NativeTranscriptFormat.lrc(words)
        guard !value.isEmpty, value.utf8.count <= 64_000 else { throw OwnerAPIError(status: 422, message: "Не удалось уверенно распознать слова. Инструментальная музыка и пение распознаются не всегда.") }
        return value
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

    private func recognize(_ file: URL, recognizer: SFSpeechRecognizer) async throws -> [NativeTranscriptWord] {
        try Task.checkCancellation()
        let token = UUID()
        return try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { done in
                if Task.isCancelled { done.resume(throwing: CancellationError()); return }
                guard completion.install(done, token: token) else { return }
                let request = NativeTranscriptPolicy.recognitionRequest(file: file)
                recognition = recognizer.recognitionTask(with: request) { [weak self] result, error in
                    Task { @MainActor in
                        if let result = result, result.isFinal {
                            let words = result.bestTranscription.segments.map { NativeTranscriptWord(text: $0.substring, time: $0.timestamp, duration: $0.duration) }
                            self?.finish(.success(words), token: token)
                        } else if error != nil {
                            self?.finish(.failure(OwnerAPIError(status: 422, message: "Локальное распознавание не завершилось. Проверьте доступность языка и повторите. Аудио не отправлялось в облако.")), token: token)
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
