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

    @MainActor func testCloseImmediatelyReleasesTabsAndRepeatedOpenDoesNotChangePlayback() throws {
        let app = launch(screen: "nowplaying")
        let toggle = app.buttons["nativePlayerToggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        toggle.tap()
        for _ in 0..<3 {
            XCTAssertEqual(toggle.label, "Слушать")
            app.buttons["nowPlayingCollapse"].tap()
            // No sleep or wait for the closing card before navigating the underlying screen.
            app.tabBars.buttons["Главная"].tap()
            XCTAssertTrue(app.staticTexts["Тестовый XASS"].waitForExistence(timeout: 3))
            let mini = app.buttons["nativeMiniPlayer"]
            XCTAssertTrue(mini.waitForExistence(timeout: 3))
            XCTAssertTrue(app.buttons["miniPlayerPlayPause"].isHittable)
            XCTAssertFalse(toggle.exists)
            mini.doubleTap()
            XCTAssertTrue(toggle.waitForExistence(timeout: 3))
            XCTAssertEqual(toggle.label, "Слушать", "A repeated expand tap must not activate the incoming transport")
            XCTAssertEqual(app.buttons.matching(identifier: "nativePlayerToggle").count, 1)
        }
        capture(app, "NowPlaying-Repeated-Reopen")
    }

    @MainActor func testReducedMotionCloseAndReopenKeepsSinglePlayer() throws {
        let app = launch(screen: "nowplaying", reducedMotion: true)
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 10))
        app.buttons["nowPlayingCollapse"].tap()
        app.tabBars.buttons["Музыка"].tap()
        XCTAssertTrue(app.buttons["nativeMiniPlayer"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["nativePlayerToggle"].exists)
        app.buttons["nativeMiniPlayer"].tap()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 3))
        XCTAssertEqual(app.buttons["nativePlayerToggle"].label, "Пауза")
        XCTAssertFalse(app.buttons["miniPlayerPlayPause"].exists)
        capture(app, "NowPlaying-ReducedMotion-Reopen")
    }

    @MainActor func testLyricsToolsHaveOneEntryAndCloseBackAndReopenKeepPlayer() throws {
        let app = launch(screen: "lyrics")
        let tools = app.buttons["nowPlayingLyricsTools"]
        XCTAssertTrue(tools.waitForExistence(timeout: 10))
        app.buttons["nowPlayingLyricsLanguage"].tap()
        XCTAssertTrue(app.buttons["lyricsTranslationToggle"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["lyricsPronunciationToggle"].exists)
        let duplicateTextAction = NSPredicate(format: "label == %@ AND identifier != %@",
                                             "Найти текст или расшифровать", "nowPlayingLyricsTools")
        XCTAssertEqual(app.buttons.matching(duplicateTextAction).count, 0, "Language menu contains only distinct language actions")
        // The screen centre lies inside the language menu's disabled hint row.
        // Tap outside the popup and verify dismissal before using the player.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.2)).tap()
        waitForDisappearance(app.buttons["lyricsTranslationToggle"])
        XCTAssertTrue(tools.isHittable)
        tools.tap()
        XCTAssertTrue(app.buttons["phoneTranscriptionDisclosure"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["lyricsToolsDone"].exists)
        XCTAssertFalse(app.buttons["nativeTrackInformation"].exists, "Lyrics opens the text tools directly, not another actions menu")
        app.buttons["lyricsToolsDone"].tap()
        XCTAssertTrue(tools.waitForExistence(timeout: 3))
        tools.tap()
        XCTAssertTrue(app.buttons["lyricsToolsDone"].waitForExistence(timeout: 3))
        app.buttons["lyricsToolsDone"].tap()
        app.buttons["nativePlayerMore"].tap()
        XCTAssertTrue(app.buttons["nativeTrackInformation"].waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["nativeTrackPCTranscription"].exists)
        app.buttons["nativeTrackInformation"].tap()
        XCTAssertTrue(app.buttons["phoneTranscriptionDisclosure"].waitForExistence(timeout: 3))
        app.navigationBars.buttons.firstMatch.tap()
        XCTAssertTrue(app.buttons["nativeTrackInformation"].waitForExistence(timeout: 3))
        app.buttons["Готово"].tap()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 3))
        capture(app, "NowPlaying-TextTools-Returned")
    }

    @MainActor private func launch(screen: String, reducedMotion: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", screen]
        if reducedMotion { app.launchArguments.append("--native-ui-reduce-motion") }
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
