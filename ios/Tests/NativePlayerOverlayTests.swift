import XCTest
import UIKit
import SwiftUI
@testable import XASS

final class NativePlayerOverlayTests: XCTestCase {
    func testDismissGestureDoesNotInheritMiniPlayerDrag() {
        XCTAssertFalse(PlayerDismissPolicy.canBegin(downward: true, allowedRegion: true, expanded: true,
                                                     gestureArmed: false, dismissing: false, sliderEditing: false))
        XCTAssertTrue(PlayerDismissPolicy.canBegin(downward: true, allowedRegion: true, expanded: true,
                                                    gestureArmed: true, dismissing: false, sliderEditing: false))
        XCTAssertFalse(PlayerDismissPolicy.canBegin(downward: true, allowedRegion: true, expanded: true,
                                                     gestureArmed: true, dismissing: false, sliderEditing: true))
    }
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
        // Enrollment (401 / account) outranks everything, including Now Playing.
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: true, devicePicker: true, transferActive: true, accountSheet: true), .accountSheet)
        XCTAssertEqual(O.resolve(hasTrack: true, expanded: true, devicePicker: false, transferActive: false, accountSheet: true), .accountSheet)
    }

    func testNowPlayingCardCollapsesIntoTheMiniPlayerAndHasOneHeroSource() {
        typealias G = NowPlayingCardGeometry
        let size = CGSize(width: 390, height: 763), insets = EdgeInsets(top: 47, leading: 0, bottom: 34, trailing: 0)
        let mini = CGRect(x: 10, y: 650, width: 370, height: 64)
        let full = CGRect(x: 0, y: -47, width: 390, height: 844)
        XCTAssertEqual(G.rect(expanded: true, reduceMotion: false, miniFrame: mini, size: size, insets: insets, dragOffset: 0), full)
        XCTAssertEqual(G.rect(expanded: false, reduceMotion: false, miniFrame: mini, size: size, insets: insets, dragOffset: 0), mini,
                       "Collapsed, the opaque card is clipped exactly to the mini player")
        XCTAssertEqual(G.rect(expanded: true, reduceMotion: false, miniFrame: mini, size: size, insets: insets, dragOffset: 120).minY, 73,
                       "The clip follows the swipe-down, so the collapse starts where the finger left it")
        XCTAssertEqual(G.rect(expanded: false, reduceMotion: true, miniFrame: mini, size: size, insets: insets, dragOffset: 0), full,
                       "Reduce Motion never morphs the frame, it only crossfades")
        XCTAssertGreaterThanOrEqual(G.rect(expanded: false, reduceMotion: false, miniFrame: .zero, size: size, insets: insets, dragOffset: 0).minY,
                                    size.height, "Without a mini player the card leaves below the screen")
        XCTAssertEqual(G.cornerRadius(expanded: false, reduceMotion: false, dragOffset: 0), 16)
        XCTAssertEqual(G.cornerRadius(expanded: true, reduceMotion: false, dragOffset: 0), 0)
        for expanded in [false, true] {
            let sources = G.heroSources(expanded: expanded)
            XCTAssertNotEqual(sources.mini, sources.card, "Exactly one matchedGeometry source per id")
        }
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

    func testTimedLyricsExposeOnlyRealInstrumentalGaps() {
        let lyrics = TimedLyrics(lines: [
            TimedLyricLine(start: 0, end: 3, text: "one"),
            TimedLyricLine(start: 6, end: 9, text: "two"),
            TimedLyricLine(start: 9.5, end: 12, text: "three")
        ])
        XCTAssertNil(lyrics.pauseAfterIndex(at: 2.9))
        XCTAssertEqual(lyrics.pauseAfterIndex(at: 3), 0)
        XCTAssertEqual(lyrics.pauseAfterIndex(at: 5.99), 0)
        XCTAssertNil(lyrics.pauseAfterIndex(at: 6))
        XCTAssertNil(lyrics.pauseAfterIndex(at: 9.25), "A sub-1.5-second spacing is not a musical pause marker")
        XCTAssertNil(lyrics.pauseAfterIndex(at: .nan))
    }

    func testPronunciationUsesProviderThenPrivateOnDeviceTransliteration() {
        let provider = TimedLyricLine(start: 0, end: 2, text: "你好", pronunciation: "ni hao")
        XCTAssertEqual(LyricPronunciation.text(for: provider), "ni hao")
        let russian = TimedLyricLine(start: 0, end: 2, text: "Привет")
        XCTAssertNotNil(LyricPronunciation.text(for: russian))
        XCTAssertTrue(LyricPronunciation.containsNonLatinLetter(russian.text))
        XCTAssertNil(LyricPronunciation.text(for: TimedLyricLine(start: 0, end: 2, text: "Hello")))
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

    func testShellSheetPriorityKeepsEnrollmentOnTop() {
        typealias S = NativeShellSheet
        XCTAssertNil(S.resolve(enrollment: false, route: false, actionsTrackID: nil, queue: false))
        XCTAssertEqual(S.resolve(enrollment: true, route: true, actionsTrackID: 7, queue: true), .enrollment)
        XCTAssertEqual(S.resolve(enrollment: true, route: false, actionsTrackID: nil, queue: true), .enrollment)
        XCTAssertEqual(S.resolve(enrollment: false, route: true, actionsTrackID: 7, queue: true), .route)
        XCTAssertEqual(S.resolve(enrollment: false, route: false, actionsTrackID: 7, queue: true), .actions(7))
        XCTAssertEqual(S.resolve(enrollment: false, route: false, actionsTrackID: nil, queue: true), .queue)
        XCTAssertEqual(S.resolve(enrollment: false, route: false, actionsTrackID: nil, queue: false, fixtureAlbums: true), .fixtureAlbums)
        XCTAssertEqual(S.resolve(enrollment: false, route: false, actionsTrackID: nil, queue: false, lyricsToolsTrackID: 7), .lyricsTools(7))
        XCTAssertEqual(S.resolve(enrollment: true, route: false, actionsTrackID: nil, queue: false, lyricsToolsTrackID: 7), .enrollment)
        XCTAssertEqual(S.resolve(enrollment: false, route: true, actionsTrackID: nil, queue: false, lyricsToolsTrackID: 7), .route)
    }
}
