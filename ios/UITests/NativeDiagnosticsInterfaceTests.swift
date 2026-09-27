import XCTest

final class NativeDiagnosticsInterfaceTests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor func testDiagnosticsCanPrepareShareDocumentAndClearWithoutServerActions() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "tools"]
        app.launch()
        let entry = app.buttons["nativeToolsAppDiagnostics"]
        for _ in 0..<4 { if entry.isHittable { break }; app.swipeUp() }
        XCTAssertTrue(entry.waitForExistence(timeout: 8)); entry.tap()
        XCTAssertTrue(app.navigationBars["Журнал приложения"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.switches["nativeDiagnosticsEnabled"].exists)
        let prepare = app.buttons["nativeDiagnosticsPrepare"]
        XCTAssertTrue(prepare.waitForExistence(timeout: 5)); prepare.tap()
        XCTAssertTrue(app.buttons["nativeDiagnosticsShare"].waitForExistence(timeout: 8))
        XCTAssertEqual(app.webViews.count, 0)
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = "Native-App-Diagnostics"; attachment.lifetime = .keepAlways; add(attachment)
        let clear = app.buttons["nativeDiagnosticsClear"]
        for _ in 0..<6 { if clear.isHittable { break }; app.swipeUp() }
        XCTAssertTrue(clear.isHittable); clear.tap()
        XCTAssertTrue(app.buttons["Отмена"].waitForExistence(timeout: 3))
        app.buttons["Отмена"].tap()
        for _ in 0..<6 { if app.buttons["nativeDiagnosticsShare"].isHittable { break }; app.swipeDown() }
        XCTAssertTrue(app.buttons["nativeDiagnosticsShare"].exists, "Cancelling clear keeps the prepared share document")
    }
}
