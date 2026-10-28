import XCTest
import SwiftUI
@testable import XASS

final class ThemeManagerTests: XCTestCase {

    func testAllSixThemesExist() {
        let expectedThemes: [AppTheme] = [.cyber, .tokyo, .nordic, .matrix, .sunset, .glass]
        XCTAssertEqual(AppTheme.allCases.count, 6)
        for theme in expectedThemes {
            XCTAssertTrue(AppTheme.allCases.contains(theme), "Theme \(theme.rawValue) must be defined")
        }
    }

    func testThemePropertiesAreNonEmpty() {
        for theme in AppTheme.allCases {
            XCTAssertFalse(theme.rawValue.isEmpty)
            XCTAssertFalse(theme.displayName.isEmpty)
            XCTAssertFalse(theme.localizedDescription.isEmpty)
            XCTAssertFalse(theme.systemIcon.isEmpty)
            XCTAssertFalse(theme.accessibilityLabel.isEmpty)
            XCTAssertFalse(theme.accessibilityHint.isEmpty)
        }
    }

    func testThemeAccessibilityLabelsIncludeDisplayNameAndDescription() {
        for theme in AppTheme.allCases {
            XCTAssertTrue(theme.accessibilityLabel.contains(theme.displayName))
            XCTAssertTrue(theme.accessibilityLabel.contains(theme.localizedDescription))
            XCTAssertTrue(theme.accessibilityHint.contains(theme.displayName))
        }
    }

    @MainActor
    func testThemeSelectionUpdatesManager() {
        let manager = ThemeManager.shared
        let originalTheme = manager.currentTheme

        manager.setTheme(.tokyo)
        XCTAssertEqual(manager.currentTheme, .tokyo)

        manager.setTheme(.matrix)
        XCTAssertEqual(manager.currentTheme, .matrix)

        manager.setTheme(.sunset)
        XCTAssertEqual(manager.currentTheme, .sunset)

        manager.setTheme(.nordic)
        XCTAssertEqual(manager.currentTheme, .nordic)

        manager.setTheme(.glass)
        XCTAssertEqual(manager.currentTheme, .glass)

        manager.setTheme(.cyber)
        XCTAssertEqual(manager.currentTheme, .cyber)

        // Reset to original
        manager.setTheme(originalTheme)
    }

    func testColorThemeExtensions() {
        for theme in AppTheme.allCases {
            let bg = Color.themeBackground(for: theme)
            let card = Color.themeCard(for: theme)
            let accent = Color.themeAccent(for: theme)
            let glow = Color.themeGlow(for: theme)
            let secondary = Color.themeSecondary(for: theme)

            XCTAssertNotNil(bg)
            XCTAssertNotNil(card)
            XCTAssertNotNil(accent)
            XCTAssertNotNil(glow)
            XCTAssertNotNil(secondary)
        }
    }
}
