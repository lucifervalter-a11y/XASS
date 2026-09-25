import XCTest
@testable import XASS

final class NativeNavigationTests: XCTestCase {
    func testReplacementWaitsForDismissal() {
        var flow = NativeModalPresentation()
        flow.request(.player)
        flow.request(.routes)
        XCTAssertNil(flow.current)
        XCTAssertEqual(flow.pending, .routes)
        XCTAssertTrue(flow.isDismissing)
        XCTAssertFalse(flow.didDismiss())
        XCTAssertEqual(flow.current, .routes)
    }
    func testLatestRequestWinsWhileDismissing() {
        var flow = NativeModalPresentation()
        flow.request(.player)
        flow.request(.routes)
        flow.request(.enrollment)
        XCTAssertFalse(flow.didDismiss())
        XCTAssertEqual(flow.current, .enrollment)
    }
    func testCancelledReplacementDoesNotReopenSheet() {
        var flow = NativeModalPresentation()
        flow.request(.player)
        flow.request(.routes)
        flow.request(nil)
        XCTAssertFalse(flow.didDismiss())
        XCTAssertNil(flow.current)
    }
    func testUserDismissalClearsRequests() {
        var flow = NativeModalPresentation()
        flow.request(.enrollment)
        flow.systemDismissed()
        XCTAssertTrue(flow.didDismiss())
        XCTAssertNil(flow.current)
    }
    func testSameDestinationDoesNotRestartPresentation() {
        var flow = NativeModalPresentation()
        flow.request(.player)
        flow.request(.player)
        XCTAssertEqual(flow.current, .player)
        XCTAssertFalse(flow.isDismissing)
    }

    @MainActor func testRoutePickerPausesOriginalIPhoneBeforeHandoff() async throws {
        let api = NativeRouteFixture()
        let store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        api.session["session_key"] = store.sessionKey
        api.session["client_id"] = store.clientID
        await store.refresh()
        try await store.selectPlaybackRoute(device: "agent:Студия", output: "speakers")
        let writes = api.requests.filter { $0.1 == "POST" }
        XCTAssertEqual(writes.map { $0.0 }, ["/api/mini/music/session", "/api/mini/music/transfers"])
        XCTAssertEqual(writes.first?.2?["state"] as? String, "paused")
        XCTAssertEqual(writes.first?.2?["device"] as? String, "local")
        XCTAssertEqual(writes.last?.2?["device"] as? String, "agent:Студия")
        XCTAssertEqual(writes.last?.2?["output_id"] as? String, "speakers")
        XCTAssertEqual(writes.last?.2?["position"] as? Double, 37)
        XCTAssertEqual(store.selectedDevice, "agent:Студия")
        XCTAssertFalse(store.showRoutePicker)
    }

    @MainActor func testFailedRouteDoesNotPretendTargetWasSelected() async {
        let api = NativeRouteFixture()
        let store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        api.session["session_key"] = store.sessionKey
        api.session["client_id"] = store.clientID
        await store.refresh()
        api.failTransfer = true
        do {
            try await store.selectPlaybackRoute(device: "agent:Студия", output: "speakers")
            XCTFail("A failed handoff must throw")
        } catch {
            XCTAssertEqual(store.selectedDevice, "local")
            XCTAssertEqual(store.outputID, "default")
            XCTAssertTrue(store.showRoutePicker)
            XCTAssertFalse(store.busy)
        }
    }

    @MainActor func testIdleRouteSelectionDoesNotStartPlayback() async throws {
        let api = NativeRouteFixture()
        let audio = AudioController()
        let store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        try await store.selectPlaybackRoute(device: "agent:Студия")
        XCTAssertEqual(store.selectedDevice, "agent:Студия")
        XCTAssertTrue(api.requests.isEmpty)
        XCTAssertFalse(audio.hasPlayableItem)
    }
}

@MainActor private final class NativeRouteFixture: OwnerService {
    let origin = try! ServerOrigin("https://native-route-fixture.invalid")
    var failTransfer = false
    var requests: [(String, String, [String: Any]?)] = []
    var session: [String: Any] = [
        "track_id": 1, "device": "local", "state": "playing", "position": 37,
        "output_id": "default", "share_site": false
    ]
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if path == "/api/mini/bootstrap" { return ["sources": []] }
        if path.contains("library") {
            return ["tracks": [["id": 1, "title": "Fixture", "duration": 100]], "playlists": []]
        }
        if path == "/api/mini/music/session" { return ["session": session] }
        if path.contains("players") { return ["players": []] }
        if path == "/api/mini/music/transfers" && method == "POST" {
            if failTransfer { throw OwnerAPIError(status: 503, message: "Fixture unavailable") }
            var next = session
            next["device"] = body?["device"]
            next["output_id"] = body?["output_id"]
            return ["transfer_id": "route-test", "status": "ready", "session": next]
        }
        throw OwnerAPIError(status: 400, message: "Unexpected fixture request: " + path)
    }
}
