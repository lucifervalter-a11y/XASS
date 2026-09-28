import XCTest
@testable import XASS

@MainActor private final class TimedLyricsFixture: OwnerService {
    let origin = try! ServerOrigin("https://timed-lyrics-fixture.invalid")
    var calls = 0
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        guard path == "/api/mini/music/tracks/4/timed-lyrics" else { throw OwnerAPIError.invalidResponse }
        calls += 1
        return ["ok": true, "lyrics": ["track_id": 4, "status": "synced", "synced": true, "source": "lrclib", "text": "A\nB",
            "lines": [["start": 12.0, "end": 15.5, "text": "B"], ["start": 1.0, "end": 4.0, "text": "A"], ["start": true, "end": 2, "text": "bad"]]]]
    }
}

final class NativeTimedLyricsTests: XCTestCase {
    func testParsingSortsLinesAndFindsActiveLineWithGaps() {
        let value = TimedLyrics(response: ["lyrics": ["status": "synced", "synced": true, "source": "lrclib",
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
        let api = TimedLyricsFixture()
        let store = TimedLyricsStore(namespace: "fixture", root: root)
        store.attach(api)
        store.trackChanged(to: 4, next: nil)
        let loaded = await store.load(4)
        XCTAssertEqual(loaded?.lines.count, 2, "Boolean timestamps are rejected")
        XCTAssertEqual(store.current?.trackID, 4)
        store.tick(position: 13, trackID: 4)
        XCTAssertEqual(store.activeLineIndex, 1)
        store.tick(position: 13, trackID: 5)
        XCTAssertNil(store.activeLineIndex, "Another track never highlights these lines")
        let second = TimedLyricsStore(namespace: "fixture", root: root)
        XCTAssertEqual(second.cached(4)?.lines.map(\.text), ["A", "B"], "Offline playback reads the disk cache")
        XCTAssertEqual(api.calls, 1)
    }
}
