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
        XCTAssertEqual(store.tracks.map(\.id), [1], "First paint stays lazy")
        XCTAssertTrue(store.libraryHasMore)
        await store.loadMoreTracks()
        XCTAssertEqual(store.tracks.map(\.id), [1, 2])
        XCTAssertEqual(api.requests.filter { $0.0.contains("library") }.count, 2)
        store.disconnect()
    }
    @MainActor func testShuffleTransferUsesOneCanonicalOrderAndQueuePatchIsPartial() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        await store.refresh()
        let tracks = (1...8).compactMap { LibraryTrack(["id": $0, "title": "Track \($0)"]) }
        store.tracks = tracks; store.selectedDevice = "agent:Студия"
        api.handler = { path, method, body in
            if path == "/api/mini/music/transfers", method == "POST" {
                return ["ok": true, "transfer_id": "shuffle-transfer", "status": "ready", "session": ["track_id": body!["track_id"]!, "device": "agent:Студия", "session_key": store.sessionKey, "client_id": store.clientID, "queue": body!["queue"]!, "repeat_mode": "off"]]
            }
            return nil
        }
        try await store.playAll(tracks, shuffled: true)
        let transfer = api.requests.first { $0.0 == "/api/mini/music/transfers" }!.2!
        XCTAssertEqual(store.queue.map(\.id), transfer["queue"] as? [Int])
        XCTAssertEqual(Set(store.queue.map(\.id)), Set(1...8))
        try await store.setQueueMode(repeatMode: "all")
        let patch = api.requests.last!.2!
        XCTAssertEqual(Set(patch.keys), Set(["session_key", "queue", "repeat_mode", "takeover"]))
        XCTAssertEqual(patch["repeat_mode"] as? String, "all")
        api.handler = { _, method, _ in if method == "POST" { throw OwnerAPIError(status: 503, message: "Unavailable") }; return nil }
        do { try await store.setQueueMode(repeatMode: "one"); XCTFail("Failed patch must throw") } catch {}
        XCTAssertEqual(store.repeatMode, "all")
        store.disconnect()
    }
    @MainActor func testCanonicalPendingQueueCannotBeOverwrittenByOldPCHeartbeat() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        api.handler = { path, _, _ in
            if path.contains("players") { return ["ok": true, "players": [["source_name": "Студия", "online": true, "available": true, "music_player": ["track_id": 1, "state": "ended", "position_sec": 100, "volume": 99]]]] }
            return nil
        }
        for state in ["loading", "error", "unavailable"] {
            api.session = ["track_id": 2, "device": "agent:Студия", "state": state, "position": 0, "volume": 45, "session_key": store.sessionKey, "queue": [1, 2], "repeat_mode": "all", "detail": "Ожидаем подтверждение ПК"]
            await store.refresh()
            XCTAssertEqual(store.currentID, 2); XCTAssertEqual(store.playbackState, state)
            XCTAssertEqual(store.position, 0); XCTAssertEqual(store.volume, 45)
            XCTAssertEqual(store.error, "Ожидаем подтверждение ПК")
        }
        XCTAssertFalse(api.requests.contains { $0.1 != "GET" }, "Native controller must not auto-advance a server-owned PC queue")
        store.disconnect()
    }
    @MainActor func testRoutePickerAcknowledgesActualSourceBeforeChangingRoute() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        api.session["session_key"] = store.sessionKey; api.session["client_id"] = store.clientID
        await store.refresh()
        api.handler = { path, method, body in
            if path == "/api/mini/music/session", method == "POST" {
                XCTAssertEqual(store.selectedDevice, "local")
                XCTAssertEqual(body?["device"] as? String, "local")
                XCTAssertEqual(body?["state"] as? String, "paused")
            }
            if path == "/api/mini/music/transfers", method == "POST" {
                return ["ok": true, "transfer_id": "route-picker", "status": "ready", "session": ["track_id": 1, "device": "agent:Studio", "session_key": store.sessionKey, "state": "playing"]]
            }
            return nil
        }
        try await store.pickRoute(device: "agent:Studio")
        XCTAssertEqual(api.requests.filter { $0.1 == "POST" }.map { $0.0 }, ["/api/mini/music/session", "/api/mini/music/transfers"])
        XCTAssertEqual(store.selectedDevice, "agent:Studio")
        XCTAssertFalse(store.showRoutePicker)
    }
    @MainActor func testCancelWhileInitialTransferReturnsReadyStillCancelsServer() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        await store.refresh()
        api.handler = { path, _, _ in
            if path == "/api/mini/music/transfers" {
                store.cancelTransfer()
                return ["ok": true, "transfer_id": "cancel-ready", "status": "ready", "session": ["track_id": 1, "device": "agent:Studio", "state": "playing"]]
            }
            if path.hasSuffix("/cancel") { return ["ok": true, "status": "failed"] }
            return nil
        }
        do { try await store.pickRoute(device: "agent:Studio"); XCTFail("Cancellation must win over a ready response") }
        catch is CancellationError {} catch { XCTFail("Unexpected failure: \(error)") }
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/transfers/cancel-ready/cancel" })
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertFalse(store.busy)
        XCTAssertTrue(store.showRoutePicker)
    }
    @MainActor func testRemoteCommandCancellationUsesCommandEndpoint() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        await store.refresh()
        api.handler = { path, _, _ in
            if path == "/api/mini/music/session/control" {
                store.cancelTransfer()
                return ["ok": true, "command_id": "remote-cancel"]
            }
            if path.hasSuffix("/cancel") { return ["ok": true] }
            return nil
        }
        do { try await store.toggle(); XCTFail("Command should remain cancelled") }
        catch is CancellationError {} catch { XCTFail("Unexpected failure: \(error)") }
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/session/commands/remote-cancel/cancel" })
        XCTAssertFalse(api.requests.contains { $0.0.hasPrefix("/api/mini/music/transfers/") })
    }
    @MainActor func testRejectedLocalSessionDoesNotStartAudioOrClaimOwnership() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        await store.refresh()
        api.handler = { path, method, body in
            if path.hasSuffix("/ticket") { return ["ok": true, "path": "/api/music/tracks/1/stream?ticket=fixture"] }
            if path == "/api/mini/music/session", method == "POST" {
                XCTAssertEqual(body?["takeover"] as? Bool, true)
                XCTAssertEqual(body?["queue"] as? [Int], [1])
                XCTAssertEqual(body?["state"] as? String, "loading")
                throw OwnerAPIError(status: 503, message: "Fixture offline")
            }
            return nil
        }
        do { try await store.play(store.tracks[0]); XCTFail("Rejected reservation must throw") }
        catch let failure as OwnerAPIError { XCTAssertEqual(failure.status, 503) }
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertFalse(audio.canResumePlayback())
        XCTAssertEqual(store.playbackState, "paused")
        XCTAssertFalse(store.busy)
    }
    @MainActor func testNewSearchWinsEvenWhenOlderResponseFinishesLast() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        let started = expectation(description: "First search is in flight")
        var finishOld: CheckedContinuation<Void, Never>?
        api.handler = { path, _, _ in
            if path.contains("q=old") {
                await withCheckedContinuation { done in finishOld = done; started.fulfill() }
                return ["ok": true, "tracks": [["id": 1, "title": "Old"]]]
            }
            if path.contains("q=new") { return ["ok": true, "tracks": [["id": 2, "title": "New"]]] }
            return nil
        }
        let first = Task { await store.searchLibrary(query: "old") }
        await fulfillment(of: [started], timeout: 2)
        await store.searchLibrary(query: "new")
        finishOld?.resume(); await first.value
        XCTAssertEqual(store.tracks.map(\.id), [2])
        XCTAssertFalse(store.loading)
    }
    @MainActor func testSearchEscapesQuerySeparatorsAndPreservesCurrentTrack() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        await store.refresh()
        api.handler = { path, _, _ in
            if path.contains("library") { return ["ok": true, "tracks": [["id": 2, "title": "Other"]]] }
            return nil
        }
        let query = "AC+DC &favorite=true#live"
        await store.searchLibrary(query: query)
        let path = api.requests.last!.0, parts = URLComponents(string: path)!
        XCTAssertEqual(parts.queryItems?.first(where: { $0.name == "q" })?.value, query)
        XCTAssertNil(parts.queryItems?.first(where: { $0.name == "favorite" }))
        XCTAssertTrue(path.contains("%2B"))
        XCTAssertEqual(store.currentTrack?.id, 1, "Filtering must not remove the mini-player's metadata")
    }
    @MainActor func testPlaylistFetchesEveryPageWithoutReplacingLibraryFilter() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        await store.refresh()
        api.handler = { path, _, _ in
            if path.contains("playlist=9") {
                let second = path.contains("offset=1")
                return ["ok": true, "tracks": [["id": second ? 3 : 2, "title": "Playlist track"]], "has_more": !second, "next_offset": 1]
            }
            return nil
        }
        let playlist = LibraryPlaylist(["id": 9, "name": "Saved", "track_ids": [2, 3]])!
        let result = try await store.playlistTracks(playlist)
        XCTAssertEqual(result.map(\.id), [2, 3])
        XCTAssertEqual(store.tracks.map(\.id), [1])
        XCTAssertEqual(store.rows(filter: "all", query: "", playlist: playlist).map(\.id), [2, 3])
    }

    @MainActor func testReopenedPCSessionUsesHandoffWithoutPublishingFalseLocalPause() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        api.session["session_key"] = store.sessionKey; api.session["client_id"] = store.clientID
        api.session["device"] = "agent:Studio"; api.session["state"] = "playing"
        await store.refresh()
        XCTAssertFalse(store.otherLocal, "A canonical PC session must not be mistaken for a foreign iPhone")
        api.handler = { path, method, body in
            if path.hasSuffix("/ticket") { return ["ok": true, "path": "/api/music/tracks/1/stream?ticket=fixture"] }
            if path == "/api/mini/music/session", method == "POST" {
                XCTAssertEqual(body?["state"] as? String, "loading", "Only the new local reservation may be published")
                throw OwnerAPIError(status: 409, message: "Handoff required", detail: ["code": "transfer_required"])
            }
            if path == "/api/mini/music/transfers" {
                store.cancelTransfer()
                return ["ok": true, "transfer_id": "from-pc", "status": "ready"]
            }
            if path.hasSuffix("/cancel") { return ["ok": true] }
            return nil
        }
        do { try await store.play(store.tracks[0]); XCTFail("Fixture cancels the handoff") }
        catch is CancellationError {} catch { XCTFail("Unexpected failure: \(error)") }
        XCTAssertEqual(api.requests.filter { $0.0 == "/api/mini/music/session" && $0.1 == "POST" }.count, 1)
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/transfers" })
        XCTAssertFalse(audio.hasPlayableItem)
    }

}
