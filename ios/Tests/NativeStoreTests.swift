import XCTest
import Combine
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
    @MainActor func testPlaybackTicksDoNotRepublishTheLibrary() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        await store.refresh()
        var clockChanges = 0
        var libraryChanges = 0
        let clockSubscription = store.playback.objectWillChange.sink { _ in clockChanges += 1 }
        let librarySubscription = store.objectWillChange.sink { _ in libraryChanges += 1 }
        store.position = 12
        store.duration = 80
        XCTAssertEqual(store.position, 12)
        XCTAssertEqual(store.duration, 80)
        try await store.refreshSession()
        XCTAssertEqual(store.position, 37, "A session poll still records the reported position")
        XCTAssertGreaterThan(clockChanges, 0)
        XCTAssertEqual(libraryChanges, 0, "A playback tick must not republish the library")
        clockSubscription.cancel()
        librarySubscription.cancel()
    }
    @MainActor func testTransientSessionFailureRecoversWithoutDiscardingLibraryOrOtherActionError() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        api.handler = { path, _, _ in
            if path == "/api/mini/music/players" { throw OwnerAPIError(status: 503, message: "Список устройств временно недоступен") }
            return nil
        }
        await store.refresh()
        XCTAssertEqual(store.tracks.count, 1)
        XCTAssertTrue(store.authorized)
        XCTAssertEqual(store.error, "Список устройств временно недоступен")
        api.handler = nil
        try await store.refreshSession()
        XCTAssertNil(store.error, "A recovered refresh must clear its own stale error")
        store.error = "Загрузка файла не выполнена"
        try await store.refreshSession()
        XCTAssertEqual(store.error, "Загрузка файла не выполнена", "Polling must not erase unrelated action failures")
    }
    @MainActor func testFavoriteUsesLatestTrackRatherThanStaleAlbumSnapshot() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        let stale = try XCTUnwrap(LibraryTrack(["id": 15, "title": "Album track", "favorite": false]))
        api.handler = { path, method, body in
            if path == "/api/mini/music/tracks/15", method == "PATCH" {
                return ["ok": true, "track": ["id": 15, "title": "Album track", "favorite": body!["favorite"]!]]
            }
            return nil
        }
        try await store.favorite(stale)
        XCTAssertTrue(store.resolvedTrack(stale).favorite)
        try await store.favorite(stale)
        XCTAssertFalse(store.resolvedTrack(stale).favorite)
    }
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
    @MainActor func testForegroundRefreshReconcilesEveryLoadedPageAndDeletedTail() async {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        var ids = Array(1...120)
        api.handler = { path, _, _ in
            guard path.contains("library"), let parts = URLComponents(string: path) else { return nil }
            let items = parts.queryItems ?? []
            let offset = Int(items.first(where: { $0.name == "offset" })?.value ?? "0") ?? 0
            let limit = Int(items.first(where: { $0.name == "limit" })?.value ?? "50") ?? 50
            let page = Array(ids.dropFirst(offset).prefix(limit))
            let hasMore = offset + page.count < ids.count
            var response: [String: Any] = ["ok": true, "tracks": page.map { ["id": $0, "title": "Track \($0)"] },
                                                   "has_more": hasMore, "playlists": []]
            if hasMore { response["next_offset"] = offset + page.count }
            else { response["next_offset"] = NSNull() }
            return response
        }
        await store.refresh()
        await store.loadMoreTracks()
        await store.loadMoreTracks()
        XCTAssertEqual(store.tracks.map(\.id), Array(1...120))

        ids.removeAll { $0 == 110 }
        await store.refresh()
        XCTAssertEqual(store.tracks.map(\.id), ids)
        XCTAssertFalse(store.libraryHasMore)
        XCTAssertTrue(api.requests.contains { $0.0.contains("limit=120") },
                      "Refresh must request the full loaded range, not only page one")
    }
    @MainActor func testPlayerHeartbeatExplainsLocalNetworkTransport() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        api.handler = { path, _, _ in
            if path.contains("players") {
                return ["ok": true, "players": [["source_name": "Studio", "live": true, "available": true,
                    "music_player": ["track_id": 1, "state": "playing", "media_transport": "lan"]]]]
            }
            return nil
        }
        await store.refresh()
        XCTAssertEqual(store.pcTransportLabel("Studio"), "Напрямую по локальной сети")
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

    @MainActor func testReadyTransferWithAnotherSessionKeyCannotClaimAudio() async throws {
        try await assertMismatchedReadyReceipt(field: "session_key", value: String(repeating: "f", count: 32))
    }

    @MainActor func testReadyTransferWithAnotherDeviceCannotClaimAudio() async throws {
        try await assertMismatchedReadyReceipt(field: "device", value: "agent:Other")
    }

    @MainActor func testPolledReadyTransferWithAnotherTrackCannotClaimAudio() async throws {
        try await assertMismatchedReadyReceipt(field: "track_id", value: 999, afterWaiting: true)
    }

    @MainActor private func assertMismatchedReadyReceipt(field: String, value: Any, afterWaiting: Bool = false) async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        await store.refresh()
        let id = String(repeating: "d", count: 32)
        let path = "/api/mini/music/transfers/" + id
        var ready: [String: Any] = [:]
        api.handler = { requestPath, method, body in
            if requestPath == "/api/mini/music/transfers", method == "POST" {
                var session: [String: Any] = ["session_key": body!["session_key"]!, "device": body!["device"]!,
                    "track_id": body!["track_id"]!, "state": "playing", "position": 81]
                session[field] = value
                ready = ["ok": true, "transfer_id": id, "status": "ready", "session": session]
                return afterWaiting ? ["ok": true, "transfer_id": id, "status": "waiting"] : ready
            }
            if requestPath == path, method == "GET" { return ready }
            if requestPath == path + "/cancel", method == "POST" {
                return ["ok": true, "transfer_id": id, "status": "failed", "detail": "Fixture cleanup"]
            }
            return nil
        }
        do { try await store.pickRoute(device: "local"); XCTFail("A replaced lease must not be accepted") }
        catch let failure as OwnerAPIError {
            XCTAssertEqual(failure.status, 409)
            XCTAssertEqual(failure.message, "Управление изменилось во время переключения. Повторите воспроизведение.")
        }
        XCTAssertEqual(api.requests.filter { $0.0 == path + "/cancel" && $0.1 == "POST" }.count, 1)
        XCTAssertFalse(api.requests.contains { $0.0.hasSuffix("/ticket") }, "Reject the receipt before fetching audio")
        XCTAssertFalse(api.requests.contains { $0.0 == "/api/mini/music/session" && $0.1 == "POST" })
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertFalse(audio.canResumePlayback())
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertEqual(store.currentID, 1)
        XCTAssertEqual(store.position, 37, "Do not apply the foreign receipt's position")
        XCTAssertEqual(store.playbackState, "error")
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
        let claims = api.requests.filter { $0.0 == "/api/mini/music/session" && $0.1 == "POST" }
        XCTAssertEqual(claims.count, 2, "A local pick first retries as an explicit forced claim")
        XCTAssertEqual(claims.last?.2?["force"] as? Bool, true)
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/transfers" }, "Servers without force support still use the handoff")
        XCTAssertFalse(audio.hasPlayableItem)
    }

    @MainActor func testPhonePickOverStalePCLeaseTakesOverWithoutWaitingForHandoff() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        api.session["device"] = "agent:Studio"; api.session["state"] = "loading"
        await store.refresh()
        api.handler = { path, method, body in
            if path.hasSuffix("/ticket") { return ["ok": true, "path": "/api/music/tracks/1/stream?ticket=fixture"] }
            if path == "/api/mini/music/session", method == "POST" {
                guard body?["force"] as? Bool == true else {
                    throw OwnerAPIError(status: 409, message: "Handoff required", detail: ["code": "transfer_required"])
                }
                return ["ok": true, "session": ["track_id": 1, "device": "local", "state": "loading", "session_key": store.sessionKey, "client_id": store.clientID]]
            }
            if path.hasPrefix("/api/mini/music/transfers") { XCTFail("A forced local claim must not wait for the PC"); return ["ok": true] }
            return nil
        }
        try await store.play(store.tracks[0])
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertNil(store.pcRemoteSource)
    }

    @MainActor func testPCOwnedSessionIsControlledRemotelyNotPulledToPhone() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        api.session["device"] = "agent:Studio"; api.session["state"] = "playing"
        await store.refresh()
        XCTAssertEqual(store.pcRemoteSource, "agent:Studio")
        XCTAssertEqual(store.playingDeviceName, "Studio")
        api.handler = { path, method, body in
            if path == "/api/mini/music/control", method == "POST" {
                XCTAssertEqual(body?["source_name"] as? String, "Studio")
                XCTAssertEqual(body?["action"] as? String, "pause")
                return ["ok": true, "command_id": 5, "status": "pending"]
            }
            if path == "/api/mini/music/control/5" { return ["ok": true, "status": "completed", "result": ["ok": true, "details": ["state": "paused"]]] }
            return nil
        }
        try await store.toggle()
        XCTAssertFalse(api.requests.contains { $0.0.hasPrefix("/api/mini/music/transfers") })
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/control" })
    }

    // MARK: P0 — the iPhone plays by itself when no PC is online.

    private static func studio(live: Bool, state: String = "playing", position: Double = 40) -> [String: Any] {
        ["source_name": "Studio", "online": true, "live": live, "available": live,
         "music_player": ["track_id": 1, "state": state, "position_sec": position, "volume": 50]]
    }

    /// Local claim handler: ticket + accepted local session, no PC handoff allowed.
    @MainActor private static func phoneHandler(_ api: NativeOwnerFixture, _ store: NativeStore, players: @escaping () -> [[String: Any]]) {
        api.handler = { path, method, body in
            if path.hasSuffix("/ticket") { return ["ok": true, "path": "/api/music/tracks/1/stream?ticket=fixture"] }
            if path == "/api/mini/music/session", method == "POST" {
                return ["ok": true, "session": ["track_id": 1, "device": "local", "state": "loading",
                    "position": body?["position"] ?? 0, "session_key": store.sessionKey, "client_id": store.clientID]]
            }
            if path == "/api/mini/music/players" { return ["ok": true, "players": players()] }
            if path.hasPrefix("/api/mini/music/transfers") || path.hasPrefix("/api/mini/music/control") {
                XCTFail("An offline PC must not be asked to play: \(path)"); throw OwnerAPIError(status: 409, message: "offline")
            }
            return nil
        }
    }

    @MainActor func testAllPCsOfflinePlayStartsOnIPhoneByDefault() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        Self.phoneHandler(api, store) { [Self.studio(live: false)] }
        await store.refresh()
        XCTAssertEqual(store.selectedDevice, "local", "The iPhone is the default output")
        XCTAssertFalse(store.pcIsLive("agent:Studio"))
        let started = Date()
        try await store.play(store.tracks[0])
        XCTAssertLessThan(Date().timeIntervalSince(started), 2)
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertNil(store.error)
    }

    @MainActor func testChosenOfflinePCFallsBackToIPhoneWithToast() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        Self.phoneHandler(api, store) { [Self.studio(live: false)] }
        await store.refresh()
        store.selectedDevice = "agent:Studio"
        try await store.play(store.tracks[0])
        XCTAssertTrue(audio.hasPlayableItem, "No silence: the iPhone plays")
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertEqual(store.notice, NativeStore.pcOfflineNotice)
        XCTAssertNil(store.error, "An offline PC is not an error")
    }

    @MainActor func testSilentPCIsAbandonedWithinTwoSeconds() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        var hang = false
        Self.phoneHandler(api, store) { [Self.studio(live: true)] }
        let base = api.handler
        api.handler = { path, method, body in
            if hang, path == "/api/mini/music/players" { try await Task.sleep(nanoseconds: 5_000_000_000); return ["ok": true, "players": []] }
            return try await base?(path, method, body)
        }
        await store.refresh()
        store.selectedDevice = "agent:Studio"
        store.pcResponseDeadline = 0.4
        hang = true
        let started = Date()
        try await store.play(store.tracks[0])
        XCTAssertLessThan(Date().timeIntervalSince(started), 2, "Fallback must happen within 2 s")
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(store.notice, NativeStore.pcOfflineNotice)
    }

    @MainActor func testFailedPCHandoffFallsBackToIPhone() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        Self.phoneHandler(api, store) { [Self.studio(live: true)] }
        let base = api.handler
        api.handler = { path, method, body in
            if path == "/api/mini/music/transfers" { throw OwnerAPIError(status: 409, message: "ПК недоступен для музыки. Проверьте подключение и версию агента") }
            return try await base?(path, method, body)
        }
        await store.refresh()
        store.selectedDevice = "agent:Studio"
        try await store.play(store.tracks[0])
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/transfers" })
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertEqual(store.notice, NativeStore.pcOfflineNotice)
        XCTAssertNil(store.error)
    }

    @MainActor func testPCOfflineMidTrackContinuesOnIPhoneFromCurrentPosition() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        var live = true
        api.session["device"] = "agent:Studio"; api.session["state"] = "playing"; api.session["position"] = 40
        api.session["session_key"] = store.sessionKey; api.session["client_id"] = store.clientID
        Self.phoneHandler(api, store) { [Self.studio(live: live)] }
        await store.refresh()
        XCTAssertEqual(store.pcRemoteSource, "agent:Studio", "Online PC stays a remote output")
        // PC switched off: server lease keeps the stale start position.
        live = false
        api.session["state"] = "unavailable"; api.session["position"] = 0; api.session["detail"] = "Компьютер не в сети"
        try await store.refreshSession()
        let claim = api.requests.last { $0.0 == "/api/mini/music/session" && $0.1 == "POST" }
        let resumed = claim?.2?["position"] as? Double ?? -1
        XCTAssertGreaterThanOrEqual(resumed, 40, "Continue from where the PC was, not from 0")
        XCTAssertLessThan(resumed, 75)
        XCTAssertEqual(claim?.2?["device"] as? String, "local")
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(store.notice, NativeStore.pcOfflineNotice)
        XCTAssertNil(store.error)
    }

    @MainActor func testPausedPCGoingOfflineDoesNotStartAudio() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        var live = true
        api.session["device"] = "agent:Studio"; api.session["state"] = "paused"; api.session["position"] = 40
        Self.phoneHandler(api, store) { [Self.studio(live: live, state: "paused")] }
        await store.refresh()
        live = false; api.session["state"] = "unavailable"; api.session["detail"] = "Компьютер не в сети"
        try await store.refreshSession()
        XCTAssertFalse(api.requests.contains { $0.0 == "/api/mini/music/session" && $0.1 == "POST" })
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertNil(store.error, "Offline PC is shown without an error")
        XCTAssertNil(store.pcRemoteSource)
        // Pressing play now starts the iPhone from the PC position.
        try await store.toggle()
        XCTAssertTrue(audio.hasPlayableItem)
        let claim = api.requests.last { $0.0 == "/api/mini/music/session" && $0.1 == "POST" }
        XCTAssertEqual(claim?.2?["position"] as? Double, 40)
        XCTAssertEqual(store.notice, NativeStore.pcOfflineNotice)
    }

    @MainActor func testOnlinePCResumeStaysRemote() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        api.session["device"] = "agent:Studio"; api.session["state"] = "paused"
        await store.refresh()
        api.handler = { path, method, body in
            if path == "/api/mini/music/players" { return ["ok": true, "players": [Self.studio(live: true, state: "paused")]] }
            if path == "/api/mini/music/control", method == "POST" {
                XCTAssertEqual(body?["action"] as? String, "resume")
                XCTAssertNotNil(body?["expires_at"], "A late resume must expire on the PC")
                return ["ok": true, "command_id": 7, "status": "pending"]
            }
            if path == "/api/mini/music/control/7" { return ["ok": true, "status": "completed", "result": ["ok": true, "details": ["state": "playing"]]] }
            if path == "/api/mini/music/session", method == "POST" { XCTFail("Online PC must not be pulled to the phone") }
            return nil
        }
        try await store.refreshSession()
        XCTAssertEqual(store.pcRemoteSource, "agent:Studio")
        try await store.toggle()
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/control" })
        XCTAssertFalse(audio.hasPlayableItem)
        XCTAssertNil(store.notice)
    }

    @MainActor private func waitUntil(_ condition: @MainActor () -> Bool) async {
        for _ in 0..<80 where !condition() { try? await Task.sleep(nanoseconds: 50_000_000) }
    }

    @MainActor func testWaitingForPCResumeOffersPlayOnIPhoneFromCurrentPosition() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        api.session["device"] = "agent:Studio"; api.session["state"] = "paused"; api.session["position"] = 40
        Self.phoneHandler(api, store) { [Self.studio(live: true, state: "paused")] }
        let base = api.handler
        var pcActions: [String] = []
        api.handler = { path, method, body in
            if path == "/api/mini/music/control", method == "POST" {
                pcActions.append(body?["action"] as? String ?? "")
                return ["ok": true, "command_id": 7, "status": "pending"]
            }
            if path == "/api/mini/music/control/7" { return ["ok": true, "status": "pending"] }
            return try await base?(path, method, body)
        }
        await store.refresh()
        XCTAssertEqual(store.pcRemoteSource, "agent:Studio")
        let resume = Task { try await store.toggle() }
        await waitUntil { store.pcConnecting != nil }
        XCTAssertEqual(store.pcConnecting, "Studio")
        XCTAssertEqual(store.transferStatus, NativeStore.pcConnectingText, "Mini player shows «Подключаемся к ПК…»")
        XCTAssertFalse(audio.hasPlayableItem)
        let tapped = Date()
        store.playHereInsteadOfPC()
        try await resume.value
        XCTAssertLessThan(Date().timeIntervalSince(tapped), 2)
        XCTAssertTrue(audio.hasPlayableItem, "«Играть на iPhone» switches immediately")
        let claim = api.requests.last { $0.0 == "/api/mini/music/session" && $0.1 == "POST" }
        XCTAssertEqual(claim?.2?["position"] as? Double, 40, "From the current position")
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertNil(store.pcConnecting)
        XCTAssertNil(store.transferStatus)
        XCTAssertEqual(store.notice, NativeStore.playingHereNotice)
        await waitUntil { pcActions.contains("pause") }
        XCTAssertEqual(pcActions, ["resume", "pause"], "A late PC resume is paused again")
    }

    @MainActor func testWaitingForPCHandoffOffersPlayOnIPhone() async throws {
        let api = NativeOwnerFixture(), audio = AudioController(), store = NativeStore(api: api, audio: audio)
        defer { store.disconnect() }
        Self.phoneHandler(api, store) { [Self.studio(live: true, state: "stopped")] }
        let base = api.handler
        api.handler = { path, method, body in
            if path == "/api/mini/music/transfers" { return ["ok": true, "transfer_id": "to-pc", "status": "waiting"] }
            if path == "/api/mini/music/transfers/to-pc" { return ["ok": true, "transfer_id": "to-pc", "status": "waiting"] }
            if path == "/api/mini/music/transfers/to-pc/cancel" { return ["ok": true, "transfer_id": "to-pc", "status": "failed"] }
            return try await base?(path, method, body)
        }
        await store.refresh()
        store.selectedDevice = "agent:Studio"
        let play = Task { try await store.play(store.tracks[0]) }
        await waitUntil { store.transferStatus == NativeStore.pcConnectingText }
        XCTAssertEqual(store.pcConnecting, "Studio")
        store.playHereInsteadOfPC()
        try await play.value
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/transfers/to-pc/cancel" }, "The PC handoff is cancelled on the server")
        XCTAssertTrue(audio.hasPlayableItem)
        XCTAssertEqual(store.selectedDevice, "local")
        XCTAssertNil(store.pcConnecting)
        XCTAssertEqual(store.notice, NativeStore.playingHereNotice)
    }

    @MainActor func testFavoriteUpdatesHiddenCurrentTrackAndDeletePrunesPlaylists() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        await store.refresh()
        let original = store.tracks[0]
        store.playlists = [LibraryPlaylist(["id": 1, "name": "Saved", "track_ids": [1, 2]])!]
        api.handler = { path, method, _ in
            if path.contains("library") { return ["ok": true, "tracks": [["id": 2, "title": "Other"]]] }
            if method == "PATCH" { return ["ok": true, "track": ["id": 1, "title": "Silent fixture", "favorite": true]] }
            if method == "DELETE" { return ["ok": true] }
            return nil
        }
        await store.searchLibrary(query: "Other")
        try await store.favorite(original)
        XCTAssertEqual(store.currentTrack?.favorite, true)
        try await store.deleteTrack(original)
        XCTAssertEqual(store.playlists.first?.trackIDs, [2])
        XCTAssertNil(store.currentTrack)
    }
    @MainActor func testCancelledTaskStillSendsTransferCleanup() async throws {
        let api = NativeOwnerFixture(), store = NativeStore(api: api, audio: AudioController())
        defer { store.disconnect() }
        await store.refresh()
        let posted = expectation(description: "Transfer POST started")
        var returnReceipt: CheckedContinuation<Void, Never>?
        var cleanupCompleted = false
        api.handler = { path, _, _ in
            if path == "/api/mini/music/transfers" {
                await withCheckedContinuation { done in returnReceipt = done; posted.fulfill() }
                return ["ok": true, "transfer_id": "task-cancelled", "status": "ready"]
            }
            if path.hasSuffix("/cancel") {
                try Task.checkCancellation()
                cleanupCompleted = true
                return ["ok": true]
            }
            return nil
        }
        let transfer = Task { try await store.transfer(to: "agent:Studio") }
        await fulfillment(of: [posted], timeout: 2)
        transfer.cancel(); returnReceipt?.resume()
        do { try await transfer.value; XCTFail("Cancelled task must not play") } catch is CancellationError {}
        XCTAssertTrue(api.requests.contains { $0.0 == "/api/mini/music/transfers/task-cancelled/cancel" })
        XCTAssertTrue(cleanupCompleted, "Cancellation cleanup must execute in a fresh, uncancelled task")
    }

}
