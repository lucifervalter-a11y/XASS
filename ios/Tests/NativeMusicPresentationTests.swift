import XCTest
import UIKit
@testable import XASS

@MainActor private final class CollectionServiceFixture: OwnerService {
    let origin = try! ServerOrigin("https://collections-fixture.invalid")
    var paths: [String] = []
    var invalidNextOffset = false
    func request(_ path: String, method: String, body: [String: Any]?) async throws -> [String: Any] {
        XCTAssertEqual(method, "GET"); XCTAssertNil(body)
        paths.append(path)
        let next = path.contains("offset=2")
        if next { return ["ok": true, "tracks": [["id": 2, "title": "duplicate"], ["id": 3, "title": "Third", "album": "Album", "artist": "Artist"]], "has_more": false] }
        return ["ok": true, "tracks": [["id": 1, "title": "First", "album": "Album", "artist": "Artist"], ["id": 2, "title": "Second"]], "has_more": true, "next_offset": invalidNextOffset ? 0 : 2]
    }
}

final class NativeMusicPresentationTests: XCTestCase {
    private func track(_ id: Int, album: String = "", artist: String = "") -> LibraryTrack {
        LibraryTrack(["id": id, "title": "Track \(id)", "album": album, "artist": artist, "duration": 90])!
    }
    func testAlbumsUseRealMetadataWithoutMergingDifferentArtistsOrInventingUnknownAlbum() {
        let a = track(1, album: " Night ", artist: "One")
        let groups = MusicCollection.groups([a, track(2, album: "night", artist: "one"), track(3, album: "Night", artist: "Two"), track(4), a], kind: .album)
        XCTAssertEqual(groups.count, 2)
        XCTAssertEqual(groups[0].tracks.map(\.id), [1, 2])
        XCTAssertEqual(groups[0].duration, 180)
        XCTAssertEqual(groups[1].tracks.map(\.id), [3])
        XCTAssertNotEqual(groups[0].id, groups[1].id)
    }
    func testArtistGroupingPreservesUnknownTracksAndStableOrder() {
        let groups = MusicCollection.groups([track(1, artist: "Артист"), track(2, artist: " артист "), track(3)], kind: .artist)
        XCTAssertEqual(groups.map(\.title), ["Артист", "Без исполнителя"])
        XCTAssertEqual(groups.first?.tracks.map(\.id), [1, 2])
    }
    @MainActor func testArtworkPaletteComesFromImageAndStaysDark() {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 16, height: 16)).image { context in
            UIColor.red.setFill(); context.fill(CGRect(x: 0, y: 0, width: 16, height: 16))
        }
        let palette = MusicArtworkPalette.sample(image)
        XCTAssertGreaterThan(palette.red, palette.blue)
        XCTAssertLessThanOrEqual(palette.red, 0.43)
        XCTAssertEqual(MusicArtworkPalette.sample(nil), .neutral)
    }
    func testLyricsRejectInvalidTimesAndKeepDuplicateTimestampOrder() {
        let lyrics = NativeLyrics(["lyrics": ["source": "embedded", "synced": true, "text": "Real text", "lines": [
            ["time": 20, "text": "Second"], ["time": -1, "text": "Invalid"], ["time": Double.infinity, "text": "Invalid"],
            ["time": 0, "text": "First"], ["time": 20, "text": "Same timestamp"], ["time": 50, "text": "  "]]]])
        XCTAssertEqual(lyrics.lines.map(\.text), ["First", "Second", "Same timestamp"])
        XCTAssertEqual(lyrics.activeLine(at: 19), 0)
        XCTAssertEqual(lyrics.activeLine(at: 20), 2)
        XCTAssertNil(lyrics.activeLine(at: .nan))
    }
    func testMissingLyricsAreHonestAndPlainLyricsAreNotSeekable() {
        XCTAssertTrue(NativeLyrics(["lyrics": ["source": "none", "text": "", "lines": []]]).empty)
        let plain = NativeLyrics(["lyrics": ["source": "owner", "text": "Owner text", "synced": false, "lines": [["time": 0, "text": "Owner text"]]]])
        XCTAssertFalse(plain.empty); XCTAssertFalse(plain.synced); XCTAssertNil(plain.activeLine(at: 10))
    }
    @MainActor func testCollectionsReadFollowingPagesWithoutDuplicateRows() async {
        let service = CollectionServiceFixture(), catalog = MusicCollectionCatalog(api: service)
        await catalog.load()
        XCTAssertEqual(catalog.tracks.map(\.id), [1, 2, 3]); XCTAssertTrue(catalog.complete)
        XCTAssertEqual(service.paths.count, 2); XCTAssertFalse(catalog.loading)
    }
    @MainActor func testCollectionsStopOnBrokenPaginationRatherThanLoop() async {
        let service = CollectionServiceFixture(); service.invalidNextOffset = true
        let catalog = MusicCollectionCatalog(api: service)
        await catalog.load()
        XCTAssertNotNil(catalog.error); XCTAssertFalse(catalog.complete)
        XCTAssertEqual(service.paths.count, 1)
    }
}
