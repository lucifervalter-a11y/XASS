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
    @MainActor func testHostPresentsTheOpeningPickerInsteadOfBeingOne() {
        final class UnusedDelegate: NSObject, UIDocumentPickerDelegate {}
        let host = NativeMusicPickerHost()
        let picker = NativeMusicDocumentPicker.openingController(delegate: UnusedDelegate())
        XCTAssertTrue(picker.allowsMultipleSelection)
        host.wantsPicker = true
        host.makePicker = { picker }
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 320, height: 480))
        window.rootViewController = host
        window.makeKeyAndVisible()
        host.loadViewIfNeeded()
        host.viewDidAppear(false)
        XCTAssertTrue(host.presentedViewController === picker)
        XCTAssertFalse(host is UIDocumentPickerViewController)
    }
}
