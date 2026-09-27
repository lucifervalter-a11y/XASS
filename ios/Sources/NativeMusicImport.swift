import Foundation
import CoreFoundation

enum NativeMusicImportPhase: String { case preparing, uploading, finishing }
enum NativeMusicImportOutcome: String { case imported, failed, cancelled, notAttempted, processing }
struct NativeMusicImportPending: LocalizedError {
    var errorDescription: String? { "Сервер ещё не подтвердил завершение. Обработка может продолжаться: обновите библиотеку перед повторным импортом. Отмена не удаляет уже добавленную музыку." }
}
/// Acquire provider access synchronously inside the picker callback, before its presentation ends.
final class NativeMusicImportSelection {
    private var scoped: [URL]
    private let release: (URL) -> Void
    init(_ urls: [URL], acquire: (URL) -> Bool = { $0.startAccessingSecurityScopedResource() }, release: @escaping (URL) -> Void = { $0.stopAccessingSecurityScopedResource() }) {
        self.release = release
        scoped = urls.filter(acquire)
    }
    func close() { let urls = scoped; scoped.removeAll(); urls.forEach(release) }
    deinit { close() }
}
struct NativeMusicImportProgress {
    let fileName: String
    let index: Int
    let total: Int
    let completed: Int
    let failed: Int
    let phase: NativeMusicImportPhase
    let fraction: Double
}
struct NativeMusicImportResult: Identifiable {
    let id = UUID()
    let fileName: String
    let outcome: NativeMusicImportOutcome
    let message: String
}

enum NativeMusicImportPolicy {
    static let chunkBytes = 512 * 1024
    static let maximumSelection = 1000
    static let maximumArchiveBytes = 2 * 1024 * 1024 * 1024
    static let extensions: Set<String> = ["mp3", "wav", "flac", "ogg", "m4a", "zip"]
    static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite, number.doubleValue.rounded(.towardZero) == number.doubleValue,
              number.doubleValue >= 0, number.doubleValue < Double(Int.max) else { return nil }
        return number.intValue
    }
    static func abortsBatch(_ error: Error) -> Bool {
        if error is CancellationError || error is URLError { return true }
        guard let api = error as? OwnerAPIError else { return false }
        return [0, 401, 403, 408, 429].contains(api.status) || api.status >= 500
    }
    static func message(_ error: Error) -> String {
        if error is CancellationError { return "Импорт отменён. Уже добавленная музыка сохранена." }
        if error is URLError { return "Связь с сервером прервалась. Уже добавленная музыка сохранена; повторите оставшиеся файлы." }
        if let value = error as? OwnerAPIError { return value.message }
        return "Не удалось прочитать файл. Скачайте его в «Файлы» на iPhone и выберите заново."
    }
}

private final class NativeMusicImportCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var coordinator: NSFileCoordinator?
    func cancel() {
        lock.lock(); cancelled = true; let active = coordinator; lock.unlock()
        active?.cancel()
    }
    func coordinate(using value: NSFileCoordinator) throws {
        lock.lock(); coordinator = value; let wasCancelled = cancelled; lock.unlock()
        if wasCancelled { throw CancellationError() }
    }
    func check() throws {
        lock.lock(); let value = cancelled; lock.unlock()
        if value { throw CancellationError() }
    }
}

/// One bounded private staging file. File-provider access and disk IO never run on MainActor.
/// Coordination covers the entire copy, not merely opening a handle that outlives its accessor.
final class NativeMusicImportFile: @unchecked Sendable {
    private static let worker = DispatchQueue(label: "app.xass.music-import-files", qos: .utility)
    let name: String
    let size: Int
    let isArchive: Bool
    let file: URL
    private let directory: URL
    private init(name: String, size: Int, file: URL, directory: URL, isArchive: Bool) {
        self.name = name; self.size = size; self.file = file; self.directory = directory; self.isArchive = isArchive
    }
    static func stage(_ url: URL, fileLimit: Int, archiveLimit: Int) async throws -> NativeMusicImportFile {
        let cancellation = NativeMusicImportCancellation()
        return try await withTaskCancellationHandler(operation: {
            try Task.checkCancellation()
            let staged: NativeMusicImportFile = try await withCheckedThrowingContinuation { continuation in
                worker.async {
                    do { continuation.resume(returning: try stageCoordinated(url, fileLimit: fileLimit, archiveLimit: archiveLimit, cancellation: cancellation)) }
                    catch { continuation.resume(throwing: error) }
                }
            }
            if Task.isCancelled { await staged.close(); throw CancellationError() }
            return staged
        }, onCancel: { cancellation.cancel() })
    }
    private static func stageCoordinated(_ url: URL, fileLimit: Int, archiveLimit: Int, cancellation: NativeMusicImportCancellation) throws -> NativeMusicImportFile {
        try cancellation.check()
        guard url.isFileURL, url.host == nil || url.host == "", !url.lastPathComponent.isEmpty,
              extensionsAllowed(url) else { throw OwnerAPIError(status: 400, message: "Выберите MP3, M4A, WAV, FLAC, OGG или ZIP из приложения «Файлы».") }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let archive = url.pathExtension.lowercased() == "zip"
        let limit = archive ? archiveLimit : fileLimit
        guard limit > 0, limit <= NativeMusicImportPolicy.maximumArchiveBytes else { throw OwnerAPIError.invalidResponse }
        let coordinator = NSFileCoordinator(filePresenter: nil)
        try cancellation.coordinate(using: coordinator)
        var coordinationError: NSError?
        var result: Result<NativeMusicImportFile, Error>?
        coordinator.coordinate(readingItemAt: url, options: .withoutChanges, error: &coordinationError) { actualURL in
            result = Result {
                try cancellation.check()
                let values = try actualURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
                let original = try url.resourceValues(forKeys: [.isSymbolicLinkKey])
                guard values.isRegularFile == true, values.isSymbolicLink != true, original.isSymbolicLink != true else {
                    throw OwnerAPIError(status: 400, message: "Нужен обычный аудиофайл или ZIP, а не папка или ссылка.")
                }
                let size = values.fileSize ?? 0
                guard size > 0, size <= limit else {
                    let label = archive ? "ZIP" : "Аудиофайл"
                    throw OwnerAPIError(status: 413, message: "\(label) пустой или больше лимита \(ByteCountFormatter.string(fromByteCount: Int64(limit), countStyle: .file)).")
                }
                let manager = FileManager.default
                let temporary = manager.temporaryDirectory
                if let free = try? temporary.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage,
                   free < Int64(size) + 32 * 1024 * 1024 {
                    throw OwnerAPIError(status: 507, message: "На iPhone недостаточно места для подготовки этого файла.")
                }
                let directory = temporary.appendingPathComponent("XASSMusicImport-" + UUID().uuidString, isDirectory: true)
                try manager.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
                let destination = directory.appendingPathComponent("input." + url.pathExtension.lowercased())
                do {
                    guard manager.createFile(atPath: destination.path, contents: nil, attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]) else { throw CocoaError(.fileWriteUnknown) }
                    let input = try FileHandle(forReadingFrom: actualURL); defer { try? input.close() }
                    let output = try FileHandle(forWritingTo: destination); defer { try? output.close() }
                    var copied = 0
                    while true {
                        try cancellation.check()
                        let chunk = try input.read(upToCount: NativeMusicImportPolicy.chunkBytes) ?? Data()
                        if chunk.isEmpty { break }
                        guard chunk.count <= size - copied else { throw OwnerAPIError(status: 400, message: "Файл изменился во время подготовки. Выберите его заново.") }
                        try output.write(contentsOf: chunk); copied += chunk.count
                    }
                    guard copied == size else { throw OwnerAPIError(status: 400, message: "Файл загружен из хранилища не полностью. Скачайте его на iPhone и повторите.") }
                    try cancellation.check()
                    return NativeMusicImportFile(name: url.lastPathComponent, size: size, file: destination, directory: directory, isArchive: archive)
                } catch {
                    try? manager.removeItem(at: destination); try? manager.removeItem(at: directory)
                    throw error
                }
            }
        }
        if let error = coordinationError { try cancellation.check(); throw error }
        guard let result = result else { throw CocoaError(.fileReadUnknown) }
        return try result.get()
    }
    private static func extensionsAllowed(_ url: URL) -> Bool { NativeMusicImportPolicy.extensions.contains(url.pathExtension.lowercased()) }
    func read(offset: Int, count: Int) async throws -> Data {
        try Task.checkCancellation()
        guard offset >= 0, offset < size, count > 0, count <= NativeMusicImportPolicy.chunkBytes, count <= size - offset else { throw OwnerAPIError.invalidResponse }
        let bytes: Data = try await withCheckedThrowingContinuation { continuation in
            Self.worker.async {
                do {
                    let handle = try FileHandle(forReadingFrom: self.file); defer { try? handle.close() }
                    try handle.seek(toOffset: UInt64(offset))
                    let bytes = try handle.read(upToCount: count) ?? Data()
                    guard bytes.count == count else { throw CocoaError(.fileReadCorruptFile) }
                    continuation.resume(returning: bytes)
                } catch { continuation.resume(throwing: error) }
            }
        }
        try Task.checkCancellation()
        return bytes
    }
    func close() async {
        await withCheckedContinuation { continuation in
            Self.worker.async {
                // Only this helper's single private file and UUID-owned directory.
                try? FileManager.default.removeItem(at: self.file)
                try? FileManager.default.removeItem(at: self.directory)
                continuation.resume()
            }
        }
    }
}
