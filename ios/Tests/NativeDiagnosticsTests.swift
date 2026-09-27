import XCTest
@testable import XASS

final class NativeDiagnosticsTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suite: String!
    private var directory: URL!

    override func setUpWithError() throws {
        suite = "xass-diagnostics-tests-" + UUID().uuidString
        defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(suite, isDirectory: true)
    }
    override func tearDownWithError() throws {
        defaults.removePersistentDomain(forName: suite)
        if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
    }
    private func recorder() -> NativeDiagnostics { NativeDiagnostics(defaults: defaults, exportDirectory: directory) }

    func testBoundedHistorySurvivesRestartAndOnlyKeepsLatest200Events() {
        let log = recorder()
        for index in 0..<260 { log.record(operation: .musicSession, step: .response, httpStatus: 200, byteCount: index) }
        log.flushPersistence()
        let restored = recorder().snapshot().events
        XCTAssertEqual(restored.count, 200)
        XCTAssertEqual(restored.first?.byteCount, 60)
        XCTAssertEqual(restored.last?.byteCount, 259)
        XCTAssertLessThanOrEqual(defaults.data(forKey: NativeDiagnostics.historyKey)?.count ?? Int.max, NativeDiagnostics.maxStoredBytes)
    }

    func testConcurrentRecordingIsBoundedAndProducesValidPersistence() {
        let log = recorder()
        DispatchQueue.concurrentPerform(iterations: 800) { index in
            log.record(operation: .audioPlayback, step: .waiting, target: .localPlayer, byteCount: index)
        }
        log.flushPersistence()
        XCTAssertEqual(log.snapshot().events.count, 200)
        XCTAssertEqual(recorder().snapshot().events.count, 200)
    }

    func testRecordingCanBeDisabledWithoutDeletingHistoryOrChangingUserData() {
        defaults.set("not-a-log-setting", forKey: "unrelated-setting")
        let log = recorder()
        log.record(operation: .bootstrap, step: .completed)
        log.setEnabled(false)
        log.record(operation: .musicTicket, step: .failed, error: .unauthorized)
        log.flushPersistence()
        let restored = recorder()
        XCTAssertFalse(restored.isEnabled)
        XCTAssertEqual(restored.snapshot().events.count, 1)
        restored.clear(); restored.flushPersistence()
        XCTAssertFalse(restored.isEnabled)
        XCTAssertEqual(defaults.string(forKey: "unrelated-setting"), "not-a-log-setting")
    }

    func testNumericFieldsRejectInvalidCodesNonFiniteDurationAndUnboundedSizes() {
        let log = recorder()
        log.record(operation: .upload, step: .failed, httpStatus: -1, envelopeStatus: 999,
                   error: .tooLarge, byteCount: Int.max, elapsedMilliseconds: .infinity)
        let event = log.snapshot().events[0]
        XCTAssertNil(event.httpStatus); XCTAssertNil(event.envelopeStatus)
        XCTAssertNil(event.byteCount); XCTAssertNil(event.elapsedMilliseconds)
        log.record(operation: .musicTransfer, step: .failed, target: .pcPlayer, httpStatus: 200,
                   envelopeStatus: 409, error: .sourceTimeout, byteCount: 128, elapsedMilliseconds: 12.6)
        let valid = log.snapshot().events[1]
        XCTAssertEqual(valid.httpStatus, 200); XCTAssertEqual(valid.envelopeStatus, 409)
        XCTAssertEqual(valid.elapsedMilliseconds, 13)
        log.flushPersistence()
    }

    func testExportIsJSONDocumentWithExactFieldAllowlistAndNoIdentifiers() async throws {
        let log = recorder()
        log.record(operation: .musicTransfer, step: .failed, target: .pcPlayer, httpStatus: 200,
                   envelopeStatus: 409, error: .targetStartFailed, byteCount: 42, elapsedMilliseconds: 90)
        let url = try await log.exportFile()
        XCTAssertEqual(url.lastPathComponent, "xass-diagnostics.json")
        let data = try Data(contentsOf: url)
        XCTAssertLessThanOrEqual(data.count, NativeDiagnostics.maxStoredBytes)
        let document = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(document.keys), Set(["schemaVersion", "application", "appVersion", "appBuild", "generatedAt", "events"]))
        XCTAssertEqual(document["application"] as? String, "XASS iOS")
        let event = try XCTUnwrap((document["events"] as? [[String: Any]])?.first)
        XCTAssertEqual(Set(event.keys), Set(["time", "operation", "step", "target", "httpStatus", "envelopeStatus", "error", "byteCount", "elapsedMilliseconds"]))
        XCTAssertEqual(event["operation"] as? String, "music_transfer")
        XCTAssertEqual(event["error"] as? String, "target_start_failed")
        let text = String(decoding: data, as: UTF8.self)
        for forbidden in ["http://", "https://", "Cookie", "session_key", "source_name", "track_id", "file://", suite!] {
            XCTAssertFalse(text.contains(forbidden), forbidden)
        }
        log.flushPersistence()
    }

    func testPersistedUnknownFieldsAndInvalidNumbersNeverReachExport() async throws {
        let secret = "https://private.invalid/song-name?session_key=private-cookie"
        let raw: [[String: Any]] = [["time": "2026-09-27T12:00:00Z", "operation": "music_library", "step": "failed",
                                    "target": "server", "error": "unsupported_response", "httpStatus": 999,
                                    "envelopeStatus": -1, "byteCount": -1, "elapsedMilliseconds": -1,
                                    "message": secret, "url": secret, "path": secret]]
        defaults.set(try JSONSerialization.data(withJSONObject: raw), forKey: NativeDiagnostics.historyKey)
        let log = recorder()
        XCTAssertEqual(log.snapshot().events.count, 1)
        let data = try Data(contentsOf: await log.exportFile())
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertFalse(text.contains("private")); XCTAssertFalse(text.contains("song-name"))
        XCTAssertFalse(text.contains("httpStatus")); XCTAssertFalse(text.contains("byteCount"))
        log.flushPersistence()
    }

    func testMalformedUnknownEnumAndOversizedPreferencesAreDiscarded() throws {
        let raw = [["time": "2026-09-27T12:00:00Z", "operation": "a-secret-song-title", "step": "response", "target": "server", "error": "none"]]
        defaults.set(try JSONSerialization.data(withJSONObject: raw), forKey: NativeDiagnostics.historyKey)
        XCTAssertTrue(recorder().snapshot().events.isEmpty)
        defaults.set(Data(repeating: 32, count: NativeDiagnostics.maxStoredBytes + 1), forKey: NativeDiagnostics.historyKey)
        XCTAssertTrue(recorder().snapshot().events.isEmpty)
    }

    func testClearRemovesPreparedFileAndCannotOverwriteEventsRecordedAfterClear() async throws {
        let log = recorder()
        log.record(operation: .musicLibrary, step: .completed)
        let url = try await log.exportFile()
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        log.clear()
        XCTAssertTrue(log.snapshot().events.isEmpty)
        log.record(operation: .musicTransfer, step: .requested, target: .pcPlayer)
        log.flushPersistence()
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        let restored = recorder().snapshot().events
        XCTAssertEqual(restored.count, 1); XCTAssertEqual(restored[0].operation, .musicTransfer)
        let next = try await log.exportFile()
        XCTAssertEqual(next, url, "Exports use one bounded file, not accumulating documents")
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path).count, 1)
        log.flushPersistence()
    }

    func testTrustedBundleVersionStillUsesOnlyNumericVersionSyntax() {
        XCTAssertEqual(NativeDiagnosticSnapshot.safeVersion("0.19.1"), "0.19.1")
        XCTAssertEqual(NativeDiagnosticSnapshot.safeVersion("12"), "12")
        for invalid in [nil, "", "1.2\n", "secret-token", "1.2/user", "https://server", String(repeating: "1", count: 80)] as [String?] {
            XCTAssertEqual(NativeDiagnosticSnapshot.safeVersion(invalid), "unknown")
        }
    }
}
