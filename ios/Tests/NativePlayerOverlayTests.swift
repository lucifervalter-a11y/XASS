import XCTest
import UIKit
@testable import XASS

final class NativePlayerOverlayTests: XCTestCase {
    func testOnlyOneOverlayAtATime() {
        typealias O = NativeRootOverlay
        XCTAssertEqual(O.resolve(hasTrack: false, expanded: false, devicePicker: false, transferActive: false), .hidden)
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: false, devicePicker: false, transferActive: false), .miniPlayer)
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: true, devicePicker: false, transferActive: false), .nowPlaying)
        // The device picker always wins, so neither Now Playing nor the mini player sit under/over it.
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: true, devicePicker: true, transferActive: true), .devicePicker)
        // A transfer never draws its banner on top of Now Playing; progress shows inside it.
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: true, devicePicker: false, transferActive: true), .nowPlaying)
        // Collapsed: the banner replaces the mini player instead of stacking with it.
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: false, devicePicker: false, transferActive: true), .transferBanner)
        XCTAssertEqual(O.resolve(hasTrack: false, expanded: true, devicePicker: false, transferActive: false), .hidden)
    }

    func testTimedLyricsIndexFollowsStartTimes() {
        let lyrics = TimedLyrics(starts: [(10, "b"), (2, "a"), (20, "c")], duration: 30)
        XCTAssertEqual(lyrics.lines.map(\.text), ["a", "b", "c"])
        XCTAssertEqual(lyrics.lines.map(\.end), [10, 20, 30])
        XCTAssertNil(lyrics.index(at: 1.9))
        XCTAssertEqual(lyrics.index(at: 2), 0)
        XCTAssertEqual(lyrics.index(at: 9.99), 0)
        XCTAssertEqual(lyrics.index(at: 10), 1)
        XCTAssertEqual(lyrics.index(at: 400), 2)
        XCTAssertNil(lyrics.index(at: .nan))
        XCTAssertNil(TimedLyrics(lines: []).index(at: 5))
    }

    @MainActor func testFixturePlayerDrivesLyricsAndControls() {
        let player = FixturePlayerState(trackIndex: 0, position: 0, playing: false)
        XCTAssertGreaterThanOrEqual(player.items.count, 3)
        let count = player.lyrics?.lines.count ?? 0
        XCTAssertTrue((20...30).contains(count), "Fixture needs 20–30 lyric lines, has \(count)")
        XCTAssertNil(player.currentLyricIndex)
        let target = player.lyrics!.lines[5]
        player.seek(to: target.start)
        XCTAssertEqual(player.currentLyricIndex, 5)
        player.play(); XCTAssertTrue(player.isPlaying)
        player.pause(); XCTAssertFalse(player.isPlaying)
        let first = player.track?.id
        player.next(); XCTAssertNotEqual(player.track?.id, first); XCTAssertEqual(player.position, 0)
        player.previous(); XCTAssertEqual(player.track?.id, first)
        player.setVolume(4); XCTAssertEqual(player.volume, 1)
        player.setVolume(-1); XCTAssertEqual(player.volume, 0)
        XCTAssertTrue(player.items.contains { $0.lyrics == nil }, "One fixture track exercises the no-lyrics placeholder")
    }

    func testArtworkLookIsComputedFromTheImage() {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 64, height: 64)).image { context in
            UIColor.systemRed.setFill(); context.fill(CGRect(x: 0, y: 0, width: 64, height: 64))
        }
        let look = PlayerArtworkProcessing.compute(image.cgImage!, key: "red")
        XCTAssertEqual(look.colors.count, 3)
        XCTAssertNotNil(look.blurred)
        XCTAssertEqual(look.key, "red")
    }
}
