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
}
