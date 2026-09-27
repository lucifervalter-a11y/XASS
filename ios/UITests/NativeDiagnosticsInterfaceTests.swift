import XCTest

final class NativeDiagnosticsInterfaceTests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor func testDiagnosticsCanPrepareShareDocumentAndClearWithoutServerActions() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-diagnostics-filled", "--native-ui-screen", "tools"]
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
        XCTAssertTrue(clear.waitForExistence(timeout: 5))
        XCTAssertTrue(clear.isHittable, "Clear remains reachable without scrolling through a full history")
        let miniPlay = app.buttons["Слушать"].firstMatch
        XCTAssertTrue(miniPlay.exists)
        XCTAssertLessThan(clear.frame.maxY, miniPlay.frame.minY, "The entire Clear button stays above the pinned mini-player")
        clear.tap()
        XCTAssertTrue(app.buttons["Отмена"].waitForExistence(timeout: 3))
        app.buttons["Отмена"].tap()
        XCTAssertTrue(app.buttons["nativeDiagnosticsShare"].isHittable, "Cancelling clear keeps the prepared share document accessible")
    }
}
