import XCTest
@testable import XASS

final class BackgroundDownloadTests: XCTestCase {
    private func root() throws -> URL {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("XASS-download-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }
    private func job(_ id: Int = 1, origin: String = "https://download.invalid") -> BackgroundDownload {
        BackgroundDownload(id: UUID(), origin: origin,
            track: DownloadedTrack(id: id, title: "Fixture", artist: "Test", duration: 30))
    }
    func testQueueSurvivesRelaunchWithPausedProgressAndNoTicket() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let first = try DownloadLedger(root: root)
        var item = job(); item.phase = .paused; item.received = 128; item.expected = 256
        try first.save([item])
        let restored = try DownloadLedger(root: root)
        XCTAssertEqual(restored.entries.count, 1)
        XCTAssertEqual(restored.entries.first?.id, item.id)
        XCTAssertEqual(restored.entries.first?.phase, .paused)
        XCTAssertEqual(restored.entries.first?.progress, 0.5)
        let raw = try String(contentsOf: first.directory.appendingPathComponent("queue.json"))
        XCTAssertFalse(raw.contains("ticket")); XCTAssertFalse(raw.contains("resumeData"))
        XCTAssertFalse(raw.contains("Authorization"))
    }
    func testOriginNamespacesAndDuplicateRestoration() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let ledger = try DownloadLedger(root: root)
        let first = job(), duplicate = job(), other = job(origin: "https://other.invalid")
        try ledger.save([first, duplicate, other, job(-1), job(origin: "http://insecure.invalid")])
        let restored = try DownloadLedger(root: root)
        XCTAssertEqual(restored.entries.map(\.id), [first.id, other.id])
    }
    func testBoundedQueueAndSafeStagingNames() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let ledger = try DownloadLedger(root: root)
        XCTAssertThrowsError(try ledger.save((1...101).map { job($0) }))
        var item = job(); item.track.fileExtension = "../../secret"
        XCTAssertEqual(ledger.stagedFile(item).deletingLastPathComponent(), ledger.directory)
        XCTAssertEqual(ledger.stagedFile(item).pathExtension, "audio")
    }
    func testIncompleteResponseCannotBecomeOfflineTrack() throws {
        let url = URL(string: "https://download.invalid/api/music/tracks/1/stream?ticket=fixture")!
        let resumed = HTTPURLResponse(url: url, statusCode: 206, httpVersion: nil,
            headerFields: ["Content-Range": "bytes 50-99/100", "Content-Length": "50"])!
        XCTAssertTrue(BackgroundDownloads.completeResponse(resumed, bytes: 100))
        XCTAssertFalse(BackgroundDownloads.completeResponse(resumed, bytes: 50))
        let complete = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: ["Content-Length": "100"])!
        XCTAssertTrue(BackgroundDownloads.completeResponse(complete, bytes: 100))
        XCTAssertFalse(BackgroundDownloads.completeResponse(complete, bytes: 99))
        let expired = HTTPURLResponse(url: url, statusCode: 403, httpVersion: nil, headerFields: nil)!
        XCTAssertFalse(BackgroundDownloads.completeResponse(expired, bytes: 100))
    }
    func testInterruptedCommitCanReplayWithoutCopyingOrLosingOriginal() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let ledger = try DownloadLedger(root: root)
        var item = job(); item.phase = .committing; item.track.fileExtension = "mp3"
        let bytes = Data(repeating: 0x51, count: 1024)
        try bytes.write(to: ledger.stagedFile(item))
        try ledger.save([item])
        let first = try ledger.prepareCommit(item, in: root)
        let stagedAttributes = try FileManager.default.attributesOfItem(atPath: ledger.stagedFile(item).path)
        let linkedAttributes = try FileManager.default.attributesOfItem(atPath: first.path)
        XCTAssertEqual(stagedAttributes[.systemFileNumber] as? NSNumber, linkedAttributes[.systemFileNumber] as? NSNumber)
        let target = root.appendingPathComponent("1.mp3")
        try FileManager.default.moveItem(at: first, to: target)
        // Simulate process death after media publication but before index write.
        let restored = try DownloadLedger(root: root)
        let retry = try restored.prepareCommit(restored.entries[0], in: root)
        XCTAssertEqual(try Data(contentsOf: retry), bytes)
        XCTAssertEqual(try Data(contentsOf: target), bytes)
        try FileManager.default.removeItem(at: ledger.stagedFile(item))
        XCTAssertEqual(try Data(contentsOf: target), bytes)
    }
    func testProgressClampsAndPausedJobDoesNotBlockRetry() {
        var item = job(); item.received = 400; item.expected = 100
        XCTAssertEqual(item.progress, 1)
        item.phase = .paused; XCTAssertFalse(item.active)
        item.phase = .queued; XCTAssertTrue(item.active)
        item.expected = 0; XCTAssertNil(item.progress)
    }
    func testStalePauseCallbackCannotAttachToAnotherTransfer() {
        let id = UUID(); var epochs = DownloadPauseEpochs()
        let first = epochs.begin(id)
        XCTAssertTrue(epochs.accepts(id, epoch: first))
        // A resume/new URL invalidates its pending cancellation callback.
        epochs.invalidate(id)
        let second = epochs.begin(id)
        XCTAssertFalse(epochs.accepts(id, epoch: first))
        XCTAssertTrue(epochs.accepts(id, epoch: second))
        epochs.invalidate(id)
        XCTAssertFalse(epochs.accepts(id, epoch: second))
    }
    func testFailedCommitCanRetryWithoutNetworkTicket() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let ledger = try DownloadLedger(root: root)
        var item = job(); item.phase = .failed; item.track.fileExtension = "flac"
        XCTAssertFalse(ledger.canReplayCommit(item))
        try Data(repeating: 0x51, count: 1024).write(to: ledger.stagedFile(item))
        XCTAssertTrue(ledger.canReplayCommit(item))
        item.phase = .paused
        XCTAssertFalse(ledger.canReplayCommit(item), "An unrelated paused transfer must not consume old staged bytes")
        item.phase = .committing
        XCTAssertTrue(ledger.canReplayCommit(item))
        try ledger.save([item])
        let restored = try DownloadLedger(root: root)
        XCTAssertTrue(restored.canReplayCommit(restored.entries[0]))
    }

    @MainActor func testHeldTicketReplyAfterCancelCannotRecreateLedgerJob() throws {
        let root = try root(); defer { try? FileManager.default.removeItem(at: root) }
        let ledger = try DownloadLedger(root: root), refreshes = DownloadTicketRefreshes()
        var item = job(); item.phase = .paused; try ledger.save([item])
        var held: ((Result<String, Error>) -> Void)?
        var received = 0
        refreshes.request(for: item.id, stillCurrent: { ledger.entries.contains { $0.id == item.id } },
            issue: { held = $0 }) { _ in
                received += 1
                try! ledger.save([item]) // The former enqueue would resurrect this job.
            }
        refreshes.invalidate(item.id); try ledger.save([])
        held?(.success("https://download.invalid/fresh-ticket"))
        XCTAssertEqual(received, 0); XCTAssertTrue(ledger.entries.isEmpty)
    }

    @MainActor func testHeldTicketReplyAfterPauseCannotRestartDownloadOrShowLateError() throws {
        let refreshes = DownloadTicketRefreshes(), id = UUID()
        var phase: DownloadPhase = .failed
        var held: ((Result<String, Error>) -> Void)?
        var received = 0
        refreshes.request(for: id, stillCurrent: { true }, issue: { held = $0 }) { _ in
            received += 1; phase = .queued
        }
        refreshes.invalidate(id); phase = .paused
        held?(.success("fresh-ticket")); held?(.failure(XASSErr.invalidMedia))
        XCTAssertEqual(received, 0); XCTAssertEqual(phase, .paused)
    }

    @MainActor func testDoubleTicketRetryAcceptsOnlyNewestHeldReplyExactlyOnce() {
        let refreshes = DownloadTicketRefreshes(), id = UUID()
        var held: [(Result<String, Error>) -> Void] = [], accepted: [String] = []
        for _ in 0..<2 {
            refreshes.request(for: id, stillCurrent: { true }, issue: { held.append($0) }) { result in
                if let url = try? result.get() { accepted.append(url) }
            }
        }
        held[1](.success("new-ticket")); held[0](.success("stale-ticket"))
        held[1](.success("duplicate-reply"))
        XCTAssertEqual(accepted, ["new-ticket"])
    }

    @MainActor func testHeldTicketReplyRequiresSameLedgerJobAndAccountAfterRelaunchOrLogout() {
        let refreshes = DownloadTicketRefreshes(), id = UUID(), account = UUID()
        var currentJob: UUID? = id, currentAccount = account
        var held: [(Result<String, Error>) -> Void] = [], accepted = 0
        let begin = {
            refreshes.request(for: id, stillCurrent: { currentJob == id && currentAccount == account },
                issue: { held.append($0) }) { _ in accepted += 1 }
        }
        begin(); currentJob = UUID(); held[0](.success("other-job"))
        currentJob = id; begin(); currentAccount = UUID(); held[1](.success("other-account"))
        currentAccount = account; begin(); refreshes.invalidateAll(); held[2](.success("logged-out"))
        XCTAssertEqual(accepted, 0)
    }

    func testOneHourTicketResumesAtTwentyMinutesWithoutChangingOpaqueResumeData() throws {
        let now = Date(timeIntervalSince1970: 1_000_000), bytes = Data([0, 255, 10, 3, 78])
        let ticket = try DownloadTicket(url: "https://download.invalid/signed", expiresIn: 3600, receivedAt: now)
        let secret = BackgroundDownloadSecret(url: ticket.url, issuedAt: now, resume: bytes, expiresAt: ticket.expiresAt)
        let stored = try JSONEncoder().encode(secret)
        let restored = try JSONDecoder().decode(BackgroundDownloadSecret.self, from: stored)
        XCTAssertTrue(restored.canResume(at: now.addingTimeInterval(20 * 60)))
        XCTAssertEqual(restored.resume, bytes)
        XCTAssertEqual(restored.url, ticket.url)
        XCTAssertFalse(restored.canResume(at: now.addingTimeInterval(3600)))
        XCTAssertFalse(restored.canResume(at: now.addingTimeInterval(3601)))
        XCTAssertEqual(restored.resume, bytes, "Expiry checks never rewrite opaque URLSession data")
    }

    func testOldSecretWithoutExpiryKeepsFifteenMinuteCompatibilityWindow() throws {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let old = BackgroundDownloadSecret(url: "https://download.invalid/old", issuedAt: now, resume: Data([1, 2]))
        let encoded = try JSONEncoder().encode(old)
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("expiresAt"))
        let restored = try JSONDecoder().decode(BackgroundDownloadSecret.self, from: encoded)
        XCTAssertTrue(restored.canResume(at: now.addingTimeInterval(899)))
        XCTAssertFalse(restored.canResume(at: now.addingTimeInterval(900)))
        XCTAssertFalse(restored.canResume(at: now.addingTimeInterval(1200)))
        XCTAssertFalse(restored.canResume(at: now.addingTimeInterval(-1)))
    }

    func testTicketExpiryRejectsMalformedAndUnboundedServerValues() throws {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let invalidLifetimes: [Any] = [-1, 0, Double.nan, Double.infinity, 86401, "3600", true, NSNull()]
        for value in invalidLifetimes {
            XCTAssertThrowsError(try DownloadTicket(url: "fixture", expiresIn: value, receivedAt: now))
        }
        XCTAssertEqual(try DownloadTicket(url: "fixture", expiresIn: 3600, receivedAt: now).expiresAt,
                       now.addingTimeInterval(3600))
        XCTAssertEqual(try DownloadTicket(url: "legacy", expiresIn: nil, receivedAt: now).expiresAt,
                       now.addingTimeInterval(900))
        let invalid = BackgroundDownloadSecret(url: "fixture", issuedAt: now, resume: nil, expiresAt: now.addingTimeInterval(86401))
        XCTAssertFalse(invalid.canResume(at: now))
        let invalidDates: [Any] = [-1, Double.infinity, Date().timeIntervalSince1970 + 86401, true]
        for value in invalidDates {
            XCTAssertThrowsError(try NativeAudioCommand(["action": "download", "trackId": 1, "downloadExpiresAt": value]))
        }
    }

    func testFastResumeWaitsForHeldCancellationAndStartsWithItsOpaqueData() {
        let id = UUID(); var pauses = DownloadPauseEpochs()
        let epoch = pauses.begin(id), bytes = Data([7, 9, 2, 1])
        var saved: Data?, startedWith: Data?, starts = 0
        let callback: (Data?) -> Void = { data in
            guard let resume = pauses.finish(id, epoch: epoch) else { return }
            saved = data
            if resume { starts += 1; startedWith = saved }
        }
        XCTAssertTrue(pauses.requestResume(id))
        XCTAssertTrue(pauses.requestResume(id), "Repeated Continue coalesces into one intent")
        XCTAssertEqual(starts, 0); XCTAssertNil(saved)
        callback(bytes); callback(Data([0]))
        XCTAssertEqual(starts, 1); XCTAssertEqual(startedWith, bytes)
        XCTAssertFalse(pauses.isPending(id))
    }

    func testRepeatedPauseClearsResumeIntentButKeepsHeldCancellationData() {
        let id = UUID(); var pauses = DownloadPauseEpochs()
        let epoch = pauses.begin(id), bytes = Data([9, 4])
        var saved: Data?, starts = 0
        let callback: (Data?) -> Void = { data in
            guard let resume = pauses.finish(id, epoch: epoch) else { return }
            saved = data; if resume { starts += 1 }
        }
        XCTAssertTrue(pauses.requestResume(id))
        pauses.keepPaused(id)
        XCTAssertTrue(pauses.accepts(id, epoch: epoch))
        callback(bytes)
        XCTAssertEqual(saved, bytes); XCTAssertEqual(starts, 0)
        XCTAssertFalse(pauses.isPending(id))
    }

    func testCancelledOrLoggedOutHeldPauseCannotResumeOrAttachBytesToNewJob() {
        for _ in 0..<2 {
            let id = UUID(); var pauses = DownloadPauseEpochs()
            let epoch = pauses.begin(id)
            var saves = 0, starts = 0
            let callback = {
                guard let resume = pauses.finish(id, epoch: epoch) else { return }
                saves += 1; if resume { starts += 1 }
            }
            XCTAssertTrue(pauses.requestResume(id))
            pauses.invalidate(id) // cancel/logout/new enqueue retire this task.
            let newer = pauses.begin(id)
            callback()
            XCTAssertTrue(pauses.accepts(id, epoch: newer))
            XCTAssertEqual(saves, 0); XCTAssertEqual(starts, 0)
        }
    }

    func testMissingSecretCancellationStillClearsPendingResumeState() {
        let id = UUID(); var pauses = DownloadPauseEpochs()
        let epoch = pauses.begin(id)
        XCTAssertTrue(pauses.requestResume(id))
        let secretAvailable = false
        let callback = {
            guard let _ = pauses.finish(id, epoch: epoch) else { return }
            guard secretAvailable else { return }
            XCTFail("Missing secret must not start network work")
        }
        callback()
        XCTAssertFalse(pauses.isPending(id))
        XCTAssertFalse(pauses.requestResume(id), "A later explicit retry can request a fresh ticket")
    }
}
