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
        // Wait for the native confirmation sheet to finish dismissing before testing hit targets.
        let share = app.buttons["nativeDiagnosticsShare"]
        let accessible = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == true AND isHittable == true"), object: share)
        let result = XCTWaiter.wait(for: [accessible], timeout: 5)
        if result != .completed {
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = "Native-Diagnostics-After-Cancel"; screenshot.lifetime = .keepAlways; add(screenshot)
            let hierarchy = XCTAttachment(string: app.debugDescription)
            hierarchy.name = "Native-Diagnostics-After-Cancel-Hierarchy"; hierarchy.lifetime = .keepAlways; add(hierarchy)
        }
        XCTAssertEqual(result, .completed, "Cancelling clear keeps the prepared share document accessible after dismissal")
    }
}
