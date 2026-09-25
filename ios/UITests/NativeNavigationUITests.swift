import XCTest

final class NativeNavigationUITests: XCTestCase {
    @MainActor func testSettingsAndEnrollmentDoNotOpenWebPages() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Настройки"].waitForExistence(timeout: 12))
        app.tabBars.buttons["Настройки"].tap()
        XCTAssertTrue(app.buttons["nativeServerAccess"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.webViews.count, 0)
        app.buttons["nativeServerAccess"].tap()
        XCTAssertTrue(app.navigationBars["Сервер и доступ"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.webViews.count, 0)
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["nativeEnrollmentSettings"].tap()
        XCTAssertTrue(app.secureTextFields["nativeEnrollmentPair"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.webViews.count, 0, "Sign-in must use the native pairing form")
        app.buttons["Отмена"].tap()
        XCTAssertTrue(app.tabBars.buttons["Музыка"].isHittable)
    }

    @MainActor func testMiniPlayerDoesNotCoverAnyTab() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture"]
        app.launch()
        XCTAssertTrue(app.buttons["nativeMiniPlayer"].waitForExistence(timeout: 12))
        for title in ["Устройства", "Загрузки", "Настройки", "Музыка"] {
            let tab = app.tabBars.buttons[title]
            XCTAssertTrue(tab.isHittable, "Tab must stay tappable: " + title)
            tab.tap()
            XCTAssertEqual(app.webViews.count, 0)
            let mini = app.buttons["nativeMiniPlayer"]
            XCTAssertTrue(mini.exists)
            XCTAssertLessThanOrEqual(mini.frame.maxY, app.tabBars.firstMatch.frame.minY + 1)
        }
    }

    @MainActor func testPlayerCanOpenAndDismissOneRouteSheet() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "player"]
        app.launch()
        XCTAssertTrue(app.buttons["nativePlayerDevices"].waitForExistence(timeout: 12))
        app.buttons["nativePlayerDevices"].tap()
        XCTAssertTrue(app.navigationBars["Где слушать"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["route-local"].exists)
        XCTAssertTrue(app.buttons["route-confirm"].exists)
        XCTAssertEqual(app.webViews.count, 0)
        app.buttons["Закрыть"].tap()
        XCTAssertTrue(app.tabBars.buttons["Музыка"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["Музыка"].isHittable)
    }
}
