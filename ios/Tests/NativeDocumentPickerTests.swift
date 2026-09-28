import XCTest
import SwiftUI
import UIKit
import UniformTypeIdentifiers
@testable import XASS

final class NativeDocumentPickerTests: XCTestCase {
    @MainActor func testSelectionIsDeliveredOnceBeforeDismissingPicker() {
        var presented = true
        var calls = 0
        let url = URL(fileURLWithPath: "/fixture.mp3")
        let picker = NativeMusicDocumentPicker(presented: Binding(get: { presented }, set: { presented = $0 })) { urls in
            XCTAssertTrue(presented, "The import engine must acquire access before dismissal")
            XCTAssertEqual(urls, [url]); calls += 1
        }
        let coordinator = picker.makeCoordinator()
        let controller = UIDocumentPickerViewController(forOpeningContentTypes: [.audio], asCopy: false)
        coordinator.documentPicker(controller, didPickDocumentsAt: [url])
        coordinator.documentPicker(controller, didPickDocumentsAt: [url])
        XCTAssertEqual(calls, 1); XCTAssertFalse(presented)
    }
    @MainActor func testCancellationDoesNotStartImportAndIgnoresLateSelection() {
        var presented = true
        let picker = NativeMusicDocumentPicker(presented: Binding(get: { presented }, set: { presented = $0 })) { _ in
            XCTFail("A cancelled picker cannot start a late import")
        }
        let coordinator = picker.makeCoordinator()
        let controller = UIDocumentPickerViewController(forOpeningContentTypes: [.audio], asCopy: false)
        coordinator.documentPickerWasCancelled(controller)
        coordinator.documentPicker(controller, didPickDocumentsAt: [URL(fileURLWithPath: "/fixture.mp3")])
        XCTAssertFalse(presented)
    }
    @MainActor func testHostWithoutAWindowDoesNotPresentFiles() {
        final class UnusedDelegate: NSObject, UIDocumentPickerDelegate {}
        let delegate = UnusedDelegate()
        let picker = NativeMusicDocumentPicker.openingController(delegate: delegate)
        XCTAssertTrue(picker.allowsMultipleSelection)
        XCTAssertTrue(picker.delegate === delegate)
        let host = NativeMusicPickerHost()
        host.wantsPicker = true
        host.makePicker = { picker }
        host.presentPickerIfNeeded()
        XCTAssertNil(host.presentedViewController)
    }
}
