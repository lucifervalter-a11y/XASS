import XCTest

/// Fixture-mode (FixturePlayerState) checks for the Apple Music-style player.
final class NativeNowPlayingInterfaceTests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor func testMiniPlayerExpandsCollapsesAndOpensLyrics() throws {
        let app = launch(screen: "miniplayer")
        let mini = app.buttons["nativeMiniPlayer"]
        XCTAssertTrue(mini.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["nativePlayerToggle"].exists, "Now Playing starts collapsed")
        XCTAssertTrue(app.tabBars.buttons["Музыка"].isHittable, "The mini player must sit above the tab bar")
        capture(app, "NowPlaying-Mini")

        mini.tap()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 5))
        waitForDisappearance(app.buttons["nativeMiniPlayer"])
        XCTAssertTrue(app.buttons["nowPlayingCollapse"].exists)
        XCTAssertFalse(app.buttons["miniPlayerPlayPause"].exists, "Mini controls are hidden at once, never shown with the card's")
        capture(app, "NowPlaying-Expanded")

        let lyrics = app.buttons["nativePlayerLyrics"]
        XCTAssertTrue(lyrics.waitForExistence(timeout: 5))
        lyrics.tap()
        XCTAssertTrue(app.buttons["lyric-line-2"].waitForExistence(timeout: 5), "Fixture lyrics must render as tappable lines")
        XCTAssertTrue(app.buttons["nativePlayerToggle"].exists, "Transport stays available in lyrics mode")
        XCTAssertEqual(app.buttons.matching(identifier: "nativePlayerToggle").count, 1, "One transport, moved, not a crossfaded copy")
        XCTAssertEqual(app.buttons.matching(identifier: "nativePlayerLyrics").count, 1)
        XCTAssertEqual(app.descendants(matching: .any).matching(identifier: "nowPlayingArtwork").count, 1,
                       "The same artwork shrinks into the lyrics header")
        capture(app, "NowPlaying-Lyrics")

        app.buttons["nowPlayingCollapse"].tap()
        XCTAssertTrue(app.buttons["nativeMiniPlayer"].waitForExistence(timeout: 5))
        waitForDisappearance(app.buttons["nativePlayerToggle"])
        XCTAssertEqual(app.webViews.count, 0)
    }

    @MainActor func testSwipeDownCanBeCancelledOrDismissesAndPlayPauseToggles() throws {
        let app = launch(screen: "nowplaying")
        let toggle = app.buttons["nativePlayerToggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        XCTAssertEqual(toggle.label, "Пауза", "Fixture starts playing")
        toggle.tap()
        let paused = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "Слушать"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [paused], timeout: 3), .completed, "Play/pause toggles the fixture")
        XCTAssertFalse(app.buttons["nativeMiniPlayer"].exists, "Mini player never shows on top of Now Playing")
        // Start on the header label: a non-button area that always starts a dismiss.
        let header = app.staticTexts["Тестовый плеер"]
        XCTAssertTrue(header.waitForExistence(timeout: 3))
        let start = header.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        // Short drag, held still before release: springs back.
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 90)), withVelocity: .slow, thenHoldForDuration: 0.4)
        XCTAssertTrue(app.buttons["nowPlayingCollapse"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["nativeMiniPlayer"].exists, "A cancelled swipe keeps Now Playing open")
        // Long, fast drag: collapses to the mini player.
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 0, dy: 460)), withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertTrue(app.buttons["nativeMiniPlayer"].waitForExistence(timeout: 5), "Swipe down collapses Now Playing")
        waitForDisappearance(app.buttons["nowPlayingCollapse"])
    }

    @MainActor func testLyricsScreenHighlightsAndSeeks() throws {
        let app = launch(screen: "lyrics")
        // Fixture starts at 0:41 (line 7); line 10 starts at 0:53, so only a seek highlights it within the timeout.
        let line = app.buttons["lyric-line-10"]
        XCTAssertTrue(line.waitForExistence(timeout: 10))
        let current = NSPredicate(format: "value == %@", "Текущая строка")
        XCTAssertTrue(app.buttons.matching(current).firstMatch.waitForExistence(timeout: 5), "One line is highlighted in sync with the fixture clock")
        line.tap()
        let selected = XCTNSPredicateExpectation(predicate: current, object: line)
        XCTAssertEqual(XCTWaiter.wait(for: [selected], timeout: 4), .completed, "Tapping a line seeks to it")
        capture(app, "NowPlaying-Lyrics-Seek")
    }

    @MainActor private func launch(screen: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", screen]
        app.launch()
        return app
    }

    @MainActor private func waitForDisappearance(_ element: XCUIElement) {
        let removed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element)
        XCTAssertEqual(XCTWaiter.wait(for: [removed], timeout: 5), .completed)
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
}
