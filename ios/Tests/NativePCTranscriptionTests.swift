import XCTest
@testable import XASS

@MainActor private final class PCTranscriptionFixture: OwnerService {
    let origin = try! ServerOrigin("https://pc-transcription-fixture.invalid")
    var statuses: [[String: Any]] = []
    var requests: [(String, String, [String: Any]?)] = []
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        guard path == "/api/mini/music/tracks/9/transcription" else { throw OwnerAPIError.invalidResponse }
        requests.append((path, method, body))
        return statuses.isEmpty ? ["ok": true, "status": "none"] : statuses.removeFirst()
    }
}

final class NativePCTranscriptionTests: XCTestCase {
    func testStatusParsingMessagesAndButtonRules() {
        let waiting = PCTranscriptionStatus(response: ["status": "waiting_for_pc", "message": "Расшифруем, когда включится компьютер"])
        XCTAssertEqual(waiting.displayText, "Расшифруем, когда включится компьютер")
        XCTAssertTrue(waiting.isActive)
        XCTAssertTrue(waiting.canRequest, "The button stays while no PC is online")
        let running = PCTranscriptionStatus(response: ["status": "running", "estimate_minutes": 3])
        XCTAssertEqual(running.estimateMinutes, 3)
        XCTAssertEqual(running.displayText, "Расшифровываем на компьютере · ~3 мин")
        XCTAssertFalse(running.canRequest)
        XCTAssertNil(PCTranscriptionStatus(response: ["status": "running", "estimate_minutes": true]).estimateMinutes)
        XCTAssertEqual(PCTranscriptionStatus(response: ["status": "weird"]).status, "none")
        let done = PCTranscriptionStatus(response: ["status": "done"])
        XCTAssertFalse(done.isActive)
        XCTAssertTrue(done.displayText.contains(PCTranscriptionStatus.automaticLabel))
        XCTAssertTrue(PCTranscriptionStatus(response: ["status": "failed"]).canRequest)
    }

    func testPreparingPCShowsSetupPercentAndKeepsPolling() {
        let preparing = PCTranscriptionStatus(response: ["status": "preparing_pc", "setup_percent": 42])
        XCTAssertEqual(preparing.status, "preparing_pc")
        XCTAssertEqual(preparing.setupPercent, 42)
        XCTAssertEqual(preparing.displayText, "ПК готовится к расшифровке, 42%")
        XCTAssertTrue(preparing.isActive)
        XCTAssertFalse(preparing.canRequest, "The job already waits for the installing PC")
        XCTAssertEqual(preparing.symbol, "arrow.down.circle")
        XCTAssertEqual(preparing.pollInterval, .seconds(15))
        let fromServer = PCTranscriptionStatus(response: ["status": "preparing_pc", "setup_percent": 7,
                                                          "message": "ПК готовится к расшифровке, 7%"])
        XCTAssertEqual(fromServer.displayText, "ПК готовится к расшифровке, 7%")
        XCTAssertEqual(PCTranscriptionStatus(response: ["status": "preparing_pc", "setup_percent": 250]).setupPercent, 100)
        XCTAssertNil(PCTranscriptionStatus(response: ["status": "preparing_pc", "setup_percent": true]).setupPercent)
        XCTAssertNil(PCTranscriptionStatus(response: ["status": "queued"]).setupPercent)
    }

    func testTimedLyricsFromPCTranscriptionAreLabelledAndPendingIsParsed() {
        let value = SyncedLyrics(response: ["lyrics": ["status": "synced", "synced": true, "source": "pc_transcription",
            "automatic": true, "lines": [["start": 1.0, "end": 2.0, "text": "раз"]]]], trackID: 9)!
        XCTAssertTrue(value.isAutomatic)
        XCTAssertEqual(value.sourceLabel, "Автоматически, может быть с ошибками")
        XCTAssertNil(value.transcriptionPending)
        let pending = SyncedLyrics(response: ["lyrics": ["status": "not_found", "synced": false, "source": "lrclib",
            "lines": [], "transcription_pending": true]], trackID: 9)!
        XCTAssertEqual(pending.transcriptionPending, true)
        // Old disk cache entries (no key) still decode.
        let legacy = try? JSONDecoder().decode(SyncedLyrics.self, from: Data(#"{"trackID":9,"status":"synced","synced":true,"source":"lrclib","lines":[],"text":"","cachedAt":0}"#.utf8))
        XCTAssertNotNil(legacy)
        let lyrics = NativeLyrics(["lyrics": ["source": "pc_transcription", "text": "раз", "synced": true, "lines": [["time": 1.0, "text": "раз"]]]])
        XCTAssertEqual(lyrics.source, "pc_transcription")
        XCTAssertTrue(lyrics.sourceLabel.contains("может быть с ошибками"))
    }

    @MainActor func testModelRequestsJobAndPollsUntilDone() async throws {
        let api = PCTranscriptionFixture()
        api.statuses = [["ok": true, "status": "queued", "estimate_minutes": 2], ["ok": true, "status": "done"]]
        let model = PCTranscriptionModel()
        var doneCalls = 0
        model.request(api: api, trackID: 9, language: "ru") { doneCalls += 1 }
        for _ in 0..<200 where model.status?.status != "queued" { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.status?.status, "queued")
        XCTAssertEqual(api.requests.first?.1, "POST")
        XCTAssertEqual(api.requests.first?.2?["language"] as? String, "ru")
        XCTAssertEqual(api.requests.first?.2?["force"] as? Bool, false)
        model.stop()
        XCTAssertEqual(doneCalls, 0)
    }

    @MainActor func testForceTimingsRequestAndPlainLyricsPolicy() async throws {
        XCTAssertTrue(PCTranscriptionRequestPolicy.needsForcedTimings(["lyrics": [
            "text": "Первая строка\nВторая строка", "synced": false, "lines": []
        ]]))
        XCTAssertFalse(PCTranscriptionRequestPolicy.needsForcedTimings(["lyrics": [
            "text": "Первая строка", "synced": true,
            "lines": [["start": 1.0, "end": 2.0, "text": "Первая строка"]]
        ]]))
        let api = PCTranscriptionFixture()
        api.statuses = [["ok": true, "status": "waiting_for_pc"]]
        let model = PCTranscriptionModel()
        model.request(api: api, trackID: 9, language: "auto", force: true) {}
        for _ in 0..<100 where api.requests.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(api.requests.first?.2?["force"] as? Bool, true)
        model.stop()
    }

    @MainActor func testFinishedInitialStatusIsNotReportedAsAnActivePoller() async throws {
        let api = PCTranscriptionFixture()
        api.statuses = [["ok": true, "status": "none"]]
        let model = PCTranscriptionModel()
        model.start(api: api, trackID: 9) { XCTFail("An idle status is not a completed transcription") }
        for _ in 0..<100 where model.isPolling { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertFalse(model.isPolling)
        XCTAssertEqual(model.status?.status, "none")
    }
}
