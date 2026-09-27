import XCTest

final class NativeImportInterfaceTests: XCTestCase {
    @MainActor func testImportHasExplicitLimitsAndDoesNotLoseLibraryOnDismiss() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "library"]
        app.launch()
        let add = app.buttons["Добавить музыку"]
        XCTAssertTrue(add.waitForExistence(timeout: 10)); add.tap()
        let choose = app.buttons["musicImportChooseFiles"]
        XCTAssertTrue(choose.waitForExistence(timeout: 5))
        XCTAssertTrue(choose.isEnabled)
        XCTAssertTrue(app.staticTexts["musicImportLimits"].exists)
        XCTAssertTrue(app.staticTexts["ZIP-архив"].exists)
        XCTAssertEqual(app.webViews.count, 0)
        app.buttons["musicImportDone"].tap()
        XCTAssertTrue(add.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["track-1"].exists)
    }
}
