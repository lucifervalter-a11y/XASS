import Foundation
import UIKit

struct DownloadTicket {
    static let legacyLifetime: TimeInterval = 15 * 60
    static let maximumLifetime: TimeInterval = 24 * 3600
    let url: String
    let expiresAt: Date
    init(url: String, expiresIn: Any?, receivedAt: Date = Date()) throws {
        let lifetime: TimeInterval
        if let raw = expiresIn {
            guard let value = raw as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
                  value.doubleValue.isFinite, value.doubleValue > 0,
                  value.doubleValue <= Self.maximumLifetime else { throw XASSErr.invalidMedia }
            lifetime = value.doubleValue
        } else { lifetime = Self.legacyLifetime }
        self.url = url; expiresAt = receivedAt.addingTimeInterval(lifetime)
    }
}

/// Kept only in Keychain. Optional expiry decodes pre-upgrade records using
/// their original conservative 15-minute window, without changing resume data.
struct BackgroundDownloadSecret: Codable {
    let url: String
    let issuedAt: Date
    var resume: Data?
    var expiresAt: Date? = nil
    func canResume(at now: Date = Date()) -> Bool {
        let expiry = expiresAt ?? issuedAt.addingTimeInterval(DownloadTicket.legacyLifetime)
        let lifetime = expiry.timeIntervalSince(issuedAt)
        return lifetime.isFinite && lifetime > 0 && lifetime <= DownloadTicket.maximumLifetime
            && now >= issuedAt && now < expiry
    }
}

enum DownloadPhase: String, Codable {
    case queued, downloading, paused, failed, committing
}

struct BackgroundDownload: Codable, Identifiable {
    let id: UUID
    let origin: String
    var track: DownloadedTrack
    var phase: DownloadPhase = .queued
    var received: Int64 = 0
    var expected: Int64 = 0
    var message: String? = nil
    var taskID: Int? = nil
    var restartAttempts: Int = 0
    var usedResumeData: Bool = false
    var discardResumeData: Bool = false
    var needsFreshTicket: Bool = false

    private enum CodingKeys: String, CodingKey {
        case id, origin, track, phase, received, expected, message, taskID
        case restartAttempts, usedResumeData, discardResumeData, needsFreshTicket
    }

    var progress: Double? {
        expected > 0 ? min(1, max(0, Double(received) / Double(expected))) : nil
    }
    var active: Bool { phase == .queued || phase == .downloading || phase == .committing }
}

extension BackgroundDownload {
    // Stored defaults alone do not migrate synthesized Codable records. Old
    // tasks are conservatively treated as not having used resume data.
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        origin = try values.decode(String.self, forKey: .origin)
        track = try values.decode(DownloadedTrack.self, forKey: .track)
        phase = try values.decode(DownloadPhase.self, forKey: .phase)
        received = try values.decode(Int64.self, forKey: .received)
        expected = try values.decode(Int64.self, forKey: .expected)
        message = try values.decodeIfPresent(String.self, forKey: .message)
        taskID = try values.decodeIfPresent(Int.self, forKey: .taskID)
        restartAttempts = try values.decodeIfPresent(Int.self, forKey: .restartAttempts) ?? 0
        usedResumeData = try values.decodeIfPresent(Bool.self, forKey: .usedResumeData) ?? false
        discardResumeData = try values.decodeIfPresent(Bool.self, forKey: .discardResumeData) ?? false
        needsFreshTicket = try values.decodeIfPresent(Bool.self, forKey: .needsFreshTicket) ?? false
    }
}

/// Injectable credential storage keeps recovery tests off the real Keychain.
/// Production continues to use ThisDeviceOnly SecureStore entries.
struct DownloadSecretAccess {
    var load: (UUID) -> BackgroundDownloadSecret?
    var save: (BackgroundDownloadSecret, UUID) throws -> Void
    var remove: (UUID) -> Void
}

/// A cancellation callback belongs to one pause, not merely to a track ID.
/// Pause -> resume/new ticket -> pause must never attach the first task's
/// opaque URLSession resume blob to the second ticket.
struct DownloadPauseEpochs {
    private struct Pending { let epoch: UUID; var wantsResume = false }
    private var values: [UUID: Pending] = [:]
    mutating func begin(_ id: UUID) -> UUID {
        let epoch = UUID(); values[id] = Pending(epoch: epoch); return epoch
    }
    mutating func invalidate(_ id: UUID) { values.removeValue(forKey: id) }
    func accepts(_ id: UUID, epoch: UUID) -> Bool { values[id]?.epoch == epoch }
    func isPending(_ id: UUID) -> Bool { values[id] != nil }
    mutating func requestResume(_ id: UUID) -> Bool {
        guard var pending = values[id] else { return false }
        pending.wantsResume = true; values[id] = pending; return true
    }
    mutating func keepPaused(_ id: UUID) {
        guard var pending = values[id] else { return }
        pending.wantsResume = false; values[id] = pending
    }
    /// Consume the matching cancellation even if its secret/job disappeared.
    /// nil is a stale callback; false is a completed pause without resume intent.
    mutating func finish(_ id: UUID, epoch: UUID) -> Bool? {
        guard let pending = values[id], pending.epoch == epoch else { return nil }
        values.removeValue(forKey: id); return pending.wantsResume
    }
}

/// The durable index contains metadata only. Download URLs and opaque resume
/// blobs can contain tickets, so they live in ThisDeviceOnly Keychain entries.
final class DownloadLedger {
    static let maxJobs = 100
    let directory: URL
    private(set) var entries: [BackgroundDownload] = []
    init(root: URL? = nil) throws {
        let base = try root ?? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        directory = base.appendingPathComponent("XASSDownloadQueue", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var folder = directory; var values = URLResourceValues(); values.isExcludedFromBackup = true
        try folder.setResourceValues(values)
        let index = directory.appendingPathComponent("queue.json")
        if FileManager.default.fileExists(atPath: index.path) {
            guard let size = try index.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 1024 * 1024 else { throw XASSErr.invalidMedia }
            let saved = try JSONDecoder().decode([BackgroundDownload].self, from: Data(contentsOf: index))
            guard saved.count <= Self.maxJobs else { throw XASSErr.invalidMedia }
            var ids = Set<UUID>(), tracks = Set<String>()
            entries = saved.filter { item in
                guard let origin = try? ServerOrigin(item.origin), origin.url.absoluteString == item.origin,
                      item.track.id > 0, item.track.id <= Int(Int32.max),
                      item.track.title.count <= 300, item.track.artist.count <= 300,
                      item.track.duration.isFinite, item.track.duration >= 0, item.track.duration <= 86400,
                      item.received >= 0, item.expected >= 0,
                      (0...1).contains(item.restartAttempts),
                      item.received <= PrivateDownload.maxBytes, item.expected <= PrivateDownload.maxBytes,
                      ids.insert(item.id).inserted,
                      tracks.insert(origin.namespace + ":" + String(item.track.id)).inserted else { return false }
                return true
            }
        }
    }
    func save(_ values: [BackgroundDownload]) throws {
        guard values.count <= Self.maxJobs else { throw XASSErr.invalidCommand }
        let data = try JSONEncoder().encode(values)
        guard data.count <= 1024 * 1024 else { throw XASSErr.invalidCommand }
        try data.write(to: directory.appendingPathComponent("queue.json"), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        entries = values
    }
    /// Fail closed in this process if storage itself is unavailable. A later
    /// successful queue write persists this rejection with the other entries.
    func rememberFailure(_ item: BackgroundDownload) {
        guard item.phase == .failed, let index = entries.firstIndex(where: { $0.id == item.id }) else { return }
        entries[index] = item
    }
    func stagedFile(_ item: BackgroundDownload) -> URL {
        let suffix = Self.extensions.contains(item.track.fileExtension ?? "") ? item.track.fileExtension! : "audio"
        return directory.appendingPathComponent(item.id.uuidString + "." + suffix)
    }
    /// Both folders belong to Application Support on the same local volume.
    /// Link immutable downloaded bytes instead of copying up to 256 MB on the
    /// UI/delegate queue. The journal retains its link until index commit; a
    /// process interruption at any point is consequently replayable.
    func prepareCommit(_ item: BackgroundDownload, in destination: URL) throws -> URL {
        let staged = stagedFile(item)
        let temporary = destination.appendingPathComponent(item.id.uuidString + ".pending")
        if FileManager.default.fileExists(atPath: temporary.path) { try FileManager.default.removeItem(at: temporary) }
        try FileManager.default.linkItem(at: staged, to: temporary)
        return temporary
    }
    func canReplayCommit(_ item: BackgroundDownload) -> Bool {
        guard [.committing, .failed].contains(item.phase),
              Self.extensions.contains(item.track.fileExtension ?? ""),
              let size = try? stagedFile(item).resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size > 0, Int64(size) <= PrivateDownload.maxBytes else { return false }
        return true
    }
    static let extensions: Set<String> = ["mp3", "m4a", "wav", "flac", "ogg", "audio"]
    static let mimeExtensions = ["audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac", "audio/x-flac": "flac",
        "audio/ogg": "ogg", "application/ogg": "ogg"]
}

final class BackgroundDownloads: NSObject, URLSessionDownloadDelegate {
    static let identifier = "app.xass.mobile.music-downloads.v1"
    static let changed = Notification.Name("XASSBackgroundDownloadsChanged")
    static let shared = BackgroundDownloads()
    private var ledger: DownloadLedger?
    private var running: [UUID: URLSessionDownloadTask] = [:]
    private var pauseEpochs = DownloadPauseEpochs()
    private var allowedOrigin: String?
    private var reconciled = false
    private var reconciling = false
    private var completionHandlers: [() -> Void] = []
    private var lastProgress = Date.distantPast
    private var secretAccess: DownloadSecretAccess?
    private var systemSchedulingEnabled = true
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.identifier)
        config.sessionSendsLaunchEvents = true; config.isDiscretionary = false
        config.waitsForConnectivity = true; config.httpMaximumConnectionsPerHost = 2
        config.httpShouldSetCookies = false; config.httpCookieStorage = nil
        config.urlCredentialStorage = nil; config.urlCache = nil
        config.timeoutIntervalForRequest = 60; config.timeoutIntervalForResource = 24 * 3600
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()
    private override init() {
        super.init()
        ledger = try? DownloadLedger()
    }
    init(ledger: DownloadLedger, origin: ServerOrigin, secrets: DownloadSecretAccess) {
        self.ledger = ledger; self.allowedOrigin = origin.url.absoluteString; self.secretAccess = secrets
        self.systemSchedulingEnabled = false
        super.init()
    }
    func start() {
        guard systemSchedulingEnabled, !reconciled, !reconciling else { return }
        if ledger == nil { ledger = try? DownloadLedger() }
        // Protected storage can be unavailable before the first device unlock.
        // Do not destroy system-owned tasks just because that index is locked.
        guard ledger != nil else { return }
        reconciling = true
        session.getAllTasks { tasks in
            DispatchQueue.main.async {
                self.reconciling = false
                guard let ledger = self.ledger else { return }
                let entries = ledger.entries
                for task in tasks {
                    guard let download = task as? URLSessionDownloadTask,
                          let raw = task.taskDescription, let id = UUID(uuidString: raw),
                          let item = entries.first(where: { $0.id == id }), item.phase == .downloading,
                          item.taskID == task.taskIdentifier,
                          let origin = try? ServerOrigin(item.origin), let url = task.originalRequest?.url,
                          (try? origin.mediaURL(url.absoluteString, trackID: item.track.id)) != nil else { task.cancel(); continue }
                    self.running[id] = download
                }
                for var item in entries {
                    if item.phase == .committing { self.commit(item); continue }
                    if item.phase == .downloading && self.running[item.id] == nil {
                        item.phase = .paused; item.taskID = nil; item.message = "Загрузка прервана. Нажмите «Продолжить»."
                        self.update(item)
                    }
                }
                self.reconciled = true; self.suspendOtherOrigins(); self.schedule(); self.notify()
            }
        }
    }
    func handleEvents(identifier: String, completion: @escaping () -> Void) {
        guard identifier == Self.identifier else { completion(); return }
        completionHandlers.append(completion); start()
    }
    func activate(_ origin: ServerOrigin) {
        allowedOrigin = origin.url.absoluteString; start()
        if reconciled { suspendOtherOrigins() }
        schedule()
    }
    private func suspendOtherOrigins() {
        guard let allowedOrigin else { return }
        for item in ledger?.entries ?? [] where item.origin != allowedOrigin && item.active {
            pause(item.id)
        }
    }
    func entries(for origin: ServerOrigin) -> [BackgroundDownload] { ledger?.entries.filter { $0.origin == origin.url.absoluteString } ?? [] }
    func enqueue(origin: ServerOrigin, track: DownloadedTrack, url: URL, expiresAt: Date? = nil) throws {
        guard let ledger, track.id > 0, track.id <= Int(Int32.max), track.title.count <= 300, track.artist.count <= 300,
              track.duration.isFinite, track.duration >= 0, track.duration <= 86400,
              (try? origin.mediaURL(url.absoluteString, trackID: track.id)) != nil else { throw XASSErr.invalidMedia }
        var item = ledger.entries.first { $0.origin == origin.url.absoluteString && $0.track.id == track.id }
            ?? BackgroundDownload(id: UUID(), origin: origin.url.absoluteString, track: track)
        guard running[item.id] == nil else { return }
        let previous = item
        let previousSecret = secret(item.id)
        item.track = track; item.phase = .queued; item.message = nil; item.taskID = nil
        item.received = 0; item.expected = 0
        item.restartAttempts = 0; item.usedResumeData = false
        item.discardResumeData = false; item.needsFreshTicket = false
        let secret = BackgroundDownloadSecret(url: url.absoluteString, issuedAt: Date(), resume: nil, expiresAt: expiresAt)
        guard secret.canResume() else { throw XASSErr.invalidMedia }
        try saveSecret(secret, id: item.id)
        var values = ledger.entries.filter { $0.id != item.id }; values.append(item)
        do { try ledger.save(values) } catch {
            if let previousSecret { try? saveSecret(previousSecret, id: item.id) }
            else { removeSecret(item.id) }
            throw error
        }
        pauseEpochs.invalidate(item.id)
        // This is a deliberately new transfer. Its predecessor's staged bytes
        // must not later be replayed as if they belonged to the new ticket.
        try? FileManager.default.removeItem(at: ledger.stagedFile(previous))
        allowedOrigin = origin.url.absoluteString; start(); schedule(); notify()
    }
    /// Resume data is reusable only with its original unexpired ticket. A new
    /// ticket starts a new transfer; URLSession resume blobs are never rewritten.
    func resume(_ id: UUID) -> Bool {
        guard let ledger, var item = ledger.entries.first(where: { $0.id == id }),
              item.origin == allowedOrigin else { return false }
        if ledger.canReplayCommit(item) {
            pauseEpochs.invalidate(id)
            item.phase = .committing; item.message = nil
            guard update(item) else { return false }
            commit(item); return true
        }
        guard !item.active, !item.needsFreshTicket else { return false }
        // URLSession has not supplied its opaque resume data yet. Record the
        // user's intent without launching a second download from byte zero.
        if pauseEpochs.requestResume(id) {
            item.message = "Сохраняю прогресс перед продолжением…"
            if !update(item) { pauseEpochs.keepPaused(id); return false }
            return true
        }
        guard let secret = secret(id), secret.canResume() else { return false }
        item.phase = .queued; item.message = nil; item.taskID = nil; item.usedResumeData = false
        guard update(item) else { return false }
        pauseEpochs.invalidate(id); schedule(); return true
    }
    func pause(_ id: UUID) {
        guard var item = ledger?.entries.first(where: { $0.id == id }), item.phase != .committing else { return }
        // A failed durable commit is already local work and must retain its
        // retry state across logout or origin switching.
        if ledger?.canReplayCommit(item) == true { return }
        item.phase = .paused; item.message = nil; item.taskID = nil
        guard update(item) else { return }
        if pauseEpochs.isPending(id) {
            // Repeated Pause cancels only queued resume intent, not the still
            // valid cancellation callback holding the original byte position.
            pauseEpochs.keepPaused(id); schedule(); return
        }
        if let task = running.removeValue(forKey: id) {
            let epoch = pauseEpochs.begin(id)
            task.cancel { data in
                DispatchQueue.main.async {
                    guard let wantsResume = self.pauseEpochs.finish(id, epoch: epoch) else { return }
                    defer { self.schedule() }
                    guard var current = self.ledger?.entries.first(where: { $0.id == id }), current.phase == .paused,
                          var secret = self.secret(id) else { return }
                    secret.resume = data.flatMap { $0.count <= 2 * 1024 * 1024 ? $0 : nil }
                    do { try self.saveSecret(secret, id: id) }
                    catch { self.fail(id, "Не удалось сохранить прогресс. Повторите загрузку."); return }
                    if wantsResume && current.origin == self.allowedOrigin {
                        if secret.canResume() { current.phase = .queued; current.message = nil }
                        else { current.phase = .failed; current.message = "Для продолжения нужна новая ссылка. Нажмите «Повторить»." }
                        self.update(current)
                    }
                }
            }
        }
        schedule()
    }
    func suspend(origin: ServerOrigin, forgetSecrets: Bool) {
        if allowedOrigin == origin.url.absoluteString { allowedOrigin = nil }
        for item in entries(for: origin) {
            pause(item.id)
            if forgetSecrets { pauseEpochs.invalidate(item.id); removeSecret(item.id) }
        }
    }
    func cancel(_ id: UUID) {
        guard let ledger, let item = ledger.entries.first(where: { $0.id == id }) else { return }
        do { try ledger.save(ledger.entries.filter { $0.id != id }) } catch { return }
        pauseEpochs.invalidate(id)
        running.removeValue(forKey: id)?.cancel(); removeSecret(id)
        try? FileManager.default.removeItem(at: ledger.stagedFile(item)); schedule(); notify()
    }
    private func schedule() {
        guard reconciled, let ledger, let allowedOrigin else { return }
        for var item in ledger.entries where item.phase == .queued && item.origin == allowedOrigin {
            guard running.count < 2 else { break }
            guard !item.needsFreshTicket, let origin = try? ServerOrigin(item.origin), var secret = secret(item.id), secret.canResume(),
                  let url = try? origin.mediaURL(secret.url, trackID: item.track.id) else {
                fail(item.id, "Для продолжения нужна новая ссылка. Нажмите «Повторить»."); continue
            }
            if item.discardResumeData {
                // The ledger consumed the one-shot budget before this Keychain
                // write. A crash between the two stores cannot reuse bad bytes.
                secret.resume = nil
                do { try saveSecret(secret, id: item.id) }
                catch { fail(item.id, "Не удалось сбросить данные продолжения. Повторите загрузку."); continue }
            }
            var pendingTask: URLSessionDownloadTask?
            if let resume = secret.resume {
                pendingTask = Self.makeResumedTask(session: session, data: resume)
                if pendingTask == nil {
                    // Malformed opaque data can throw an Objective-C exception
                    // before a task exists, so no delegate callback can help.
                    guard recoverRejectedResumeCreation(id: item.id) else {
                        fail(item.id, "Данные продолжения отклонены. Повторите загрузку."); continue
                    }
                    guard let recovered = ledger.entries.first(where: { $0.id == item.id }),
                          recovered.phase == .queued else { continue }
                    item = recovered; secret.resume = nil
                    do { try saveSecret(secret, id: item.id) }
                    catch { fail(item.id, "Не удалось сбросить данные продолжения. Повторите загрузку."); continue }
                }
            }
            if pendingTask == nil {
                var request = URLRequest(url: url)
                // No owner cookies, Authorization, API key or custom credential
                // headers are sent to the background networking daemon.
                request.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
                pendingTask = session.downloadTask(with: request)
            }
            guard let task = pendingTask else { continue }
            item.usedResumeData = secret.resume != nil; item.discardResumeData = false
            if !item.usedResumeData { item.received = 0; item.expected = 0 }
            item.phase = .downloading; item.taskID = task.taskIdentifier
            guard update(item) else {
                // Terminal in-memory state must precede cancellation: its
                // completion callback schedules the queue again.
                fail(item.id, "Не удалось сохранить состояние загрузки. Нажмите «Повторить».")
                task.cancel(); continue
            }
            task.taskDescription = item.id.uuidString; running[item.id] = task; task.resume()
        }
    }
    static func makeResumedTask(session: URLSession, data: Data) -> URLSessionDownloadTask? {
        XASSDownloadTaskWithResumeData(session, data)
    }
    private func key(_ id: UUID) -> String { "music-download-" + id.uuidString }
    private func secret(_ id: UUID) -> BackgroundDownloadSecret? {
        if let secretAccess { return secretAccess.load(id) }
        guard let data = SecureStore.load(key(id)), data.count <= 3 * 1024 * 1024 else { return nil }
        return try? JSONDecoder().decode(BackgroundDownloadSecret.self, from: data)
    }
    private func saveSecret(_ value: BackgroundDownloadSecret, id: UUID) throws {
        if let secretAccess { try secretAccess.save(value, id); return }
        try SecureStore.save(JSONEncoder().encode(value), name: key(id))
    }
    private func removeSecret(_ id: UUID) {
        if let secretAccess { secretAccess.remove(id); return }
        SecureStore.remove(key(id))
    }
    @discardableResult private func update(_ item: BackgroundDownload) -> Bool {
        guard let ledger, let index = ledger.entries.firstIndex(where: { $0.id == item.id }) else { return false }
        var values = ledger.entries; values[index] = item
        do { try ledger.save(values); notify(); return true } catch { return false }
    }
    private func notify() { NotificationCenter.default.post(name: Self.changed, object: nil) }
    private func fail(_ id: UUID, _ message: String) {
        guard var item = ledger?.entries.first(where: { $0.id == id }) else { return }
        item.phase = .failed; item.message = message
        if !update(item) { ledger?.rememberFailure(item); notify() }
    }
    private func item(_ task: URLSessionTask) -> BackgroundDownload? {
        guard let raw = task.taskDescription, let id = UUID(uuidString: raw),
              let item = ledger?.entries.first(where: { $0.id == id }),
              item.taskID == task.taskIdentifier else { return nil }
        return item
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard var item = item(downloadTask), item.phase == .downloading else { return }
        if totalBytesWritten > PrivateDownload.maxBytes || totalBytesExpectedToWrite > PrivateDownload.maxBytes {
            fail(item.id, "Трек превышает лимит 256 МБ."); running.removeValue(forKey: item.id)?.cancel(); schedule(); return
        }
        guard Date().timeIntervalSince(lastProgress) >= 0.5 else { return }
        lastProgress = Date(); item.message = nil; item.received = max(0, totalBytesWritten); item.expected = max(0, totalBytesExpectedToWrite); update(item)
    }
    func urlSession(_ session: URLSession, taskIsWaitingForConnectivity task: URLSessionTask) {
        guard var item = item(task), item.phase == .downloading else { return }
        item.message = "Ожидание сети. Загрузка продолжится после подключения."; update(item)
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard var item = item(downloadTask), item.phase == .downloading, let ledger else { return }
        if recoverFailure(id: item.id, taskID: downloadTask.taskIdentifier,
                          statusCode: (downloadTask.response as? HTTPURLResponse)?.statusCode, error: nil,
                          responseURL: downloadTask.response?.url) { return }
        guard let response = downloadTask.response as? HTTPURLResponse, [200, 206].contains(response.statusCode),
              let origin = try? ServerOrigin(item.origin), let url = response.url,
              (try? origin.mediaURL(url.absoluteString, trackID: item.track.id)) != nil,
              let suffix = DownloadLedger.mimeExtensions[response.mimeType ?? ""],
              let size = try? location.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size > 0, Int64(size) <= PrivateDownload.maxBytes,
              Self.completeResponse(response, bytes: Int64(size)) else {
            fail(item.id, "Сервер не вернул аудиофайл. Повторите загрузку со свежей ссылкой."); return
        }
        do {
            // Background URLSession follows redirects itself. Accept only a final
            // same-origin track response; never accept cross-origin output. Unlike
            // the foreground loader, this cannot prevent the redirect request.
            item.track.fileExtension = suffix; item.phase = .committing
            // Persist the response-approved suffix and commit intention before
            // moving the daemon-owned temporary file. A kill after the move
            // must leave a replayable record, never an orphaned .mp3/.m4a.
            guard update(item) else { throw XASSErr.invalidMedia }
            let staged = ledger.stagedFile(item)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: location.path)
            if FileManager.default.fileExists(atPath: staged.path) { _ = try FileManager.default.replaceItemAt(staged, withItemAt: location) }
            else { try FileManager.default.moveItem(at: location, to: staged) }
            commit(item)
        } catch { fail(item.id, "Не удалось сохранить файл. Проверьте свободное место.") }
    }
    static func completeResponse(_ response: HTTPURLResponse, bytes: Int64) -> Bool {
        if response.statusCode == 206 {
            guard let range = response.value(forHTTPHeaderField: "Content-Range"),
                  let parsed = SecureMediaLoader.parseRange(range) else { return false }
            return bytes == parsed.total
        }
        return response.statusCode == 200 && (response.expectedContentLength < 0 || response.expectedContentLength == bytes)
    }
    private func commit(_ item: BackgroundDownload) {
        guard let ledger else { return }
        do {
            let origin = try ServerOrigin(item.origin), library = try OfflineLibrary(origin: origin)
            let staged = ledger.stagedFile(item), target = library.file(for: item.track)
            guard let size = try staged.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 0, Int64(size) <= PrivateDownload.maxBytes else { throw XASSErr.invalidMedia }
            // Retain a journal-owned link until both media and index commit.
            let temporary = try ledger.prepareCommit(item, in: library.directory)
            if FileManager.default.fileExists(atPath: target.path) { _ = try FileManager.default.replaceItemAt(target, withItemAt: temporary) }
            else { try FileManager.default.moveItem(at: temporary, to: target) }
            var values = library.tracks().filter { $0.id != item.track.id }; values.append(item.track); try library.save(values)
            try ledger.save(ledger.entries.filter { $0.id != item.id })
            try? FileManager.default.removeItem(at: staged); removeSecret(item.id); notify()
        } catch { fail(item.id, "Файл получен, но сохранение не завершено. Повторите загрузку.") }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let item = item(task) else {
            // Successful commit may already have removed its ledger entry.
            if let raw = task.taskDescription, let id = UUID(uuidString: raw), running[id]?.taskIdentifier == task.taskIdentifier { running.removeValue(forKey: id) }
            schedule(); return
        }
        running.removeValue(forKey: item.id)
        if recoverFailure(id: item.id, taskID: task.taskIdentifier,
                          statusCode: (task.response as? HTTPURLResponse)?.statusCode, error: error as NSError?,
                          responseURL: task.response?.url,
                          missingResumeRequest: task.originalRequest?.url == nil && task.currentRequest?.url == nil) {
            schedule(); notify(); return
        }
        if let error = error as NSError?, item.phase == .downloading {
            if var secret = secret(item.id) {
                secret.resume = (error.userInfo[NSURLSessionDownloadTaskResumeData] as? Data).flatMap { $0.count <= 2 * 1024 * 1024 ? $0 : nil }
                try? saveSecret(secret, id: item.id)
            }
            fail(item.id, "Загрузка прервана. Можно продолжить или повторить её.")
        }
        schedule(); notify()
    }

    /// Shared by both delegate callbacks. Current task identity fences late
    /// callbacks after pause, cancel, logout, ticket refresh or replacement.
    /// This never starts networking: scheduling remains with the delegate.
    @discardableResult
    func recoverFailure(id: UUID, taskID: Int, statusCode: Int?, error: NSError?, responseURL: URL? = nil,
                        missingResumeRequest: Bool = false) -> Bool {
        guard var item = ledger?.entries.first(where: { $0.id == id }),
              item.phase == .downloading, item.taskID == taskID else { return false }
        if let responseURL {
            guard let origin = try? ServerOrigin(item.origin),
                  (try? origin.mediaURL(responseURL.absoluteString, trackID: item.track.id)) != nil else { return false }
        }
        if statusCode == 401 || statusCode == 403 {
            item.needsFreshTicket = true; item.discardResumeData = true
            item.phase = .failed; item.taskID = nil; item.usedResumeData = false
            item.message = "Ссылка на загрузку отклонена сервером. Нажмите «Повторить» для получения новой."
            // Even failed Keychain deletion cannot make resume() reuse a URL
            // rejected by the server: the durable ledger is authoritative.
            if !update(item) {
                ledger?.rememberFailure(item)
                if var rejected = secret(id) {
                    rejected.resume = nil; rejected.expiresAt = .distantPast
                    try? saveSecret(rejected, id: id)
                }
                notify()
            }
            pauseEpochs.invalidate(id); removeSecret(id)
            return true
        }
        guard item.origin == allowedOrigin, item.usedResumeData, item.restartAttempts < 1,
              !item.needsFreshTicket, let secret = secret(id), secret.canResume(),
              let origin = try? ServerOrigin(item.origin),
              (try? origin.mediaURL(secret.url, trackID: item.track.id)) != nil else { return false }
        let resumeFailure: Bool
        if statusCode == 416 {
            resumeFailure = error == nil || (error?.domain == NSURLErrorDomain && error?.code == URLError.badServerResponse.rawValue)
        } else if statusCode == nil || statusCode == 200 || statusCode == 206 {
            // These are candidates only because THIS task used resume data.
            // Network, cancellation, TLS, auth and disk-write errors are not.
            let codes: Set<Int> = [URLError.cannotDecodeRawData.rawValue, URLError.cannotDecodeContentData.rawValue,
                URLError.fileDoesNotExist.rawValue, URLError.downloadDecodingFailedMidStream.rawValue,
                URLError.downloadDecodingFailedToComplete.rawValue]
            // -1008 means resourceUnavailable, NOT cannotDecodeContentData.
            // With no HTTP response on a resumed task it can indicate that
            // the daemon cannot reopen resume state. Permit just one fresh GET.
            let unavailableResume = statusCode == nil && error?.code == URLError.resourceUnavailable.rawValue
                && error?.userInfo[NSUnderlyingErrorKey] == nil
            // A malformed dictionary can produce a task with no request at all.
            // Darwin reports unsupportedURL, including a matching CFNetwork
            // cause. This is not permission to retry an arbitrary bad URL: the
            // original HTTPS ticket was separately validated above.
            let underlying = error?.userInfo[NSUnderlyingErrorKey] as? NSError
            let unsupportedResume = missingResumeRequest && statusCode == nil
                && error?.code == URLError.unsupportedURL.rawValue
                && (underlying == nil || (underlying?.domain == "kCFErrorDomainCFNetwork"
                    && underlying?.code == URLError.unsupportedURL.rawValue))
            resumeFailure = error?.domain == NSURLErrorDomain
                && (codes.contains(error?.code ?? 0) || unavailableResume || unsupportedResume)
        } else { resumeFailure = false }
        guard resumeFailure else { return false }
        return queueFreshAttempt(item)
    }
    /// There is no task ID or delegate event when Foundation rejects task
    /// construction itself. Only a queued job holding resume bytes qualifies.
    @discardableResult
    func recoverRejectedResumeCreation(id: UUID) -> Bool {
        guard var item = ledger?.entries.first(where: { $0.id == id }),
              item.phase == .queued, item.taskID == nil, item.origin == allowedOrigin,
              !item.needsFreshTicket,
              let secret = secret(id), secret.resume != nil, secret.canResume(),
              let origin = try? ServerOrigin(item.origin),
              (try? origin.mediaURL(secret.url, trackID: item.track.id)) != nil else { return false }
        guard item.restartAttempts < 1 else {
            // No second automatic attempt. A later explicit Retry may start
            // fresh, but must never pass the known-rejected blob back again.
            item.discardResumeData = true; item.usedResumeData = false; item.phase = .failed
            item.message = "Данные продолжения отклонены. Нажмите «Повторить» для загрузки заново."
            if !update(item) { ledger?.rememberFailure(item); notify() }
            return true
        }
        return queueFreshAttempt(item)
    }
    private func queueFreshAttempt(_ original: BackgroundDownload) -> Bool {
        var item = original
        item.restartAttempts += 1; item.discardResumeData = true; item.usedResumeData = false
        item.taskID = nil; item.phase = .queued; item.received = 0; item.expected = 0
        item.message = "Загружаю заново…"
        // Persist both the budget and the instruction to ignore old opaque
        // bytes before touching Keychain or creating the replacement task.
        guard update(item) else {
            item.phase = .failed; item.message = "Не удалось сохранить состояние загрузки. Нажмите «Повторить»."
            ledger?.rememberFailure(item); notify(); return true
        }
        pauseEpochs.invalidate(item.id)
        return true
    }
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        let handlers = completionHandlers; completionHandlers.removeAll()
        DispatchQueue.main.async { handlers.forEach { $0() } }
    }
}

final class XASSDownloadAppDelegate: NSObject, UIApplicationDelegate {
    func applicationProtectedDataDidBecomeAvailable(_ application: UIApplication) {
        BackgroundDownloads.shared.start()
    }
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String, completionHandler: @escaping () -> Void) {
        BackgroundDownloads.shared.handleEvents(identifier: identifier, completion: completionHandler)
    }
}
