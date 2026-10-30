import XCTest
@testable import XASS

/// Exercises the same durable recovery handler used by URLSession delegates.
/// No external request, background daemon, or real Keychain entry is needed.
final class DownloadRecoveryTests: XCTestCase {
    private final class SecretFixture {
        var values: [UUID: BackgroundDownloadSecret] = [:]
        var ignoredRemovals = false
        var removals: [UUID] = []
        var saves = 0
        var failSaves = false

        var access: DownloadSecretAccess {
            DownloadSecretAccess(load: { self.values[$0] }, save: { value, id in
                self.saves += 1
                if self.failSaves { throw XASSErr.keychain }
                self.values[id] = value
            }, remove: { id in
                self.removals.append(id)
                if !self.ignoredRemovals { self.values.removeValue(forKey: id) }
            })
        }
    }

    private func root() throws -> URL {
        let value = FileManager.default.temporaryDirectory
            .appendingPathComponent("XASS-download-recovery-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: value, withIntermediateDirectories: true)
        return value
    }

    private func job(_ trackID: Int = 1, taskID: Int = 21,
                     origin: String = "https://download.invalid", resumed: Bool = true) -> BackgroundDownload {
        var value = BackgroundDownload(id: UUID(), origin: origin,
            track: DownloadedTrack(id: trackID, title: "Fixture", artist: "Test", duration: 30))
        value.phase = .downloading; value.taskID = taskID
        value.received = 128; value.expected = 256; value.usedResumeData = resumed
        return value
    }

    private func secret(for item: BackgroundDownload, expired: Bool = false) -> BackgroundDownloadSecret {
        let now = Date()
        return BackgroundDownloadSecret(
            url: item.origin + "/api/music/tracks/\(item.track.id)/stream?ticket=fixture",
            issuedAt: now.addingTimeInterval(-120), resume: Data([7, 9, 2, 1]),
            expiresAt: now.addingTimeInterval(expired ? -60 : 3600))
    }

    private func encoded(_ entries: [BackgroundDownload]) throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(entries)
    }

    private var invalidResume: NSError {
        NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotDecodeContentData)
    }

    @MainActor func testLegacyLedgerWithoutRecoveryFieldsDecodesWithSafeDefaults() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory)
        let item = job()
        var payload = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(item)) as? [String: Any])
        for field in ["restartAttempts", "usedResumeData", "discardResumeData", "needsFreshTicket"] {
            payload.removeValue(forKey: field)
        }
        try JSONSerialization.data(withJSONObject: [payload])
            .write(to: ledger.directory.appendingPathComponent("queue.json"), options: .atomic)
        let restored = try XCTUnwrap(DownloadLedger(root: directory).entries.first)
        XCTAssertEqual(restored.id, item.id)
        XCTAssertEqual(restored.received, 128)
        XCTAssertEqual(restored.taskID, 21)
        XCTAssertEqual(restored.restartAttempts, 0)
        XCTAssertFalse(restored.usedResumeData)
        XCTAssertFalse(restored.discardResumeData)
        XCTAssertFalse(restored.needsFreshTicket)
    }

    @MainActor func testRejectedResumeQueuesOnlyThisFileFromZeroWithVisibleRestart() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)

        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))

        let restarted = try XCTUnwrap(ledger.entries.first)
        XCTAssertEqual(restarted.id, item.id)
        XCTAssertEqual(restarted.track.id, item.track.id)
        XCTAssertEqual(restarted.phase, .queued)
        XCTAssertEqual(restarted.restartAttempts, 1)
        XCTAssertTrue(restarted.discardResumeData)
        XCTAssertFalse(restarted.usedResumeData)
        XCTAssertFalse(restarted.needsFreshTicket)
        XCTAssertNil(restarted.taskID)
        XCTAssertEqual(restarted.received, 0)
        XCTAssertEqual(restarted.expected, 0)
        XCTAssertNil(restarted.progress)
        XCTAssertTrue(restarted.message?.contains("Загружаю заново") == true)
        // A durable discard marker, not a successful Keychain mutation, fences
        // the old opaque bytes until scheduling safely clears the secret.
        XCTAssertEqual(secrets.values[item.id]?.resume, Data([7, 9, 2, 1]))
    }

    @MainActor func testFoundationRejectsInvalidOpaqueResumeDataThroughRealDelegate() async throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        var item = job()
        let secrets = SecretFixture(); secrets.values[item.id] = secret(for: item)
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil; configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        let session = URLSession(configuration: configuration, delegate: manager, delegateQueue: .main)
        defer { session.invalidateAndCancel() }
        // This malformed blob contains no URL or request. Foundation rejects
        // it locally; the injected manager cannot schedule a replacement GET.
        let task = session.downloadTask(withResumeData: Data("invalid-xass-resume-fixture".utf8))
        item.taskID = task.taskIdentifier; task.taskDescription = item.id.uuidString
        try ledger.save([item])
        let settled = expectation(description: "Real URLSession delegate settles rejected resume data")
        var observed = false
        let observer = NotificationCenter.default.addObserver(forName: BackgroundDownloads.changed,
            object: nil, queue: .main) { _ in
                guard !observed, let phase = ledger.entries.first(where: { $0.id == item.id })?.phase,
                      phase != .downloading else { return }
                observed = true; settled.fulfill()
            }
        defer { NotificationCenter.default.removeObserver(observer) }

        task.resume()
        await fulfillment(of: [settled], timeout: 5)

        let recovered = try XCTUnwrap(ledger.entries.first)
        let diagnostic = task.error.map { String(describing: $0) } ?? "Foundation returned no transport error"
        XCTAssertEqual(recovered.phase, .queued, diagnostic)
        XCTAssertEqual(recovered.restartAttempts, 1, diagnostic)
        XCTAssertTrue(recovered.discardResumeData, diagnostic)
        XCTAssertFalse(recovered.usedResumeData)
        XCTAssertNil(recovered.taskID)
        XCTAssertEqual(recovered.received, 0)
        XCTAssertEqual(recovered.expected, 0)
    }

    @MainActor func testFallbackLedgerWriteFailureDoesNotScheduleOrLoseRetryBudgetInMemory() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), other = job(2, taskID: 22), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item); try ledger.save([item, other])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let queue = ledger.directory.appendingPathComponent("queue.json")
        try FileManager.default.removeItem(at: queue)
        try FileManager.default.createDirectory(at: queue, withIntermediateDirectories: false)

        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))

        let failed = try XCTUnwrap(ledger.entries.first { $0.id == item.id })
        XCTAssertEqual(failed.phase, .failed)
        XCTAssertEqual(failed.restartAttempts, 1)
        XCTAssertTrue(failed.discardResumeData)
        XCTAssertFalse(failed.usedResumeData)
        XCTAssertNil(failed.taskID)
        XCTAssertFalse(manager.resume(item.id), "An unwritable journal must not start another transfer")
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 416, error: nil))
        XCTAssertEqual(try encoded(ledger.entries.filter { $0.id == other.id }), try encoded([other]))
        XCTAssertEqual(secrets.saves, 0)
        XCTAssertTrue(secrets.removals.isEmpty)
    }

    @MainActor func testOneFailedFileLeavesOtherFortyNineJobsExactlyUntouched() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        var jobs = (1...50).map { job($0, taskID: 20 + $0) }
        for index in 1..<jobs.count {
            jobs[index].phase = index.isMultiple(of: 2) ? .paused : .downloading
            jobs[index].received = Int64(index * 10)
            jobs[index].expected = 1024
            jobs[index].message = "Original \(index)"
        }
        let before = try encoded(Array(jobs.dropFirst())), secrets = SecretFixture()
        for item in jobs { secrets.values[item.id] = secret(for: item) }
        try ledger.save(jobs)
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)

        XCTAssertTrue(manager.recoverFailure(id: jobs[0].id, taskID: 21, statusCode: 416, error: nil))

        XCTAssertEqual(ledger.entries.count, 50)
        XCTAssertEqual(try encoded(Array(ledger.entries.dropFirst())), before)
        XCTAssertEqual(secrets.values.count, 50)
        XCTAssertEqual(secrets.saves, 0)
        XCTAssertTrue(secrets.removals.isEmpty)
    }

    @MainActor func testRestartBudgetAndDiscardMarkerSurviveRelaunch() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let first = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        XCTAssertTrue(first.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))

        let restored = try DownloadLedger(root: directory)
        var secondAttempt = try XCTUnwrap(restored.entries.first)
        XCTAssertEqual(secondAttempt.restartAttempts, 1)
        XCTAssertTrue(secondAttempt.discardResumeData)
        secondAttempt.phase = .downloading; secondAttempt.taskID = 22
        secondAttempt.usedResumeData = true
        try restored.save([secondAttempt])
        let afterRelaunch = BackgroundDownloads(ledger: restored, origin: origin, secrets: secrets.access)
        let before = try encoded(restored.entries)

        XCTAssertFalse(afterRelaunch.recoverFailure(id: item.id, taskID: 22, statusCode: 416, error: nil))
        XCTAssertEqual(try encoded(restored.entries), before, "Relaunch must not replenish the automatic retry budget")
    }

    @MainActor func testDuplicateAndOldTaskCallbacksCannotConsumeAnotherRetryOrChangeNewTask() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
        let once = try encoded(ledger.entries)
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 403, error: nil))
        XCTAssertEqual(try encoded(ledger.entries), once)

        var newer = try XCTUnwrap(ledger.entries.first)
        newer.phase = .downloading; newer.taskID = 22; newer.received = 42
        try ledger.save([newer])
        let before = try encoded(ledger.entries)
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 401, error: nil))
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
        XCTAssertEqual(try encoded(ledger.entries), before)
        XCTAssertTrue(secrets.removals.isEmpty)
    }

    @MainActor func testFreshNeverResumedTaskDoesNotGetAutomaticDecodeOrRangeRetry() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(resumed: false), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries)

        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 416, error: nil))
        XCTAssertEqual(try encoded(ledger.entries), before)
    }

    @MainActor func testMissingResumeTemporaryFileCanRestartOnce() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item); try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil,
            error: NSError(domain: NSURLErrorDomain, code: NSURLErrorFileDoesNotExist)))
        XCTAssertEqual(ledger.entries.first?.restartAttempts, 1)
        XCTAssertEqual(ledger.entries.first?.phase, .queued)
    }

    @MainActor func testCancellationNetworkTLSAndDiskFailuresNeverTriggerAutomaticRestart() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries)
        let errors = [
            NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorTimedOut),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorNetworkConnectionLost),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorSecureConnectionFailed),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorServerCertificateUntrusted),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotWriteToFile),
            NSError(domain: NSCocoaErrorDomain, code: NSFileWriteOutOfSpaceError),
            NSError(domain: "fixture.other-domain", code: NSURLErrorCannotDecodeContentData)
        ]
        for error in errors {
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: error),
                           "Unexpected automatic retry for \(error.domain) / \(error.code)")
            XCTAssertEqual(try encoded(ledger.entries), before)
        }
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 500, error: nil))
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 200, error: nil))
        XCTAssertEqual(try encoded(ledger.entries), before)
    }

    @MainActor func testHTTPAuthenticationFailuresInvalidateTicketWithoutTransportError() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let secrets = SecretFixture()
        for statusCode in [401, 403] {
            var item = job(resumed: false); item.restartAttempts = 1
            secrets.values[item.id] = secret(for: item)
            try ledger.save([item])
            let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)

            XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: statusCode, error: nil))

            let rejected = try XCTUnwrap(ledger.entries.first)
            XCTAssertEqual(rejected.phase, .failed)
            XCTAssertTrue(rejected.needsFreshTicket)
            XCTAssertTrue(rejected.discardResumeData)
            XCTAssertNil(rejected.taskID)
            XCTAssertEqual(rejected.restartAttempts, 1, "An auth rejection is not another resume fallback")
            XCTAssertNil(secrets.values[item.id])
            XCTAssertTrue(secrets.removals.contains(item.id))
            XCTAssertFalse(manager.resume(item.id))
        }
    }

    @MainActor func testRejectedTicketCannotBeReusedWhenKeychainRemovalFailsEvenAfterRelaunch() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.ignoredRemovals = true; secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 403, error: nil))
        XCTAssertTrue(secrets.values[item.id]?.canResume() == true, "Simulate SecItemDelete not clearing an otherwise unexpired ticket")
        XCTAssertFalse(manager.resume(item.id))

        let restored = try DownloadLedger(root: directory)
        let afterRelaunch = BackgroundDownloads(ledger: restored, origin: origin, secrets: secrets.access)
        XCTAssertTrue(restored.entries.first?.needsFreshTicket == true)
        XCTAssertFalse(afterRelaunch.resume(item.id))
        XCTAssertEqual(restored.entries.first?.phase, .failed)
    }

    @MainActor func testAuthRejectionStaysFailClosedWhenLedgerAndSecretStorageBothFail() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), other = job(2, taskID: 22), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        secrets.values[other.id] = secret(for: other)
        secrets.failSaves = true; secrets.ignoredRemovals = true
        try ledger.save([item, other])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        // This path belongs only to the per-test temporary fixture. A directory
        // at queue.json makes the atomic ledger write fail deterministically.
        let queue = ledger.directory.appendingPathComponent("queue.json")
        try FileManager.default.removeItem(at: queue)
        try FileManager.default.createDirectory(at: queue, withIntermediateDirectories: false)

        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 403, error: nil))

        let rejected = try XCTUnwrap(ledger.entries.first { $0.id == item.id })
        XCTAssertEqual(rejected.phase, .failed)
        XCTAssertTrue(rejected.needsFreshTicket)
        XCTAssertTrue(rejected.discardResumeData)
        XCTAssertNil(rejected.taskID)
        XCTAssertFalse(manager.resume(item.id))
        XCTAssertTrue(secrets.values[item.id]?.canResume() == true,
                      "Both credential invalidation and deletion were deliberately refused")
        XCTAssertEqual(try encoded(ledger.entries.filter { $0.id == other.id }), try encoded([other]))
        XCTAssertEqual(ledger.entries.count, 2)
    }

    @MainActor func testExpiredMissingOrCrossOriginSecretCannotRestart() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries)
        let now = Date()
        let unsafe = BackgroundDownloadSecret(url: "https://other.invalid/api/music/tracks/1/stream?ticket=fixture",
            issuedAt: now.addingTimeInterval(-60), resume: Data([1]), expiresAt: now.addingTimeInterval(300))
        let candidates: [BackgroundDownloadSecret?] = [nil, secret(for: item, expired: true), unsafe]
        for candidate in candidates {
            secrets.values[item.id] = candidate
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
            XCTAssertEqual(try encoded(ledger.entries), before)
        }
    }

    @MainActor func testOtherActiveOriginCannotAutomaticallyRestartOldServerJob() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), other = try ServerOrigin("https://other.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item); try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: other, secrets: secrets.access)
        let before = try encoded(ledger.entries)

        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
        XCTAssertEqual(try encoded(ledger.entries), before)
    }

    @MainActor func testNonDownloadingPhasesFenceDecodeAndAuthenticationCallbacks() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let secrets = SecretFixture()
        for phase in [DownloadPhase.queued, .paused, .failed, .committing] {
            var item = job(); item.phase = phase
            secrets.values[item.id] = secret(for: item); try ledger.save([item])
            let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
            let before = try encoded(ledger.entries)
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 401, error: nil))
            XCTAssertEqual(try encoded(ledger.entries), before)
        }
        XCTAssertTrue(secrets.removals.isEmpty)
    }

    @MainActor func testExplicitPauseFencesLateCallbackAndRetainsConsumedBudget() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        var item = job(); item.restartAttempts = 1
        let secrets = SecretFixture(); secrets.values[item.id] = secret(for: item)
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        manager.pause(item.id)
        let paused = try encoded(ledger.entries)

        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: invalidResume))
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 403, error: nil))
        XCTAssertEqual(try encoded(ledger.entries), paused)
        XCTAssertEqual(ledger.entries.first?.phase, .paused)
        XCTAssertEqual(ledger.entries.first?.restartAttempts, 1)
        XCTAssertTrue(manager.resume(item.id))
        XCTAssertEqual(ledger.entries.first?.restartAttempts, 1, "Manual resume is not a newly issued ticket")
    }

    @MainActor func testLogoutAndCancelCannotBeUndoneByLateRecoveryCallback() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let secrets = SecretFixture(), loggedOut = job(), cancelled = job(2, taskID: 22)
        secrets.values[loggedOut.id] = secret(for: loggedOut)
        secrets.values[cancelled.id] = secret(for: cancelled)
        try ledger.save([loggedOut, cancelled])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        manager.cancel(cancelled.id)
        manager.suspend(origin: origin, forgetSecrets: true)
        let before = try encoded(ledger.entries)

        XCTAssertFalse(manager.recoverFailure(id: cancelled.id, taskID: 22, statusCode: 416, error: nil))
        XCTAssertFalse(manager.recoverFailure(id: loggedOut.id, taskID: 21, statusCode: nil, error: invalidResume))
        XCTAssertFalse(manager.recoverFailure(id: loggedOut.id, taskID: 21, statusCode: 403, error: nil))
        XCTAssertEqual(try encoded(ledger.entries), before)
        XCTAssertEqual(ledger.entries.map(\.id), [loggedOut.id])
        XCTAssertEqual(ledger.entries.first?.phase, .paused)
        XCTAssertFalse(manager.resume(loggedOut.id))
        XCTAssertTrue(secrets.values.isEmpty)
    }

    @MainActor func testFreshTicketEnqueueResetsBudgetAndRejectedTicketMarkersOnlyForItsTrack() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        var item = job(); item.phase = .failed; item.taskID = nil; item.restartAttempts = 1
        item.discardResumeData = true; item.needsFreshTicket = true
        let other = job(2, taskID: 22), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item)
        secrets.values[other.id] = secret(for: other)
        try ledger.save([item, other])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let freshURL = try origin.mediaURL(
            "/api/music/tracks/1/stream?ticket=fresh-ticket", trackID: 1)
        try manager.enqueue(origin: origin, track: item.track, url: freshURL,
                            expiresAt: Date().addingTimeInterval(300))

        let fresh = try XCTUnwrap(ledger.entries.first { $0.id == item.id })
        XCTAssertEqual(fresh.phase, .queued)
        XCTAssertEqual(fresh.restartAttempts, 0)
        XCTAssertFalse(fresh.usedResumeData)
        XCTAssertFalse(fresh.discardResumeData)
        XCTAssertFalse(fresh.needsFreshTicket)
        XCTAssertNil(fresh.taskID)
        XCTAssertNil(fresh.message)
        XCTAssertEqual(fresh.received, 0)
        XCTAssertEqual(fresh.expected, 0)
        XCTAssertEqual(secrets.values[item.id]?.url, freshURL.absoluteString)
        XCTAssertNil(secrets.values[item.id]?.resume)
        XCTAssertEqual(try encoded(ledger.entries.filter { $0.id == other.id }), try encoded([other]))
        // The injected manager never creates a background session. Thus an
        // old delegate callback can be tested before any replacement task.
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 401, error: nil))
        XCTAssertEqual(secrets.values[item.id]?.url, freshURL.absoluteString)
        XCTAssertFalse(ledger.entries.first { $0.id == item.id }?.needsFreshTicket ?? true)
    }

    @MainActor func testFailedFreshTicketStorageDoesNotResetBudgetOrRemoveExistingSecret() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        var item = job(); item.phase = .failed; item.restartAttempts = 1; item.needsFreshTicket = true
        let secrets = SecretFixture(); secrets.values[item.id] = secret(for: item); secrets.failSaves = true
        try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries), oldURL = secrets.values[item.id]?.url
        let freshURL = try origin.mediaURL("/api/music/tracks/1/stream?ticket=fresh", trackID: 1)

        XCTAssertThrowsError(try manager.enqueue(origin: origin, track: item.track, url: freshURL,
                                                 expiresAt: Date().addingTimeInterval(300)))
        XCTAssertEqual(try encoded(ledger.entries), before)
        XCTAssertEqual(secrets.values[item.id]?.url, oldURL)
        XCTAssertTrue(secrets.removals.isEmpty)
        XCTAssertFalse(manager.resume(item.id))
    }

    @MainActor func testCrossOriginAndWrongTrackHTTPResponsesCannotAuthorizeRecovery() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item); try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries)
        let responses = [
            URL(string: "https://other.invalid/api/music/tracks/1/stream?ticket=fixture")!,
            URL(string: "https://download.invalid/api/music/tracks/2/stream?ticket=fixture")!,
            URL(string: "http://download.invalid/api/music/tracks/1/stream?ticket=fixture")!
        ]
        for response in responses {
            for status in [401, 403, 416] {
                XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: status,
                                                     error: nil, responseURL: response))
            }
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 200,
                                                 error: invalidResume, responseURL: response))
        }
        XCTAssertEqual(try encoded(ledger.entries), before)
        XCTAssertTrue(secrets.removals.isEmpty)
    }

    @MainActor func testRangeRejectionDoesNotMaskTLSOrCancellationFailure() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item); try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries)
        for code in [NSURLErrorCancelled, NSURLErrorSecureConnectionFailed, NSURLErrorCannotWriteToFile] {
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 416,
                error: NSError(domain: NSURLErrorDomain, code: code)))
        }
        XCTAssertEqual(try encoded(ledger.entries), before)
        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 416,
            error: NSError(domain: NSURLErrorDomain, code: NSURLErrorBadServerResponse)))
        XCTAssertEqual(ledger.entries.first?.phase, .queued)
    }

    @MainActor func testUnavailableResumeStateRequiresNoHTTPResponseOrUnderlyingFailure() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let item = job(), secrets = SecretFixture()
        secrets.values[item.id] = secret(for: item); try ledger.save([item])
        let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
        let before = try encoded(ledger.entries)
        let unavailable = NSError(domain: NSURLErrorDomain, code: NSURLErrorResourceUnavailable)
        let tls = NSError(domain: NSURLErrorDomain, code: NSURLErrorSecureConnectionFailed)
        let underlying = NSError(domain: NSURLErrorDomain, code: NSURLErrorResourceUnavailable,
                                 userInfo: [NSUnderlyingErrorKey: tls])
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 404, error: unavailable))
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 200, error: unavailable))
        XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: underlying))
        XCTAssertEqual(try encoded(ledger.entries), before)
        XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: nil, error: unavailable))
        XCTAssertEqual(ledger.entries.first?.restartAttempts, 1)
    }

    @MainActor func testRecognizedResumeDecodingFailuresGetAtMostOneFreshAttempt() throws {
        let directory = try root(); defer { try? FileManager.default.removeItem(at: directory) }
        let ledger = try DownloadLedger(root: directory), origin = try ServerOrigin("https://download.invalid")
        let secrets = SecretFixture()
        for code in [NSURLErrorCannotDecodeRawData, NSURLErrorCannotDecodeContentData,
                     NSURLErrorDownloadDecodingFailedMidStream, NSURLErrorDownloadDecodingFailedToComplete] {
            let item = job()
            secrets.values[item.id] = secret(for: item); try ledger.save([item])
            let manager = BackgroundDownloads(ledger: ledger, origin: origin, secrets: secrets.access)
            let error = NSError(domain: NSURLErrorDomain, code: code)
            XCTAssertTrue(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 206, error: error))
            XCTAssertEqual(ledger.entries.first?.restartAttempts, 1)
            XCTAssertFalse(manager.recoverFailure(id: item.id, taskID: 21, statusCode: 206, error: error))
            XCTAssertEqual(ledger.entries.first?.restartAttempts, 1)
        }
    }
}
