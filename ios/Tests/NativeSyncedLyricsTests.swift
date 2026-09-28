import XCTest
@testable import XASS

@MainActor private final class SyncedLyricsFixture: OwnerService {
    let origin = try! ServerOrigin("https://timed-lyrics-fixture.invalid")
    var calls = 0
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        guard path == "/api/mini/music/tracks/4/timed-lyrics" else { throw OwnerAPIError.invalidResponse }
        calls += 1
        return ["ok": true, "lyrics": ["track_id": 4, "status": "synced", "synced": true, "source": "lrclib", "text": "A\nB",
            "lines": [["start": 12.0, "end": 15.5, "text": "B"], ["start": 1.0, "end": 4.0, "text": "A"], ["start": true, "end": 2, "text": "bad"]]]]
    }
}


/// Serves just enough of the owner API for NativeStore.refresh() plus synced lyrics for track 1.
@MainActor private final class PlayerLyricsFixture: OwnerService {
    let origin = try! ServerOrigin("https://player-lyrics-fixture.invalid")
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        if path == "/api/mini/bootstrap" { return ["ok": true, "sources": []] }
        if path.contains("library") { return ["ok": true, "tracks": [["id": 1, "title": "Silent fixture", "duration": 100]], "playlists": []] }
        if path == "/api/mini/music/session" {
            return ["ok": true, "session": ["track_id": 1, "device": "local", "client_id": "other-phone",
                "session_key": String(repeating: "b", count: 32), "state": "paused", "position": 37, "share_site": false]]
        }
        if path.contains("players") { return ["ok": true, "players": []] }
        if path == "/api/mini/music/tracks/1/timed-lyrics" {
            return ["ok": true, "lyrics": ["track_id": 1, "status": "synced", "synced": true, "source": "owner", "text": "A\nB\nC",
                "lines": [["start": 0, "end": 10, "text": "A"], ["start": 30, "end": 40, "text": "B"], ["start": 50, "end": 60, "text": "C"]]]]
        }
        throw OwnerAPIError(status: 400, message: "Unexpected fixture request: " + path)
    }
}

final class NativeSyncedLyricsTests: XCTestCase {
    func testParsingSortsLinesAndFindsActiveLineWithGaps() {
        let value = SyncedLyrics(response: ["lyrics": ["status": "synced", "synced": true, "source": "lrclib",
            "lines": [["start": 12.0, "end": 15.5, "text": "B"], ["start": 1.0, "end": 4.0, "text": "A"]]]], trackID: 4)!
        XCTAssertEqual(value.lines.map(\.text), ["A", "B"])
        XCTAssertNil(value.lineIndex(at: 0.5))
        XCTAssertEqual(value.lineIndex(at: 1.0), 0)
        XCTAssertEqual(value.lineIndex(at: 3.9), 0)
        XCTAssertNil(value.lineIndex(at: 8), "Instrumental gap highlights nothing")
        XCTAssertEqual(value.lineIndex(at: 13), 1)
        XCTAssertNil(value.lineIndex(at: 20))
        XCTAssertNil(value.lineIndex(at: .nan))
    }

    @MainActor func testStoreCachesOnDiskAndPublishesActiveLine() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("timed-lyrics-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let api = SyncedLyricsFixture()
        let store = SyncedLyricsStore(namespace: "fixture", root: root)
        store.attach(api)
        store.trackChanged(to: 4, next: nil)
        let loaded = await store.load(4)
        XCTAssertEqual(loaded?.lines.count, 2, "Boolean timestamps are rejected")
        XCTAssertEqual(store.current?.trackID, 4)
        store.tick(position: 13, trackID: 4)
        XCTAssertEqual(store.currentLyricIndex, 1)
        store.tick(position: 13, trackID: 5)
        XCTAssertNil(store.currentLyricIndex, "Another track never highlights these lines")
        let second = SyncedLyricsStore(namespace: "fixture", root: root)
        XCTAssertEqual(second.cached(4)?.lines.map(\.text), ["A", "B"], "Offline playback reads the disk cache")
        XCTAssertEqual(api.calls, 1)
    }

    func testMappingToTimedLyricsKeepsOnlySyncedLinesOfTheCurrentTrack() {
        let synced = SyncedLyrics(trackID: 7, status: "synced", synced: true, source: "lrclib",
            lines: [SyncedLyricLine(id: 0, start: 1, end: 4, text: "A"), SyncedLyricLine(id: 1, start: 12, end: 15.5, text: "B")], text: "A\nB")
        let mapped = NativeStorePlayerState.timedLyrics(from: synced, currentID: 7)
        XCTAssertEqual(mapped, TimedLyrics(lines: [TimedLyricLine(start: 1, end: 4, text: "A"), TimedLyricLine(start: 12, end: 15.5, text: "B")]))
        XCTAssertEqual(mapped?.index(at: 13), 1)
        XCTAssertNil(NativeStorePlayerState.timedLyrics(from: synced, currentID: 8), "Lyrics of another track never reach Now Playing")
        XCTAssertNil(NativeStorePlayerState.timedLyrics(from: synced, currentID: nil))
        XCTAssertNil(NativeStorePlayerState.timedLyrics(from: nil, currentID: 7))
        let plain = SyncedLyrics(trackID: 7, status: "plain", synced: false, source: "lrclib", lines: [], text: "A\nB")
        XCTAssertNil(NativeStorePlayerState.timedLyrics(from: plain, currentID: 7), "Plain text falls back to the legacy lyrics view")
    }

    @MainActor func testStorePlayerStatePublishesTimedLyricsAndActiveLine() async throws {
        let store = NativeStore(api: PlayerLyricsFixture(), audio: AudioController())
        defer { store.disconnect() }
        let player = NativeStorePlayerState(store: store)
        XCTAssertNil(player.lyrics)
        await store.refresh()
        XCTAssertEqual(store.currentID, 1)
        await store.lyrics.load(1)
        for _ in 0..<100 where player.lyrics == nil || player.currentLyricIndex == nil {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertEqual(player.lyrics?.lines.map(\.text), ["A", "B", "C"])
        XCTAssertEqual(player.currentLyricIndex, 1, "Paused at 37 s is inside the second line")
    }
}
