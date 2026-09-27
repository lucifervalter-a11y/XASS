import XCTest

final class NativeVolumeInterfaceTests: XCTestCase {
    @MainActor func testPlayerShowsSystemVolumeInsteadOfRemoteGainSlider() {
        let app = XCUIApplication()
        app.launchArguments = ["--native-ui-fixture", "--native-ui-screen", "player"]
        app.launch()
        XCTAssertTrue(app.buttons["nativePlayerToggle"].waitForExistence(timeout: 10))
        let label = app.staticTexts["nativeSystemVolumeLabel"]
        for _ in 0..<5 { if label.isHittable { break }; app.scrollViews.firstMatch.swipeUp() }
        XCTAssertTrue(label.isHittable)
        XCTAssertEqual(label.label, "Громкость iPhone")
        XCTAssertFalse(app.sliders["nativeRemoteVolume"].exists)
        XCTAssertEqual(app.webViews.count, 0)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Native-System-Volume"; screenshot.lifetime = .keepAlways; add(screenshot)
        // MPVolumeView's volume/route mutation is unsupported in Simulator.
        // Hardware button and actual output checks require a physical iPhone.
    }
}
