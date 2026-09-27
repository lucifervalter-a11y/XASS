import XCTest

final class NativeInterfaceTests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor func testAllPrimaryTabsRemainReachableWithMiniPlayer() throws {
        let app = launch(screen: "home")
        for title in ["Главная", "Музыка", "Сайт", "Инструменты", "Погода", "Главная"] {
            selectTab(title, in: app)
            switch title {
            case "Главная": XCTAssertTrue(app.staticTexts["Тестовый XASS"].waitForExistence(timeout: 5))
            case "Музыка": XCTAssertTrue(app.buttons["track-1"].waitForExistence(timeout: 5))
            case "Сайт": XCTAssertTrue(app.buttons["nativeEditSiteProfile"].waitForExistence(timeout: 5))
            case "Инструменты": XCTAssertTrue(app.buttons["nativeToolsDevices"].waitForExistence(timeout: 5))
            case "Погода": XCTAssertTrue(app.staticTexts["Москва"].waitForExistence(timeout: 5))
            default: XCTFail("Unknown tab")
            }
            XCTAssertEqual(app.webViews.count, 0, "\(title) must be rendered by the native interface")
            capture(app, "Native-Tab-\(title)")
        }
    }

    @MainActor func testRealNativeLibraryPlayerAndDeviceConfirmation() throws {
        let app = launch(screen: "home")
        selectTab("Музыка", in: app)
        XCTAssertTrue(app.buttons["track-1"].waitForExistence(timeout: 12))
        XCTAssertEqual(app.webViews.count, 0, "The music library must not be a website wrapper")
        XCTAssertTrue(app.buttons["nativePlayAll"].exists)
        XCTAssertTrue(app.buttons["nativeShuffleAll"].exists)
        capture(app, "Native-Library")
        app.buttons["Избранное"].tap()
        XCTAssertTrue(app.buttons["track-1"].exists)
        waitForDisappearance(app.buttons["track-2"])
        app.buttons["Все"].tap()
        XCTAssertTrue(app.buttons["track-2"].waitForExistence(timeout: 5))
        selectTab("Инструменты", in: app)
        let devices = app.buttons["nativeToolsDevices"]
        XCTAssertTrue(devices.waitForExistence(timeout: 5))
        devices.tap()
        let studio = app.buttons["native-device-1"]
        XCTAssertTrue(studio.waitForExistence(timeout: 6), "Device Студия must appear")
        studio.tap()
        XCTAssertTrue(app.buttons["Заблокировать экран"].waitForExistence(timeout: 5))
        capture(app, "Native-Device")
        app.buttons["Заблокировать экран"].tap()
        XCTAssertTrue(app.buttons["Отмена"].waitForExistence(timeout: 3))
        app.buttons["Отмена"].tap()
        XCTAssertEqual(app.webViews.count, 0)
    }

    @MainActor func testNativePlayerAndRoutesRenderWithoutWebView() throws {
        let app = launch(screen: "player")
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Player")
        app.buttons["nativePlayerDevices"].tap()
        XCTAssertTrue(app.buttons["route-local"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.navigationBars["Куда играть"].exists)
        XCTAssertTrue(app.buttons["route-local"].isEnabled)
        XCTAssertTrue(app.buttons["route-device-1"].isEnabled)
        XCTAssertTrue(app.buttons["route-device-2"].exists)
        XCTAssertFalse(app.buttons["route-device-2"].isEnabled, "An offline PC must not accept a playback transfer")
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Routes")
    }

    @MainActor func testNativeSearchAndLargeTextPlayerCanReachBottomControls() throws {
        let app = launch(screen: "library")
        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 10)); search.tap(); search.typeText("север")
        XCTAssertTrue(app.buttons["track-2"].waitForExistence(timeout: 5))
        waitForDisappearance(app.buttons["track-1"])
        capture(app, "Native-Search")
        app.terminate()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "player", "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        app.launch()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 10))
        let lyrics = app.buttons["nativePlayerLyrics"]
        for _ in 0..<8 { if lyrics.isHittable { break }; app.scrollViews.firstMatch.swipeUp() }
        XCTAssertTrue(lyrics.isHittable, "Large Dynamic Type must scroll to bottom player controls")
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Player-LargeText")
        let more = app.buttons["nativePlayerMore"]
        for _ in 0..<8 { if more.isHittable { break }; app.scrollViews.firstMatch.swipeDown() }
        XCTAssertTrue(more.isHittable); more.tap()
        XCTAssertTrue(app.buttons["nativeDownload"].waitForExistence(timeout: 5), "Download remains available in native track actions")
        XCTAssertTrue(app.switches["nativeShareSite"].exists, "Website sharing is preserved")
    }

    @MainActor func testNativeAlbumsAndArtistsUseActualMetadata() throws {
        let app = launch(screen: "library")
        let albums = app.buttons["nativeAlbums"]
        XCTAssertTrue(albums.waitForExistence(timeout: 10)); albums.tap()
        let album = app.buttons["nativeAlbum-1"]
        XCTAssertTrue(album.waitForExistence(timeout: 8)); album.tap()
        XCTAssertTrue(app.buttons["nativeCollectionPlay"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Ночной маршрут"].exists)
        XCTAssertTrue(app.buttons["track-1"].exists); XCTAssertTrue(app.buttons["track-2"].exists)
        XCTAssertFalse(app.buttons["track-3"].exists)
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Album-Detail")
        app.terminate()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "library"]
        app.launch()
        let artists = app.buttons["nativeArtists"]
        XCTAssertTrue(artists.waitForExistence(timeout: 10)); artists.tap()
        XCTAssertTrue(app.buttons["nativeArtist-1"].waitForExistence(timeout: 8))
        capture(app, "Native-Artists")
    }

    @MainActor func testLyricsAndQueueKeepNativeControls() throws {
        let app = launch(screen: "player")
        let lyrics = app.buttons["nativePlayerLyrics"]
        XCTAssertTrue(lyrics.waitForExistence(timeout: 10))
        if !lyrics.isHittable { app.scrollViews.firstMatch.swipeUp() }
        lyrics.tap()
        XCTAssertTrue(app.buttons["lyric-line-2"].waitForExistence(timeout: 8))
        XCTAssertTrue(app.buttons["nativePlayerToggle"].exists)
        capture(app, "Native-Lyrics")
        app.buttons["nativePlayerQueue"].tap()
        XCTAssertTrue(app.buttons["nativeQueueShuffle"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["nativeQueueRepeat"].exists)
        XCTAssertTrue(app.buttons["queue-track-1"].exists)
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Queue")
    }

    @MainActor private func launch(screen: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", screen]
        app.launch()
        return app
    }

    @MainActor private func selectTab(_ title: String, in app: XCUIApplication) {
        let tab = app.tabBars.buttons[title]
        XCTAssertTrue(tab.waitForExistence(timeout: 10))
        XCTAssertTrue(tab.isHittable, "The mini player must not cover the \(title) tab")
        tab.tap()
        let selected = XCTNSPredicateExpectation(predicate: NSPredicate(format: "selected == true"), object: tab)
        XCTAssertEqual(XCTWaiter.wait(for: [selected], timeout: 5), .completed, "Tapping \(title) must select that tab")
        XCTAssertFalse(app.buttons["nativePlayerToggle"].exists, "Tab navigation must not open the player sheet")
    }

    @MainActor private func waitForDisappearance(_ element: XCUIElement) {
        // Search is debounced and updates after the API response, not the keystroke.
        let removed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element)
        XCTAssertEqual(XCTWaiter.wait(for: [removed], timeout: 5), .completed)
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
}
