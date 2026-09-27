import XCTest
@testable import XASS

final class NativeLyricsTests: XCTestCase {
    private func timed(_ lines: [[String: Any]], source: String = "owner") -> NativeLyrics {
        NativeLyrics(["lyrics": ["source": source, "synced": true, "lines": lines]])
    }
    func testRealTimestampsHandleIntroBackwardSeekAndSimultaneousLines() {
        let lyrics = timed([["time": 20.0, "text": "Second"], ["time": 5.5, "text": "First"],
                            ["time": 20.0, "text": "Translation"], ["time": 40.25, "text": "Last"]])
        XCTAssertNil(lyrics.activeLine(at: 5.49))
        XCTAssertEqual(lyrics.activeLine(at: 5.5), 0)
        XCTAssertEqual(lyrics.activeLine(at: 40.25), 3)
        XCTAssertEqual(lyrics.activeLine(at: 10), 0, "Backward seek must immediately find the previous real line")
        XCTAssertEqual(lyrics.activeLine(at: 20), 2)
        XCTAssertTrue(lyrics.isActive(lyrics.lines[1], at: 20))
        XCTAssertTrue(lyrics.isActive(lyrics.lines[2], at: 20))
        XCTAssertFalse(lyrics.isActive(lyrics.lines[0], at: 20))
        XCTAssertNil(lyrics.activeLine(at: -.infinity))
    }
    func testInvalidAndBooleanTimesNeverBecomeInventedLyricsTiming() {
        let lyrics = timed([["time": true, "text": "Boolean is not a timestamp"],
                            ["time": -1, "text": "Negative"], ["time": Double.nan, "text": "NaN"],
                            ["time": "10", "text": "Unparsed string"], ["time": 86_401, "text": "Too long"],
                            ["time": 1.25, "text": "Genuine timestamp"]])
        XCTAssertEqual(lyrics.lines.map(\.text), ["Genuine timestamp"])
        XCTAssertEqual(lyrics.lines[0].time, 1.25)
        XCTAssertNil(lyrics.activeLine(at: 1))
        XCTAssertNil(lyrics.activeLine(at: .nan))
    }
    func testEmptyTimedMarkersEndVocalLinesWithoutRenderingOrSeekingBlankRows() {
        let lyrics = timed([["time": 0, "text": "Vocal"], ["time": 3, "text": " "],
                            ["time": 6, "text": "Next"], ["time": 6, "text": "Translation"],
                            ["time": 6, "text": ""]])
        XCTAssertEqual(lyrics.activeLine(at: 2.99), 0)
        XCTAssertNil(lyrics.activeLine(at: 3))
        XCTAssertNil(lyrics.activeLine(at: 5.99))
        XCTAssertEqual(lyrics.activeLine(at: 6), 3)
        XCTAssertTrue(lyrics.isActive(lyrics.lines[2], at: 6))
        XCTAssertTrue(lyrics.isActive(lyrics.lines[3], at: 6))
        XCTAssertFalse(lyrics.isActive(lyrics.lines[4], at: 6))
        XCTAssertNil(lyrics.seekTime(lineID: 1, displayedTrackID: 7, currentTrackID: 7))
        XCTAssertEqual(lyrics.lines.filter { !$0.isPause }.count, 3)
        let onlyPauses = timed([["time": 0, "text": ""], ["time": 3, "text": " "]])
        XCTAssertTrue(onlyPauses.empty)
        XCTAssertFalse(onlyPauses.synced)
    }
    func testSeekRequiresTimedLyricsAndTheStillCurrentTrack() {
        let lyrics = timed([["time": 12.5, "text": "Fixture"]])
        XCTAssertEqual(lyrics.seekTime(lineID: 0, displayedTrackID: 7, currentTrackID: 7), 12.5)
        XCTAssertNil(lyrics.seekTime(lineID: 0, displayedTrackID: 7, currentTrackID: 8))
        XCTAssertNil(lyrics.seekTime(lineID: 0, displayedTrackID: 7, currentTrackID: nil))
        XCTAssertNil(lyrics.seekTime(lineID: -1, displayedTrackID: 7, currentTrackID: 7))
        let plain = NativeLyrics(["lyrics": ["text": "Only plain text", "source": "owner", "synced": false,
                                            "lines": [["time": 0, "text": "Only plain text"]]]])
        XCTAssertNil(plain.seekTime(lineID: 0, displayedTrackID: 7, currentTrackID: 7))
        XCTAssertNil(plain.activeLine(at: 30))
    }
    func testLyricsInputIsBoundedAndExternalProvenanceIsStrict() {
        let lyrics = timed((0..<2200).map { ["time": Double($0), "text": "Fixture \($0)"] })
        XCTAssertEqual(lyrics.lines.count, 2000)
        let external = NativeLyrics(["lyrics": ["source": "lrclib", "source_url": "https://lrclib.net/lyrics/123",
            "text": "Public-domain test fixture", "synced": false], "enrichment": ["status": "matched"]])
        XCTAssertEqual(external.source, "lrclib"); XCTAssertEqual(external.status, "matched")
        XCTAssertEqual(external.sourceURL?.absoluteString, "https://lrclib.net/lyrics/123")
        for value in ["http://lrclib.net/lyrics/123", "https://lrclib.net.evil.invalid/lyrics/123",
                      "https://user:password@lrclib.net/lyrics/123", "https://lrclib.net/lyrics/123?ticket=secret",
                      "https://lrclib.net/lyrics/123#fragment", "https://lrclib.net:8080/lyrics/123", "javascript:alert(1)"] {
            XCTAssertNil(NativeLyrics.provenanceURL(value, source: "lrclib"))
        }
        XCTAssertNil(NativeLyrics.provenanceURL("https://lrclib.net/lyrics/123", source: "owner"))
        XCTAssertEqual(NativeLyrics(["lyrics": ["source": "on_device_transcription", "text": "Fixture"]]).sourceLabel,
                       "Расшифровка на устройстве")
    }
    func testPlaybackClockInterpolatesBetweenRemotePollsButNeverRunsWithoutReports() {
        let sample = Date(timeIntervalSince1970: 1_000)
        func position(_ elapsed: Double, playing: Bool = true, duration: Double = 90, limit: Double = 6) -> Double {
            NativeLyricsClock.position(10, duration: duration, playing: playing, sampledAt: sample,
                                       now: sample.addingTimeInterval(elapsed), projectionLimit: limit)
        }
        XCTAssertEqual(position(0.25), 10.25)
        XCTAssertEqual(position(5), 15)
        XCTAssertEqual(position(600), 16, "Lost remote reports must not advance through the whole song")
        XCTAssertEqual(position(5, playing: false), 10, "Pause/loading/error never extrapolate")
        XCTAssertEqual(position(5, duration: 12), 12)
        XCTAssertEqual(position(-10), 10)
        XCTAssertEqual(position(5, limit: 0), 10)
        XCTAssertEqual(position(5, limit: 1.25), 11.25)
        XCTAssertEqual(NativeLyricsClock.position(10, duration: 90, playing: true, sampledAt: .distantPast, now: sample), 10)
        XCTAssertEqual(NativeLyricsClock.position(.nan, duration: 90, playing: true, sampledAt: sample, now: sample), 0)
    }
    func testRemoteProjectionBudgetDoesNotRenewAnAlreadyStaleServerPosition() {
        let server = "2026-09-27T18:00:15Z"
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: server, updatedAt: "2026-09-27T18:00:13Z"), 6)
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: server, updatedAt: "2026-09-27T18:00:01.250000Z"), 1.25)
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: server, updatedAt: "2026-09-27T18:00:00Z"), 0)
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: server, updatedAt: "2026-09-26T18:00:00Z"), 0)
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: nil, updatedAt: nil), 0)
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: server, updatedAt: "untrusted"), 0)
        XCTAssertEqual(NativeLyricsClock.projectionLimit(serverTime: server, updatedAt: "2026-09-28T18:00:00Z"), 0)
    }
    func testManualBrowsingStaysPausedUntilExplicitResume() {
        var follow = NativeLyricsFollowing()
        XCTAssertTrue(follow.enabled)
        follow.drag(horizontal: 30, vertical: 4); XCTAssertTrue(follow.enabled)
        follow.drag(horizontal: 0, vertical: 5); XCTAssertTrue(follow.enabled)
        follow.drag(horizontal: 2, vertical: -20); XCTAssertFalse(follow.enabled)
        follow.drag(horizontal: 0, vertical: 0); XCTAssertFalse(follow.enabled)
        follow.resume(); XCTAssertTrue(follow.enabled)
        follow.pause(); XCTAssertFalse(follow.enabled)
    }
    func testTranscriptEditorByteLimitReportsOverflowWithoutTruncatingUnicode() {
        XCTAssertNil(NativeEnrichmentPresentation.transcriptProblem(String(repeating: "a", count: 64_000)))
        XCTAssertNotNil(NativeEnrichmentPresentation.transcriptProblem(String(repeating: "a", count: 64_001)))
        XCTAssertNil(NativeEnrichmentPresentation.transcriptProblem(String(repeating: "я", count: 32_000)))
        XCTAssertNotNil(NativeEnrichmentPresentation.transcriptProblem(String(repeating: "я", count: 32_001)))
    }
}

@MainActor private final class LyricsClockService: OwnerService {
    let origin = try! ServerOrigin("https://lyrics-clock-fixture.invalid")
    var state = "playing"
    var updatedAt = "2026-09-27T18:00:14Z"
    var failEnrichment = false
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        if path == "/api/mini/music/session" {
            return ["ok": true, "session": ["track_id": 7, "device": "agent:Fixture", "state": state, "position": 10,
                "session_key": "fixture-remote-session", "server_time": "2026-09-27T18:00:15Z", "updated_at": updatedAt]]
        }
        if path == "/api/mini/music/players" { return ["ok": true, "players": []] }
        if path == "/api/mini/music/tracks/7/enrichment", method == "POST" {
            if failEnrichment { throw OwnerAPIError.invalidResponse }
            return ["ok": true, "track": ["id": 7, "title": "Fixture"]]
        }
        throw OwnerAPIError.invalidResponse
    }
}

@MainActor final class NativeLyricsClockStoreTests: XCTestCase {
    func testOnlySuccessfulMutationsInvalidateLyricsNotReadResponseApplication() async throws {
        let service = LyricsClockService()
        let store = NativeStore(api: service, audio: AudioController())
        defer { store.disconnect() }
        XCTAssertEqual(store.lyricsRevision, 0)
        store.applyEnrichedTrack(["track": ["id": 7, "title": "GET fixture"]])
        XCTAssertEqual(store.lyricsRevision, 0, "GET lyrics applies track metadata without triggering itself again")
        _ = try await store.enrichTrack(7)
        XCTAssertEqual(store.lyricsRevision, 1)
        service.failEnrichment = true
        do { _ = try await store.enrichTrack(7); XCTFail("Expected failed request") }
        catch { }
        XCTAssertEqual(store.lyricsRevision, 1, "A failed mutation must not claim to have changed lyrics")
        store.invalidateLyrics()
        XCTAssertEqual(store.lyricsRevision, 2, "Confirmed restore/transcript mutations share the same explicit signal")
        store.applyEnrichedTrack(["track": ["id": 7, "title": "Reloaded fixture"]])
        XCTAssertEqual(store.lyricsRevision, 2)
    }
    func testRemoteSamplesReanchorEvenWhenTheReportedPositionIsUnchanged() async throws {
        let service = LyricsClockService()
        let store = NativeStore(api: service, audio: AudioController())
        defer { store.disconnect() }
        let started = Date()
        try await store.refreshSession()
        XCTAssertGreaterThanOrEqual(store.playbackSampleAt, started)
        XCTAssertEqual(store.playbackProjectionLimit, 6)
        let first = store.playbackSampleAt
        service.updatedAt = "2026-09-26T18:00:00Z"
        try await store.refreshSession()
        XCTAssertEqual(store.position, 10)
        XCTAssertGreaterThanOrEqual(store.playbackSampleAt, first)
        XCTAssertEqual(store.playbackProjectionLimit, 0, "Repeated stale snapshots must not invent fresh playback")
        service.state = "paused"; try await store.refreshSession()
        XCTAssertFalse(store.playing)
    }
}
