import XCTest
@testable import XASS

@MainActor private final class NativeOwnerFixture: OwnerService {
    let origin = try! ServerOrigin("https://native-store-fixture.invalid")
    var requests: [(String, String, [String: Any]?)] = []
    var session: [String: Any] = ["track_id": 1, "device": "local", "client_id": "other-phone", "session_key": String(repeating: "b", count: 32), "state": "paused", "position": 37, "share_site": false]
    var handler: ((String, String, [String: Any]?) async throws -> [String: Any]?)?
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if let result = try await handler?(path, method, body) { return result }
        if path == "/api/mini/bootstrap" { return ["ok": true, "sources": []] }
        if path.contains("library") { return ["ok": true, "tracks": [["id": 1, "title": "Silent fixture", "duration": 100]], "playlists": []] }
        if path == "/api/mini/music/session" { return ["ok": true, "session": session] }
        if path.contains("players") { return ["ok": true, "players": []] }
        throw OwnerAPIError(status: 400, message: "Unexpected fixture request: " + path)
    }
}

final class NativeStoreTests: XCTestCase {
    @MainActor func testForeignPhoneIsNotThisPhoneAndCannotResumeControlCenter() async {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        await store.refresh()
        XCTAssertTrue(store.otherLocal)
        XCTAssertEqual(store.deviceLabel, "Другое устройство")
        XCTAssertFalse(audio.canResumePlayback())
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertFalse(api.requests.contains { $0.1 != "GET" })
        store.disconnect()
    }

    @MainActor func testReopenedOwnPhoneConfirmsPauseBeforeAgentTransfer() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        api.session["session_key"] = store.sessionKey; api.session["client_id"] = store.clientID; api.session["state"] = "playing"
        api.handler = { path, method, body in
            if path == "/api/mini/music/transfers", method == "POST" {
                XCTAssertEqual(body?["position"] as? Double, 37)
                return ["ok": true, "transfer_id": "test-transfer", "status": "ready", "session": ["track_id": 1, "device": "agent:Студия", "position": 37, "state": "playing", "session_key": store.sessionKey, "client_id": store.clientID]]
            }
            return nil
        }
        await store.refresh(); try await store.transfer(to: "agent:Студия")
        let writes = api.requests.filter { $0.1 == "POST" }
        XCTAssertEqual(writes.map { $0.0 }, ["/api/mini/music/session", "/api/mini/music/transfers"])
        XCTAssertEqual(writes.first?.2?["state"] as? String, "paused")
        XCTAssertEqual(store.selectedDevice, "agent:Студия")
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertFalse(audio.canResumePlayback())
        store.disconnect()
    }

    @MainActor func testFailedSharingDoesNotClaimPublishedAndRapidToggleIsBounded() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        await store.refresh()
        api.handler = { path, method, _ in
            if path == "/api/mini/music/session", method == "POST" {
                try await Task.sleep(for: .milliseconds(30))
                throw OwnerAPIError(status: 503, message: "Fixture unavailable")
            }
            return nil
        }
        let first = Task { try await store.setSharing(true) }
        await Task.yield()
        _ = try? await store.setSharing(false)
        _ = try? await first.value
        XCTAssertFalse(store.shareSite); XCTAssertFalse(store.shareSaving)
        XCTAssertEqual(api.requests.filter { $0.1 == "POST" }.count, 1)
        store.disconnect()
    }

    @MainActor func testLibraryLoadsFollowingPagesAndPreservesOrder() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        api.handler = { path, _, _ in
            if path.contains("library") {
                let second = path.contains("offset=1")
                return ["ok": true, "tracks": [["id": second ? 2 : 1, "title": second ? "Second" : "First"]], "has_more": !second, "next_offset": 1, "playlists": []]
            }
            return nil
        }
        await store.refresh()
        XCTAssertEqual(store.tracks.map(\.id), [1, 2])
        XCTAssertEqual(api.requests.filter { $0.0.contains("library") }.count, 2)
        store.disconnect()
    }
}
