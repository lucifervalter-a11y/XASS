import XCTest

final class DevicePickerInterfaceTests: XCTestCase {
    override func setUpWithError() throws { continueAfterFailure = false }

    @MainActor func testPickerListsDevicesAndSwitchesInsideOneSheet() {
        let app = launch("devicepicker")
        let phone = app.buttons["devicePicker-local"]
        XCTAssertTrue(phone.waitForExistence(timeout: 10))
        XCTAssertTrue(app.navigationBars["Где слушать"].exists)
        XCTAssertTrue(app.buttons["devicePicker-device-1"].exists)
        XCTAssertTrue(app.buttons["devicePicker-device-2"].exists)
        XCTAssertTrue(phone.label.contains("Играет сейчас"), "Active device announces that it plays: \(phone.label)")
        capture(app, "Native-DevicePicker")
        app.buttons["devicePicker-device-2"].tap()
        XCTAssertTrue(element(app, "deviceStatus-deviceOffline").waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["deviceStatusRetry"].exists)
        capture(app, "Native-DevicePicker-Offline")
        app.buttons["devicePicker-device-1"].tap()
        XCTAssertTrue(app.buttons["remotePlayPause"].waitForExistence(timeout: 8), "The remote appears in the same sheet after the switch")
        XCTAssertEqual(app.webViews.count, 0)
        capture(app, "Native-DevicePicker-Remote")
    }

    @MainActor func testFailureAndEmptyStatesAreExplicit() {
        let app = launch("devicepicker", state: "failed")
        XCTAssertTrue(element(app, "deviceStatus-switchFailed").waitForExistence(timeout: 10))
        capture(app, "Native-DevicePicker-Failed")
        app.buttons["deviceStatusRetry"].tap()
        XCTAssertTrue(app.buttons["remotePlayPause"].waitForExistence(timeout: 8))
        app.terminate()
        let empty = launch("devicepicker", state: "empty")
        XCTAssertTrue(element(empty, "deviceStatus-noDevices").waitForExistence(timeout: 10))
        XCTAssertTrue(empty.buttons["devicePicker-local"].exists)
        capture(empty, "Native-DevicePicker-Empty")
        empty.terminate()
        let connecting = launch("devicepicker", state: "connecting")
        XCTAssertTrue(element(connecting, "deviceStatus-connecting").waitForExistence(timeout: 10))
        XCTAssertTrue(connecting.buttons["deviceStatusCancel"].exists)
        capture(connecting, "Native-DevicePicker-Connecting")
    }

    @MainActor func testRemoteControlsRespondInFixture() {
        let app = launch("remote")
        let toggle = app.buttons["remotePlayPause"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        XCTAssertTrue(app.sliders["remoteSeek"].exists)
        XCTAssertTrue(app.sliders["remoteVolume"].exists)
        XCTAssertTrue(app.buttons["remoteNext"].exists)
        XCTAssertTrue(app.buttons["remotePrevious"].exists)
        XCTAssertEqual(toggle.label, "Пауза")
        toggle.tap()
        let paused = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", "Слушать"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [paused], timeout: 5), .completed)
        capture(app, "Native-Remote")
    }

    @MainActor private func launch(_ screen: String, state: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", screen] + (state.map { ["--native-ui-device-state", $0] } ?? [])
        app.launch()
        return app
    }

    @MainActor private func element(_ app: XCUIApplication, _ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
}
