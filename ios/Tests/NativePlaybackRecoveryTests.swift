import XCTest
@testable import XASS

@MainActor private final class RecoveryOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://recovery-fixture.invalid")
    var requests: [(String, String, [String: Any]?)] = []
    var fail = false
    var available = true
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if path == "/api/mini/bootstrap" { return ["ok": true, "sources": []] }
        if path.contains("/music/library") { return ["ok": true, "tracks": [["id": 1, "duration": 99]], "playlists": []] }
        if path == "/api/mini/music/players" { return ["ok": true, "players": []] }
        if path == "/api/mini/music/session" {
            return ["ok": true, "session": ["track_id": 1, "device": "local", "state": "playing", "position": 37,
                "session_key": String(repeating: "z", count: 32), "revision": 7, "recovery_available": available]]
        }
        if path == "/api/mini/music/session/recover" {
            if fail { throw OwnerAPIError(status: 409, message: "Прежний плеер снова в сети") }
            return ["ok": true, "session": ["track_id": 1, "device": "local", "state": "paused", "position": 37,
                "session_key": body!["session_key"]!, "revision": 8, "recovery_available": false]]
        }
        throw OwnerAPIError.invalidResponse
    }
}

final class NativePlaybackRecoveryTests: XCTestCase {
    @MainActor func testRecoveryIsExplicitRevisionBoundAndNeverAutoplays() async throws {
        let api = RecoveryOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        await store.refresh()
        XCTAssertTrue(store.canRecoverPlayback)
        XCTAssertTrue(api.requests.allSatisfy { $0.1 == "GET" }, "Reading stale state must not take over")
        try await store.recoverStalePlayback()
        let write = try XCTUnwrap(api.requests.first { $0.0.hasSuffix("/recover") })
        XCTAssertEqual(write.2?["expected_source_key"] as? String, String(repeating: "z", count: 32))
        XCTAssertEqual(write.2?["expected_revision"] as? Int, 7)
        XCTAssertEqual(write.2?["session_key"] as? String, store.sessionKey)
        XCTAssertEqual(write.2?["confirm_stopped"] as? Bool, true)
        XCTAssertFalse(store.canRecoverPlayback)
        XCTAssertEqual(store.playbackState, "paused")
        XCTAssertEqual(store.position, 37)
        XCTAssertFalse(audio.hasPlayableItem); XCTAssertFalse(audio.canResumePlayback())
        XCTAssertFalse(store.busy)
        XCTAssertEqual(api.requests.filter { $0.1 == "POST" }.count, 1)
    }

    @MainActor func testFreshOrConflictingRecoveryDoesNotChangePlayback() async throws {
        let api = RecoveryOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        api.available = false; await store.refresh()
        try await store.recoverStalePlayback()
        XCTAssertFalse(api.requests.contains { $0.1 == "POST" })
        api.available = true; await store.refresh(); api.fail = true
        do { try await store.recoverStalePlayback(); XCTFail("Concurrent fresh player must win") }
        catch { XCTAssertEqual((error as? OwnerAPIError)?.status, 409) }
        XCTAssertFalse(store.busy)
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertTrue(store.otherLocal)
        XCTAssertNil(store.notice)
    }
}
