import XCTest

final class NativeInterfaceTests: XCTestCase {
    @MainActor func testRealNativeLibraryPlayerAndDeviceConfirmation() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture"]
        app.launch()
        XCTAssertTrue(app.buttons["track-1"].waitForExistence(timeout: 12))
        XCTAssertEqual(app.webViews.count, 0, "The music library must not be a website wrapper")
        XCTAssertTrue(app.buttons["nativePlayAll"].exists)
        XCTAssertTrue(app.buttons["nativeShuffleAll"].exists)
        XCTAssertTrue(app.tabBars.buttons["Музыка"].exists)
        XCTAssertTrue(app.tabBars.buttons["Устройства"].exists)
        XCTAssertTrue(app.tabBars.buttons["Загрузки"].exists)
        XCTAssertTrue(app.tabBars.buttons["Настройки"].exists)
        capture(app, "Native-Library")
        app.buttons["Избранное"].tap()
        XCTAssertTrue(app.buttons["track-1"].exists)
        XCTAssertFalse(app.buttons["track-2"].exists)
        app.buttons["Все"].tap()
        app.tabBars.buttons["Устройства"].tap()
        app.staticTexts["Студия"].firstMatch.tap()
        XCTAssertTrue(app.buttons["Заблокировать экран"].waitForExistence(timeout: 5))
        capture(app, "Native-Device")
        app.buttons["Заблокировать экран"].tap()
        XCTAssertTrue(app.buttons["Отмена"].waitForExistence(timeout: 3))
        app.buttons["Отмена"].tap()
        XCTAssertEqual(app.webViews.count, 0)
    }

    @MainActor func testNativePlayerAndRoutesRenderWithoutWebView() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "player"]
        app.launch()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Player")
        app.buttons["nativePlayerDevices"].tap()
        XCTAssertTrue(app.staticTexts["Где слушать"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Переключить"].exists)
        capture(app, "Native-Routes")
    }

    @MainActor func testNativeSearchAndLargeTextPlayerCanReachBottomControls() throws {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture"]
        app.launch()
        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 10)); search.tap(); search.typeText("север")
        XCTAssertTrue(app.buttons["track-2"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["track-1"].exists)
        capture(app, "Native-Search")
        app.terminate()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "player", "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        app.launch()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 10))
        let download = app.buttons["nativeDownload"]
        for _ in 0..<8 { if download.isHittable { break }; app.scrollViews.firstMatch.swipeUp() }
        XCTAssertTrue(download.isHittable, "Large Dynamic Type must scroll to sharing/download controls")
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-Player-LargeText")
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
}
